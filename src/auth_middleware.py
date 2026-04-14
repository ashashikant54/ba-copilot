# auth_middleware.py
# JWT auth middleware — Phase 2 Sprint 3 (CLAUDE.md A8 step 6).
#
# CONTRACT (per A3):
#   Every authenticated request carries Authorization: Bearer <JWT>.
#   The middleware decodes the token and writes the claims onto request.state
#   so downstream route handlers and helpers can read user identity uniformly:
#
#       request.state.user_id
#       request.state.org_id
#       request.state.role
#
#   Missing / invalid / expired tokens → JSON 401. The auth layer never
#   raises to the route handler — it short-circuits with a response.
#
# DEV_MODE:
#   When DEV_MODE=true in the environment, authentication is bypassed and
#   a synthetic dev user is injected:
#       user_id = "dev-user", org_id = "default", role = "super_admin".
#   This is the pilot / local-dev path. See CLAUDE.md rule 5.
#
# SWAPPABILITY (per A3):
#   Token shape matches Azure AD B2C conventions (sub, iat, exp) plus two
#   custom claims (org_id, role) that B2C supports as extension attributes.
#   Migration to RS256 + JWKS fetch is a configuration change, not a rewrite.
#
# WHAT IS IN SCOPE THIS SPRINT:
#   - create_token(...)       — issue a signed JWT (for the future login endpoint)
#   - decode_token(...)       — validate + parse; raises PyJWT exceptions on failure
#   - AuthMiddleware          — Starlette BaseHTTPMiddleware subclass
#   - get_current_user(req)   — FastAPI Depends-compatible claim reader
#   - AUTH_EXCLUDED_PATHS     — paths that bypass auth even outside DEV_MODE
#
# WHAT IS NOT IN SCOPE (deferred by design):
#   - Login / signup endpoints (Sprint 4+; main.py wiring is frozen here)
#   - require_role(...) role-gate dependency (Sprint 4 with tab visibility)
#   - Password hashing / verification (future login-flow sprint)
#   - Azure AD B2C adapter (post-pilot)

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import jwt   # PyJWT
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# ── Configuration ─────────────────────────────────────────────
JWT_SECRET_KEY    = os.environ.get("JWT_SECRET_KEY")
JWT_ALGORITHM     = "HS256"
# Override via env for shorter-lived tokens in prod, without code changes.
JWT_EXPIRES_HOURS = int(os.environ.get("JWT_EXPIRES_HOURS", "24"))

# Paths that never require a JWT even when DEV_MODE is off. Everything else
# (including /admin/*, /cache/*, /eval/*) MUST carry a token.
AUTH_EXCLUDED_PATHS = {
    "/",
    "/favicon.ico",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}
AUTH_EXCLUDED_PREFIXES = ("/static/",)

# Synthetic user injected on every request when DEV_MODE=true. Exposed as a
# module constant so tests and downstream helpers can assert against it.
DEV_MODE_USER = {
    "user_id": "dev-user",
    "org_id":  "default",
    "role":    "super_admin",
}


def _dev_mode_enabled() -> bool:
    """Read DEV_MODE at call time so tests can flip it without re-importing."""
    return os.environ.get("DEV_MODE", "").lower() == "true"


def _is_excluded_path(path: str) -> bool:
    if path in AUTH_EXCLUDED_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in AUTH_EXCLUDED_PREFIXES)


