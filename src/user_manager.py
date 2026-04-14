# user_manager.py
# User / role entity — Phase 2 Sprint 3 (CLAUDE.md A8 step 5).
#
# PURPOSE:
#   Own the Phase 2 user model. Five personas per A2:
#     - subscriber     — founding account per org; provisions users
#     - super_admin    — org-wide full access
#     - admin          — creates/manages KBs; scoped dashboard views
#     - analyst        — core product user; KB access is granted per-KB
#     - app_support    — cross-org analytics only; no content access
#
#   This module is pure data. Tab visibility / endpoint-level RBAC
#   (A7 + A8 step 7) lives in auth_middleware and the future main.py wiring.
#
# STORAGE:
#   users/{org_id}/{user_id}.json   (Azure Blob container: users)
#   App Support users live under the "default" org prefix — role field is the
#   real access decider; the path is a bootstrap convenience per design Q2.
#
# CONVENTIONS (CLAUDE.md rules 1, 3, 5, 6):
#   - sys.path.append for flat imports
#   - load_dotenv() at module top (Sprint 1 ordering lesson)
#   - Every operation takes org_id; _resolve_org_id is the auth seam
#   - New container "users"; reuses the session_manager container pattern
#
# DESIGN NOTES:
#   - Email uniqueness is WITHIN ORG ONLY and CASE-INSENSITIVE. Original
#     casing is preserved on the stored blob; comparisons use .lower().
#   - get_user_by_email returns None on miss — login flows branch on that.
#   - password_hash is stored but always None here. A future login-flow
#     sprint will populate it via bcrypt (bcrypt 5.0.0 is already pinned
#     in requirements.txt, ready when that sprint lands).
#   - assign_kb_access cross-module validates the kb_id exists by calling
#     kb_manager.get_kb — catches admin-panel typos at write time rather
#     than blowing up at retrieval time.

import json
import os
import sys
import uuid
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

from session_manager import _resolve_org_id, DEFAULT_ORG_ID
import kb_manager

# ── Role constants ─────────────────────────────────────────────
ROLE_SUBSCRIBER  = "subscriber"
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN       = "admin"
ROLE_ANALYST     = "analyst"
ROLE_APP_SUPPORT = "app_support"

VALID_ROLES = frozenset({
    ROLE_SUBSCRIBER, ROLE_SUPER_ADMIN, ROLE_ADMIN,
    ROLE_ANALYST, ROLE_APP_SUPPORT,
})

# ── CONFIGURATION ──────────────────────────────────────────────
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
USERS_CONTAINER         = "users"

# update_user uses an ALLOWLIST — defense-in-depth. A future dev adding a
# new user field (e.g. "is_locked") won't accidentally make it user-mutable
# unless they add it here. KB grants go through assign/revoke helpers.
# password_hash is allowed here so the /auth/register endpoint can stamp a
# bcrypt hash via update_user; raw-dict updates from endpoints are fine
# because the /auth/* endpoints are the only callers that ever set it.
_UPDATE_ALLOWED_FIELDS = frozenset({
    "email", "role", "password_hash",
})


# ── Helpers ───────────────────────────────────────────────────
def _get_container():
    """Connect to Azure Blob and return the users container."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError(
            "AZURE_STORAGE_CONNECTION_STRING is not set. "
            "Add it to .env (local dev) or App Service settings (prod)."
        )
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container = blob_service.get_container_client(USERS_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass
    return container


def _save_user(user):
    """Persist a user dict under its org-prefixed blob path."""
    user["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    org_id = _resolve_org_id(user.get("org_id"))
    user["org_id"] = org_id
    blob_name = f"{org_id}/{user['user_id']}.json"
    data      = json.dumps(user, indent=2)
    container = _get_container()
    container.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,
        encoding="utf-8"
    )
    return user


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _validate_role(role):
    if role not in VALID_ROLES:
        raise ValueError(
            f"Invalid role '{role}'. Must be one of: {sorted(VALID_ROLES)}"
        )


def _validate_email(email):
    if not email or not email.strip():
        raise ValueError("email cannot be empty")
    if "@" not in email:
        raise ValueError(f"email '{email}' does not look like an address")


# ── Email uniqueness ──────────────────────────────────────────
def check_email_unique(org_id, email):
    """True if no existing user in this org has this email (case-insensitive)."""
    org_id = _resolve_org_id(org_id)
    needle = email.lower()
    for user in list_users(org_id):
        if user.get("email", "").lower() == needle:
            return False
    return True


# ── CRUD ──────────────────────────────────────────────────────
def create_user(org_id, email, role, accessible_kb_ids=None):
    """Create a new user. Returns the generated user_id.

    Email uniqueness is enforced within the org, case-insensitively. Original
    casing is preserved on the stored blob. App Support users are stored
    under the default org's prefix — the role claim is what grants cross-org
    visibility, not the blob path.
    """
    _validate_email(email)
    _validate_role(role)

    # App Support ignores the caller's org_id — always stored under default.
    org_id = DEFAULT_ORG_ID if role == ROLE_APP_SUPPORT else _resolve_org_id(org_id)

    if not check_email_unique(org_id, email):
        raise ValueError(
            f"email '{email}' is already registered in org '{org_id}'"
        )

    user_id = uuid.uuid4().hex[:8]
    user = {
        "user_id":           user_id,
        "org_id":            org_id,
        "email":             email,
        "role":              role,
        "accessible_kb_ids": list(accessible_kb_ids or []),
        "password_hash":     None,   # set by the future login-flow sprint
        "created_at":        _now(),
        "updated_at":        _now(),
    }
    _save_user(user)
    print(f"Created user '{email}' ({user_id}, role={role}) in org '{org_id}'")
    return user_id


def get_user(org_id, user_id):
    """Load a user by id. Raises FileNotFoundError if missing."""
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    try:
        blob = container.get_blob_client(f"{org_id}/{user_id}.json")
        data = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except ResourceNotFoundError:
        raise FileNotFoundError(
            f"User '{user_id}' not found in org '{org_id}' "
            f"(looked at {org_id}/{user_id}.json)"
        )


def get_user_by_email(org_id, email):
    """Return the user dict with this email, or None if not found.

    Case-insensitive match. Intended for login flows — returning None keeps
    the 'no such account' path a normal business branch rather than an
    exception, so the caller can emit a generic 401 without leaking
    whether the email exists.
    """
    org_id = _resolve_org_id(org_id)
    needle = (email or "").lower()
    for user in list_users(org_id):
        if user.get("email", "").lower() == needle:
            return user
    return None


def list_users(org_id, role=None):
    """List users in an org, newest first. Optional role filter."""
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    users     = []

    for blob_item in container.list_blobs(name_starts_with=f"{org_id}/"):
        if not blob_item.name.endswith(".json"):
            continue
        try:
            blob = container.get_blob_client(blob_item.name)
            data = blob.download_blob().readall()
            user = json.loads(data.decode("utf-8"))
            if role is not None and user.get("role") != role:
                continue
            users.append(user)
        except Exception:
            pass   # skip malformed blobs

    users.sort(key=lambda u: u.get("updated_at", ""), reverse=True)
    return users


def update_user(org_id, user_id, updates):
    """Apply allowlisted updates and return the updated dict.

    Allowed fields: email, role, password_hash (see _UPDATE_ALLOWED_FIELDS).
    Anything else raises ValueError — including user_id, org_id, created_at,
    and accessible_kb_ids (grants go through assign/revoke helpers).
    Email rename — strict case-insensitive collision check within the org.
    Same email (case-insensitive) is a silent no-op. Role is validated.
    """
    org_id = _resolve_org_id(org_id)
    user   = get_user(org_id, user_id)

    disallowed = set(updates.keys()) - _UPDATE_ALLOWED_FIELDS
    if disallowed:
        raise ValueError(
            f"Cannot update fields: {sorted(disallowed)}. "
            f"Allowed: {sorted(_UPDATE_ALLOWED_FIELDS)}. "
            f"Use assign_kb_access / revoke_kb_access for accessible_kb_ids."
        )

    if "email" in updates:
        new_email = updates["email"]
        if new_email.lower() != user["email"].lower():
            _validate_email(new_email)
            for other in list_users(org_id):
                if other["user_id"] == user_id:
                    continue
                if other.get("email", "").lower() == new_email.lower():
                    raise ValueError(
                        f"email '{new_email}' is already registered in org '{org_id}'"
                    )

    if "role" in updates:
        _validate_role(updates["role"])

    user.update(updates)
    _save_user(user)
    return user


def delete_user(org_id, user_id):
    """Hard-delete a user blob. Returns True on success, False if not found.

    No cascade — sessions and KBs do not yet reference user_id. Revisit when
    ownership tracking gets added.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    try:
        container.delete_blob(f"{org_id}/{user_id}.json")
        return True
    except ResourceNotFoundError:
        return False


# ── KB access grants ──────────────────────────────────────────
def assign_kb_access(org_id, user_id, kb_id):
    """Grant a user access to a KB. Idempotent — granting twice is a no-op.

    Cross-module validates that kb_id exists in the same org by calling
    kb_manager.get_kb. That raises FileNotFoundError on typo — we let it
    propagate so main.py's thin wrapper surfaces HTTP 404.
    """
    org_id = _resolve_org_id(org_id)
    user   = get_user(org_id, user_id)
    kb_manager.get_kb(org_id, kb_id)   # existence check; raises if missing

    if kb_id in user["accessible_kb_ids"]:
        return user   # already granted — no write
    user["accessible_kb_ids"].append(kb_id)
    _save_user(user)
    return user


def revoke_kb_access(org_id, user_id, kb_id):
    """Remove a user's access to a KB. Idempotent on miss."""
    org_id = _resolve_org_id(org_id)
    user   = get_user(org_id, user_id)

    if kb_id not in user["accessible_kb_ids"]:
        return user   # not granted — no write
    user["accessible_kb_ids"] = [k for k in user["accessible_kb_ids"] if k != kb_id]
    _save_user(user)
    return user