# ── Token issuance ────────────────────────────────────────────
def create_token(user_id: str, org_id: str, role: str,
                 expires_hours: int = None) -> str:
    """Build and sign a JWT carrying (user_id, org_id, role) claims.

    Uses timezone-aware UTC timestamps — PyJWT accepts either but aware
    datetimes avoid a deprecation warning on Python 3.12+. Returns the
    compact serialization (three dot-separated base64url segments).
    """
    if not JWT_SECRET_KEY:
        # In DEV_MODE the middleware bypasses decode, but token issuance
        # still needs a secret — so fail loudly even in dev if it's missing.
        raise EnvironmentError(
            "JWT_SECRET_KEY is not set. Add it to .env (see Sprint 3 notes)."
        )

    hours = expires_hours if expires_hours is not None else JWT_EXPIRES_HOURS
    now   = datetime.now(tz=timezone.utc)

    payload = {
        "sub":    user_id,
        "org_id": org_id,
        "role":   role,
        "iat":    int(now.timestamp()),
        "exp":    int((now + timedelta(hours=hours)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


# ── Token validation ──────────────────────────────────────────
def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Returns the claims dict on success.

    Raises the native PyJWT exception subclasses on failure — callers can
    branch on them cleanly:
      - jwt.ExpiredSignatureError  → 401 "Token expired"
      - jwt.InvalidSignatureError  → 401 "Signature mismatch"
      - jwt.InvalidTokenError      → 401 "Malformed or missing claims"
    """
    if not JWT_SECRET_KEY:
        raise EnvironmentError("JWT_SECRET_KEY is not set.")
    claims = jwt.decode(
        token,
        JWT_SECRET_KEY,
        algorithms=[JWT_ALGORITHM],
        options={"require": ["exp", "iat", "sub"]},
    )
    # Ensure the custom claims we depend on are present — PyJWT's `require`
    # only covers registered claim names.
    missing = [k for k in ("org_id", "role") if k not in claims]
    if missing:
        raise jwt.InvalidTokenError(f"Missing required claims: {missing}")
    return claims


# ── FastAPI / Starlette middleware ────────────────────────────
class AuthMiddleware(BaseHTTPMiddleware):
    """Injects request.state.user_id / .org_id / .role on every request.

    Execution order:
      1. DEV_MODE → write DEV_MODE_USER onto request.state, continue.
      2. Excluded path (frontend, docs) → continue without touching state
         (handlers for those paths don't need identity).
      3. Otherwise: extract Bearer token, decode, write claims, continue.
      4. Any failure in step 3 returns a JSON 401 immediately.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # (1) DEV_MODE bypass — injects synthetic super_admin so Sprint 1/2
        # semantics hold while the login flow is still being built.
        if _dev_mode_enabled():
            request.state.user_id = DEV_MODE_USER["user_id"]
            request.state.org_id  = DEV_MODE_USER["org_id"]
            request.state.role    = DEV_MODE_USER["role"]
            return await call_next(request)

        # (2) Excluded paths — frontend / OpenAPI / health probes.
        if _is_excluded_path(path):
            return await call_next(request)

        # (3) Real token path.
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Authorization header"},
            )
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"detail": "Authorization header must be 'Bearer <token>'"},
            )
        token = parts[1]

        try:
            claims = decode_token(token)
        except jwt.ExpiredSignatureError:
            return JSONResponse(status_code=401, content={"detail": "Token expired"})
        except jwt.InvalidSignatureError:
            return JSONResponse(status_code=401, content={"detail": "Invalid token signature"})
        except jwt.InvalidTokenError as e:
            return JSONResponse(status_code=401, content={"detail": f"Invalid token: {e}"})

        request.state.user_id = claims["sub"]
        request.state.org_id  = claims["org_id"]
        request.state.role    = claims["role"]
        return await call_next(request)


# ── Dependency helper ─────────────────────────────────────────
def get_current_user(request: Request) -> dict:
    """FastAPI Depends-compatible helper returning the injected claims.

    Assumes AuthMiddleware ran before the endpoint. If request.state is
    missing the fields, raises RuntimeError — that signals the middleware
    was not installed (a deployment misconfiguration, not a runtime input
    error).
    """
    try:
        return {
            "user_id": request.state.user_id,
            "org_id":  request.state.org_id,
            "role":    request.state.role,
        }
    except AttributeError:
        raise RuntimeError(
            "AuthMiddleware has not populated request.state. "
            "Ensure app.add_middleware(AuthMiddleware) runs during startup."
        )


# ── Smoke test ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Auth middleware (JWT issue + verify + DEV_MODE)")
    print("=" * 55)

    # 1. Round-trip: create a token, decode it, assert claims.
    print("\n── Round-trip (create_token → decode_token)")
    token = create_token("alice-123", "acme", "admin")
    claims = decode_token(token)
    assert claims["sub"]    == "alice-123",   claims
    assert claims["org_id"] == "acme",        claims
    assert claims["role"]   == "admin",       claims
    assert claims["exp"]    >  claims["iat"], claims
    print(f"  OK — claims={ {k: claims[k] for k in ('sub','org_id','role')} }")

    # 2. Expired token — encode manually with a past exp, expect the raise.
    print("\n── Expired token raises ExpiredSignatureError")
    past = int((datetime.now(tz=timezone.utc) - timedelta(hours=1)).timestamp())
    expired_payload = {
        "sub": "x", "org_id": "y", "role": "analyst",
        "iat": past - 60, "exp": past,
    }
    expired_token = jwt.encode(expired_payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    try:
        decode_token(expired_token)
        print("  FAIL — expired token accepted")
    except jwt.ExpiredSignatureError:
        print("  OK — ExpiredSignatureError raised as expected")

    # 3. Tampered signature — sign with a different key, expect the raise.
    # PyJWT warns about HMAC keys below 32 bytes, so the bogus key is also
    # padded — the test is about signature mismatch, not key length.
    print("\n── Wrong-secret token raises InvalidSignatureError")
    bogus_secret = "wrong-secret-" * 4   # 52 chars — well above 32-byte floor
    bogus = jwt.encode(
        {"sub": "x", "org_id": "y", "role": "admin",
         "iat": int(datetime.now(tz=timezone.utc).timestamp()),
         "exp": int((datetime.now(tz=timezone.utc) + timedelta(hours=1)).timestamp())},
        bogus_secret,
        algorithm=JWT_ALGORITHM,
    )
    try:
        decode_token(bogus)
        print("  FAIL — bogus signature accepted")
    except jwt.InvalidSignatureError:
        print("  OK — InvalidSignatureError raised as expected")

    # 4. Missing custom claim — encoded with the right secret but no org_id.
    print("\n── Token missing 'org_id' raises InvalidTokenError")
    stripped = jwt.encode(
        {"sub": "x", "role": "admin",
         "iat": int(datetime.now(tz=timezone.utc).timestamp()),
         "exp": int((datetime.now(tz=timezone.utc) + timedelta(hours=1)).timestamp())},
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
    try:
        decode_token(stripped)
        print("  FAIL — token with missing org_id accepted")
    except jwt.InvalidTokenError as e:
        print(f"  OK — InvalidTokenError raised: {e}")

    # 5. Malformed token.
    print("\n── Malformed token raises InvalidTokenError")
    try:
        decode_token("not.a.jwt")
        print("  FAIL — malformed token accepted")
    except jwt.InvalidTokenError:
        print("  OK — InvalidTokenError raised as expected")

    # 6. DEV_MODE user shape assertions.
    print("\n── DEV_MODE_USER shape assertions")
    assert DEV_MODE_USER == {
        "user_id": "dev-user", "org_id": "default", "role": "super_admin",
    }
    print(f"  OK — DEV_MODE_USER={DEV_MODE_USER}")
    print(f"  DEV_MODE currently enabled? {_dev_mode_enabled()}")

    # 7. Excluded-path predicate.
    print("\n── AUTH_EXCLUDED_PATHS predicate")
    for p, expected in [
        ("/",             True),
        ("/docs",         True),
        ("/static/x.js",  True),
        ("/health",       True),
        ("/admin/stats",  False),
        ("/cache",        False),
        ("/sessions/abc", False),
    ]:
        got = _is_excluded_path(p)
        print(f"   {p:18s} excluded={got}   (expected {expected})")
        assert got is expected, f"mismatch at {p}"

    print("\nAuth middleware smoke test complete.")
    print("(AuthMiddleware integration runs only once main.py wires it in Sprint 4.)")