# ── Smoke test ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: User Manager (org-scoped, role-validated)")
    print("=" * 55)

    # Two isolated throwaway orgs to prove cross-org independence.
    org_a = f"smoketest-a-{uuid.uuid4().hex[:6]}"
    org_b = f"smoketest-b-{uuid.uuid4().hex[:6]}"

    # Supplementary KB to exercise assign_kb_access's cross-module check.
    kb_id = kb_manager.create_kb(org_a, f"UserSmoketest_KB_{uuid.uuid4().hex[:4]}", "admin-seed")

    print(f"\n── Creating three users under '{org_a}' (admin, analyst, analyst)")
    uid_admin    = create_user(org_a, "Admin.Alice@acme.corp", ROLE_ADMIN)
    uid_analyst1 = create_user(org_a, "bob@acme.corp", ROLE_ANALYST)
    uid_analyst2 = create_user(org_a, "carol@acme.corp", ROLE_ANALYST)

    print("\n── Duplicate email check (case-insensitive) — should raise")
    try:
        create_user(org_a, "admin.alice@acme.corp", ROLE_ANALYST)   # lowercased collision
        print("  FAIL — duplicate email was accepted")
    except ValueError as e:
        print(f"  OK — rejected: {e}")

    print("\n── Invalid role check — should raise")
    try:
        create_user(org_a, "someone@acme.corp", "wizard")
        print("  FAIL — invalid role accepted")
    except ValueError as e:
        print(f"  OK — rejected: {e}")

    print("\n── Creating one user under '{}' with the SAME email".format(org_b))
    create_user(org_b, "bob@acme.corp", ROLE_ANALYST)
    print("  OK — same email in different org is allowed (per design)")

    print("\n── get_user_by_email (case-insensitive lookup)")
    found = get_user_by_email(org_a, "BOB@acme.corp")
    assert found is not None and found["user_id"] == uid_analyst1
    print(f"  OK — found {found['email']} (stored casing preserved)")
    missing = get_user_by_email(org_a, "nobody@acme.corp")
    assert missing is None
    print("  OK — miss returns None (not an exception)")

    print("\n── list_users with role filter")
    admins   = list_users(org_a, role=ROLE_ADMIN)
    analysts = list_users(org_a, role=ROLE_ANALYST)
    print(f"  admins={len(admins)} (expect 1)   analysts={len(analysts)} (expect 2)")
    assert len(admins) == 1 and len(analysts) == 2

    print("\n── assign_kb_access (twice — second is idempotent no-op)")
    u = assign_kb_access(org_a, uid_analyst1, kb_id)
    assert kb_id in u["accessible_kb_ids"]
    u = assign_kb_access(org_a, uid_analyst1, kb_id)
    assert u["accessible_kb_ids"].count(kb_id) == 1
    print(f"  OK — {u['email']} has {len(u['accessible_kb_ids'])} KB(s) granted")

    print("\n── assign_kb_access with bogus kb_id — should raise FileNotFoundError")
    try:
        assign_kb_access(org_a, uid_analyst1, "bogus123")
        print("  FAIL — missing KB accepted")
    except FileNotFoundError as e:
        print(f"  OK — rejected: {e}")

    print("\n── revoke_kb_access")
    u = revoke_kb_access(org_a, uid_analyst1, kb_id)
    assert kb_id not in u["accessible_kb_ids"]
    print(f"  OK — {u['email']} now has {len(u['accessible_kb_ids'])} KB(s)")

    print("\n── update_user: role change + blocked-field rejection")
    update_user(org_a, uid_analyst2, {"role": ROLE_ADMIN})
    assert get_user(org_a, uid_analyst2)["role"] == ROLE_ADMIN
    print("  OK — promoted analyst → admin")
    try:
        update_user(org_a, uid_analyst2, {"accessible_kb_ids": ["x"]})
        print("  FAIL — blocked field accepted")
    except ValueError as e:
        print(f"  OK — rejected: {e}")

    print("\n── App Support user (stored under 'default' regardless of org arg)")
    uid_support = create_user(org_a, f"support+{uuid.uuid4().hex[:4]}@platform", ROLE_APP_SUPPORT)
    support = get_user(DEFAULT_ORG_ID, uid_support)
    assert support["role"] == ROLE_APP_SUPPORT
    assert support["org_id"] == DEFAULT_ORG_ID
    print(f"  OK — app_support user {uid_support} pinned to org '{DEFAULT_ORG_ID}'")

    print("\n── delete_user + verify gone")
    assert delete_user(org_a, uid_analyst1) is True
    try:
        get_user(org_a, uid_analyst1)
        print("  FAIL — deleted user still readable")
    except FileNotFoundError:
        print("  OK — user is gone")

    print("\n── Teardown")
    # Remove remaining test blobs so re-runs stay clean.
    for org, uid in [
        (org_a, uid_admin), (org_a, uid_analyst2),
        (org_b, get_user_by_email(org_b, "bob@acme.corp")["user_id"]),
        (DEFAULT_ORG_ID, uid_support),
    ]:
        delete_user(org, uid)
    kb_manager.delete_kb(org_a, kb_id)
    print("  OK — smoke-test blobs cleaned up")

    print("\nUser manager smoke test complete.")
