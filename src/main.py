# main.py
# CoAnalytica — Phase 1 FastAPI Server
# Updated: Feature #5 Observability Dashboard added
#
# API GROUPS:
#   /sessions/*         — workflow session management
#   /stage/*            — 8-stage workflow endpoints
#   /systems/*          — system and source management
#   /upload             — document upload
#   /documents/*        — document registry
#   /meetings/*         — meeting processing (Feature #4)
#   /admin/*            — observability dashboard (Feature #5)
#   /health             — health check
 
import os
import sys
import tempfile
import bcrypt
import jwt
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional

from dotenv import load_dotenv
load_dotenv()
 
# ── CRITICAL: sys.path must come before ANY src/ module imports ──
# telemetry, semantic_cache, and all other src/ modules require this.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
# ── OpenTelemetry setup ────────────────────────────────────────
from telemetry import setup_telemetry
 
from session_manager import (
    create_session, load_session, list_sessions,
    delete_session, get_session_summary, revert_session
)
from clarification_module import (
    generate_clarifying_questions, save_answers,
    refine_problem_statement, approve_problem
)
from analysis_module import (
    run_analysis, generate_system_graph, approve_analysis
)
from gap_module import (
    generate_gap_questions, save_gap_answers,
    assess_clarity, confirm_and_advance
)
from requirements_module import (
    extract_requirements, update_requirement_status,
    bulk_update_requirements, get_requirements_summary,
    advance_to_brd
)
from requirements_agent import validate_requirements
from brd_review_agent import review_brd
from lg_coordinator import lg_validate_requirements, lg_review_brd, lg_run_both_agents
from eval_runner import run_evaluation, run_ab_test, get_latest_results
from semantic_cache import get_cache_stats, clear_cache
from hallucination_detector import check_requirements_batch, format_qa_context
from brd_module import (
    generate_brd_preview, approve_brd, regenerate_brd
)
from stories_module import (
    generate_user_stories, get_stories_summary,
    export_stories_as_csv, mark_complete
)
from systems_manager import (
    get_all_systems, add_system, add_source,
    remove_system, remove_source
)
from document_registry import (
    register_document, get_registry_as_tree,
    get_all_documents, delete_document
)
from retriever import load_and_index_document

# ── Phase 2 Sprint 4: auth middleware + user model ───────────
from auth_middleware import (
    AuthMiddleware, create_token, decode_token, DEV_MODE_USER,
)
from user_manager import (
    create_user, get_user, get_user_by_email, update_user, list_users,
    ROLE_ANALYST, ROLE_SUBSCRIBER, VALID_ROLES,
)

# ── Lifespan: initialise OTel on startup ─────────────────────
# LangGraph added a context variable warning about asyncio — using
# lifespan to ensure OTel is set up before first request.
from contextlib import asynccontextmanager
 
@asynccontextmanager
async def lifespan(app):
    # Startup
    setup_telemetry()
    # FastAPI auto-instrumentation: every HTTP request gets a span
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        print("✅ FastAPI OTel instrumentation active")
    except Exception as e:
        print(f"ℹ️  FastAPI OTel instrumentation skipped: {e}")
    yield
    # Shutdown (nothing needed for OTel — BatchSpanProcessor flushes on exit)
 
app = FastAPI(title="CoAnalytica — Phase 2", lifespan=lifespan)

# Phase 2 Sprint 4 (A8.7) — install AuthMiddleware BEFORE mounting static
# files so OTel's FastAPIInstrumentor (applied in lifespan) traces the auth
# path too. In DEV_MODE the middleware injects dev-user synthetically.
app.add_middleware(AuthMiddleware)

app.mount("/static", StaticFiles(directory="static"), name="static")
 
 
# ── Request Models ─────────────────────────────────────────────
class CreateSessionRequest(BaseModel):
    problem:     str
    system_name: Optional[str] = None
    source_type: Optional[str] = None
 
class AnswersRequest(BaseModel):
    answers: dict
 
class ApproveProblemRequest(BaseModel):
    approved:    bool = True
    manual_edit: Optional[str] = None
 
class FeedbackRequest(BaseModel):
    feedback: str
 
class RequirementUpdateRequest(BaseModel):
    req_id:      str
    status:      str
    edited_text: Optional[str] = ""
 
class BulkRequirementsRequest(BaseModel):
    updates: list
 
class GapAnswersRequest(BaseModel):
    answers: dict
 
class SystemRequest(BaseModel):
    system_name: str
 
class SourceRequest(BaseModel):
    system_name: str
    source_type: str
 
 
# ── Pages ──────────────────────────────────────────────────────
@app.get("/")
def serve_home():
    return FileResponse("static/index.html")


# ══════════════════════════════════════════════════════════════
# AUTH — Phase 2 Sprint 4 (A8.7)
# ══════════════════════════════════════════════════════════════
# /auth/login and /auth/register are in AUTH_EXCLUDED_PATHS so they work
# without a token. /auth/whoami is gated — its 401 tells the frontend to
# show the login modal, and its 200 hydrates localStorage on page load.
#
# First-user-in-org promotion: per Sprint 4 Q2 decision, if the target org
# has zero users at registration time, the new user is promoted to
# ROLE_SUBSCRIBER regardless of what the caller requested. All subsequent
# signups default to ROLE_ANALYST. Role promotion to admin/super_admin
# happens through a future admin UI (not in this sprint's scope).

MIN_PASSWORD_LENGTH = 8


class RegisterRequest(BaseModel):
    email:    str
    password: str
    org_id:   Optional[str] = None   # defaults to "default" for pilot


class LoginRequest(BaseModel):
    email:    str
    password: str
    org_id:   Optional[str] = None


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _user_public_view(user: dict) -> dict:
    """Shape returned to the frontend — never includes password_hash."""
    return {
        "user_id":           user["user_id"],
        "org_id":            user["org_id"],
        "email":             user["email"],
        "role":              user["role"],
        "accessible_kb_ids": user.get("accessible_kb_ids", []),
    }


@app.post("/auth/register")
def api_auth_register(req: RegisterRequest):
    """Public signup. First user in an org is promoted to subscriber."""
    email = (req.email or "").strip()
    if "@" not in email:
        raise HTTPException(400, "email must be a valid address")
    if len(req.password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"password must be at least {MIN_PASSWORD_LENGTH} characters")

    org_id = (req.org_id or "default").strip() or "default"

    # First-user-in-org promotion. list_users returns [] for a brand-new org.
    is_first = len(list_users(org_id)) == 0
    role = ROLE_SUBSCRIBER if is_first else ROLE_ANALYST

    try:
        user_id = create_user(org_id=org_id, email=email, role=role)
    except ValueError as e:
        raise HTTPException(400, str(e))

    update_user(org_id, user_id, {"password_hash": _hash_password(req.password)})
    user  = get_user(org_id, user_id)
    token = create_token(user_id=user_id, org_id=org_id, role=role)
    return {"token": token, "user": _user_public_view(user)}


@app.post("/auth/login")
def api_auth_login(req: LoginRequest):
    """Issue a JWT for a registered (email, password) pair. Generic 401 on miss."""
    org_id = (req.org_id or "default").strip() or "default"
    user   = get_user_by_email(org_id, (req.email or "").strip())
    if user is None:
        raise HTTPException(401, "invalid credentials")
    if not user.get("password_hash"):
        # Pre-Sprint-4 user with no password set yet, or mid-signup failure.
        raise HTTPException(401, "invalid credentials")
    if not _verify_password(req.password or "", user["password_hash"]):
        raise HTTPException(401, "invalid credentials")

    token = create_token(
        user_id=user["user_id"], org_id=user["org_id"], role=user["role"],
    )
    return {"token": token, "user": _user_public_view(user)}


@app.get("/auth/whoami")
def api_auth_whoami(request: Request):
    """Return the authenticated user's claims (or DEV_MODE_USER).

    In DEV_MODE the middleware injected the synthetic super_admin — we
    still return a real-looking payload so the frontend can hydrate its
    localStorage the same way as a logged-in production user.
    """
    # Middleware guarantees these are present on any non-excluded path.
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        # Defense-in-depth — should never fire because this path isn't excluded.
        raise HTTPException(401, "unauthenticated")

    # DEV_MODE path — return a synthetic view without hitting blob storage.
    if user_id == DEV_MODE_USER["user_id"]:
        return {
            "user": {
                "user_id":           DEV_MODE_USER["user_id"],
                "org_id":            DEV_MODE_USER["org_id"],
                "email":             "dev@localhost",
                "role":              DEV_MODE_USER["role"],
                "accessible_kb_ids": [],
            },
            "dev_mode": True,
        }

    # Real user — lookup preserves public-view shape and never leaks password_hash.
    try:
        user = get_user(request.state.org_id, user_id)
    except FileNotFoundError:
        # Valid token but the user blob is gone — force re-login.
        raise HTTPException(401, "user record not found")
    return {"user": _user_public_view(user), "dev_mode": False}


# ══════════════════════════════════════════════════════════════
# SESSION MANAGEMENT
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/create")
def api_create_session(req: CreateSessionRequest):
    if not req.problem.strip():
        raise HTTPException(400, "Please enter a business problem")
    if len(req.problem.strip()) < 20:
        raise HTTPException(400, "Please describe your problem in more detail")
    session = create_session(
        problem_raw=req.problem,
        system_name=req.system_name,
        source_type=req.source_type
    )
    return session
 
 
@app.get("/sessions")
def api_list_sessions():
    return list_sessions()
 
 
@app.get("/sessions/{session_id}")
def api_get_session(session_id: str):
    try:
        return load_session(session_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Session '{session_id}' not found")
 
 
@app.get("/sessions/{session_id}/summary")
def api_get_session_summary(session_id: str):
    try:
        return get_session_summary(session_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Session '{session_id}' not found")
 
 
@app.delete("/sessions/{session_id}")
def api_delete_session(session_id: str):
    return delete_session(session_id)
 
@app.post("/sessions/{session_id}/revert")
async def api_revert_session(session_id: str, body: dict = Body(...)):
    target_stage = body.get("target_stage")
    if not target_stage or not (2 <= target_stage <= 6):
        raise HTTPException(400, "target_stage must be between 2 and 6")
    try:
        revert_session(session_id, target_stage)
        return {"message": f"Session reverted to stage {target_stage}"}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# STAGE 2 — CLARIFICATION
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/clarify/questions")
def api_generate_questions(session_id: str):
    try:
        questions = generate_clarifying_questions(session_id)
        return {"questions": questions}
    except Exception as e:
        print(f"Admin /admin/costs/by-stage error: {e}")
        return {"error": str(e), "items": [], "data": []}
 
 
@app.post("/sessions/{session_id}/clarify/answers")
def api_save_answers(session_id: str, req: AnswersRequest):
    try:
        save_answers(session_id, req.answers)
        return {"success": True}
    except Exception as e:
        print(f"Admin /admin/kb/breakdown error: {e}")
        return {"error": str(e), "items": [], "data": []}
 
 
@app.post("/sessions/{session_id}/clarify/refine")
def api_refine_problem(session_id: str):
    try:
        refined = refine_problem_statement(session_id)
        return {"problem_refined": refined}
    except Exception as e:
        print(f"Admin /admin/costs/by-session error: {e}")
        return {"error": str(e), "items": [], "data": []}
 
 
@app.post("/sessions/{session_id}/clarify/approve")
def api_approve_problem(session_id: str, req: ApproveProblemRequest):
    try:
        session = approve_problem(
            session_id,
            approved=req.approved,
            manual_edit=req.manual_edit
        )
        return session
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# STAGE 3 — ANALYSIS
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/analyse")
def api_run_analysis(session_id: str):
    try:
        analysis = run_analysis(session_id)
        return analysis
    except Exception as e:
        print(f"Admin /admin/prompts/versions error: {e}")
        return {"error": str(e), "items": [], "data": []}
 
 
@app.post("/sessions/{session_id}/analyse/graph")
def api_generate_graph(session_id: str):
    try:
        graph = generate_system_graph(session_id)
        return {"graph": graph}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/analyse/approve")
def api_approve_analysis(session_id: str):
    try:
        approve_analysis(session_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# STAGE 4 — GAP FILLING
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/gaps/questions")
def api_generate_gaps(session_id: str):
    try:
        questions = generate_gap_questions(session_id)
        return {"questions": questions}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/gaps/answers")
def api_save_gap_answers(session_id: str, req: GapAnswersRequest):
    try:
        save_gap_answers(session_id, req.answers)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/gaps/assess")
def api_assess_clarity(session_id: str):
    try:
        assessment = assess_clarity(session_id)
        return assessment
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/gaps/confirm")
def api_confirm_gaps(session_id: str):
    try:
        confirm_and_advance(session_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# STAGE 5 — REQUIREMENTS REVIEW
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/requirements/extract")
def api_extract_requirements(session_id: str):
    try:
        requirements = extract_requirements(session_id)
        return {"requirements": requirements}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.put("/sessions/{session_id}/requirements/update")
def api_update_requirement(session_id: str, req: RequirementUpdateRequest):
    try:
        update_requirement_status(
            session_id, req.req_id, req.status, req.edited_text or ""
        )
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.put("/sessions/{session_id}/requirements/bulk")
def api_bulk_update(session_id: str, req: BulkRequirementsRequest):
    try:
        bulk_update_requirements(session_id, req.updates)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/sessions/{session_id}/requirements/summary")
def api_requirements_summary(session_id: str):
    return get_requirements_summary(session_id)
 
 
@app.post("/sessions/{session_id}/requirements/advance")
def api_advance_to_brd(session_id: str):
    try:
        advance_to_brd(session_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/requirements/validate")
def api_validate_requirements(session_id: str):
    """
    Feature 7 — Requirements Validation Agent.
    Runs 3-tool observe-plan-act loop:
      Tool 1: KB contradiction search (Python, no GPT)
      Tool 2: BABOK quality check (GPT)
      Tool 3: Meeting decisions cross-reference (GPT)
    Reflection loop: if quality_score < 70 → re-evaluate up to 3x.
    Returns quality score, issues, suggested fixes, meeting conflicts.
    """
    try:
        result = validate_requirements(session_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# STAGE 6 — BRD PREVIEW
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/brd/generate")
def api_generate_brd(session_id: str):
    try:
        brd = generate_brd_preview(session_id)
        return {"brd": brd}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/brd/approve")
def api_approve_brd(session_id: str):
    try:
        approve_brd(session_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/brd/regenerate")
def api_regenerate_brd(session_id: str, req: FeedbackRequest):
    try:
        brd = regenerate_brd(session_id, req.feedback)
        return {"brd": brd}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/brd/review")
def api_review_brd(session_id: str):
    """
    Feature 8 — BRD Review Agent.
    Multi-agent coordination: reads Feature 7 requirements quality score
    and adjusts BRD quality threshold accordingly.
 
    Tool 1: Requirements traceability check (Python, no GPT)
    Tool 2: BRD quality check — 6 BABOK dimensions (GPT)
    Tool 3: Stakeholder alignment vs Stage 3 analysis (GPT)
    Reflection: rewrites weak sections until score >= threshold (max 3x).
 
    Returns quality score, section issues, traceability gaps,
    stakeholder gaps, suggested section rewrites.
    """
    try:
        result = review_brd(session_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# LANGGRAPH AGENTS — Feature 7 + 8 via LangGraph
# ══════════════════════════════════════════════════════════════
 
@app.post("/sessions/{session_id}/requirements/validate/lg")
def api_validate_requirements_lg(session_id: str):
    """LangGraph version of Requirements Validation Agent."""
    try:
        result = lg_validate_requirements(session_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/brd/review/lg")
def api_review_brd_lg(session_id: str):
    """LangGraph version of BRD Review Agent."""
    try:
        result = lg_review_brd(session_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/agents/run-all")
def api_run_all_agents(session_id: str):
    """Run F7 then F8 sequentially in one LangGraph invocation."""
    try:
        result = lg_run_both_agents(session_id)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# STAGE 7 — USER STORIES
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/stories/generate")
def api_generate_stories(session_id: str):
    try:
        stories = generate_user_stories(session_id)
        return {"stories": stories}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/sessions/{session_id}/stories/csv")
def api_export_csv(session_id: str):
    try:
        csv = export_stories_as_csv(session_id)
        return Response(
            content=csv,
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f"attachment; filename=stories_{session_id}.csv"
            }
        )
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/sessions/{session_id}/complete")
def api_mark_complete(session_id: str):
    try:
        mark_complete(session_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# KNOWLEDGE BASE — SYSTEMS
# ══════════════════════════════════════════════════════════════
@app.get("/systems")
def api_get_systems():
    return get_all_systems()
 
 
@app.post("/systems/add")
def api_add_system(req: SystemRequest):
    result = add_system(req.system_name.strip())
    if not result["success"]:
        raise HTTPException(400, result["message"])
    return result
 
 
@app.post("/systems/add-source")
def api_add_source(req: SourceRequest):
    result = add_source(req.system_name.strip(), req.source_type.strip())
    if not result["success"]:
        raise HTTPException(400, result["message"])
    return result
 
 
@app.delete("/systems/{system_name}")
def api_delete_system(system_name: str):
    result = remove_system(system_name)
    if not result["success"]:
        raise HTTPException(404, result["message"])
    return result
 
 
# ══════════════════════════════════════════════════════════════
# KNOWLEDGE BASE — DOCUMENT UPLOAD
# ══════════════════════════════════════════════════════════════
@app.post("/upload")
async def api_upload_document(
    file:        UploadFile = File(...),
    system_name: str        = Form(...),
    source_type: str        = Form(...)
):
    """Upload and index a document into Azure AI Search."""
    allowed = [".txt", ".docx", ".pdf"]
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(400, f"File type '{ext}' not supported. Use: {allowed}")
 
    systems = get_all_systems()
    if system_name not in systems:
        raise HTTPException(400, f"System '{system_name}' not found")
    if source_type not in systems[system_name]:
        raise HTTPException(400, f"Source '{source_type}' not found in '{system_name}'")
 
    file_bytes   = await file.read()
    file_size_kb = round(len(file_bytes) / 1024, 1)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
 
    try:
        tmp.write(file_bytes)
        tmp.flush()
        tmp.close()
 
        chunks = load_and_index_document(
            file_path=tmp.name,
            system_name=system_name,
            source_type=source_type
        )
 
        record = register_document(
            document_name=file.filename,
            system_name=system_name,
            source_type=source_type,
            chunks=chunks,
            file_size_kb=file_size_kb
        )
 
        return {
            "success":  True,
            "message":  f"✅ Indexed {chunks} chunks from '{file.filename}'",
            "document": record
        }
 
    except Exception as e:
        raise HTTPException(500, str(e))
 
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
 
 
# ══════════════════════════════════════════════════════════════
# KNOWLEDGE BASE — DOCUMENT REGISTRY
# ══════════════════════════════════════════════════════════════
@app.get("/documents")
def api_get_documents():
    return get_registry_as_tree()
 
 
@app.get("/documents/list")
def api_list_documents():
    return get_all_documents()
 
 
@app.delete("/documents/{doc_id}")
def api_remove_document(doc_id: str):
    result = delete_document(doc_id)
    if not result["success"]:
        raise HTTPException(404, result["message"])
    return result
 
 
# ══════════════════════════════════════════════════════════════
# MEETINGS — Feature #4
# ══════════════════════════════════════════════════════════════
from meeting_module import (
    process_meeting,
    store_meeting_to_kb,
    load_meeting,
    list_meetings
)
 
class ProcessMeetingRequest(BaseModel):
    title:       str
    system_name: Optional[str] = None
 
class StoreMeetingRequest(BaseModel):
    system_name: str
    source_type: str
 
 
@app.post("/meetings/process")
async def api_process_meeting(
    file:        UploadFile = File(...),
    title:       str        = Form(...),
    system_name: str        = Form("")
):
    allowed = [".txt", ".vtt", ".docx", ".mp4"]
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(400, f"File type '{ext}' not supported. Use: {allowed}")
 
    if not title.strip():
        raise HTTPException(400, "Meeting title is required")
 
    file_bytes   = await file.read()
    file_size_kb = round(len(file_bytes) / 1024, 1)
 
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        tmp.write(file_bytes)
        tmp.flush()
        tmp.close()
 
        meeting = process_meeting(
            title=title.strip(),
            system_name=system_name.strip() if system_name else "",
            file_path=tmp.name,
            filename=file.filename,
            file_size_kb=file_size_kb
        )
 
        return {"success": True, "meeting": meeting}
 
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
 
 
@app.get("/meetings")
def api_list_meetings():
    try:
        return list_meetings()
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/meetings/{meeting_id}")
def api_get_meeting(meeting_id: str):
    try:
        return load_meeting(meeting_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Meeting '{meeting_id}' not found")
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/meetings/{meeting_id}/store")
def api_store_meeting_to_kb(meeting_id: str, req: StoreMeetingRequest):
    if not req.system_name.strip():
        raise HTTPException(400, "system_name is required")
    if not req.source_type.strip():
        raise HTTPException(400, "source_type is required")
 
    systems = get_all_systems()
    if req.system_name not in systems:
        raise HTTPException(400, f"System '{req.system_name}' not found")
    if req.source_type not in systems[req.system_name]:
        raise HTTPException(400, f"Source '{req.source_type}' not found in '{req.system_name}'")
 
    try:
        meeting = store_meeting_to_kb(
            meeting_id=meeting_id,
            system_name=req.system_name.strip(),
            source_type=req.source_type.strip()
        )
        return {
            "success": True,
            "message": f"✅ Meeting indexed into '{req.system_name} → {req.source_type}'",
            "meeting": meeting
        }
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# ADMIN — Feature #5: Observability Dashboard
# ══════════════════════════════════════════════════════════════
from observability import (
    get_platform_overview,
    get_cost_by_stage,
    get_session_cost_table,
    get_kb_breakdown,
    get_prompt_versions,
)
 
 
@app.get("/admin/overview")
def api_admin_overview():
    """High-level platform stats: session/meeting counts, KB size, cumulative cost."""
    try:
        return get_platform_overview()
    except Exception as e:
        # Return partial data rather than crashing to HTML
        print(f"Admin overview error: {e}")
        return {
            "total_sessions": 0,
            "total_meetings": 0,
            "kb_documents": 0,
            "cumulative_cost_usd": 0.0,
            "error": str(e)
        }
 
 
@app.get("/admin/costs/by-stage")
def api_admin_costs_by_stage():
    """Token usage + cost broken down by pipeline stage, aggregated across all sessions."""
    try:
        return get_cost_by_stage()
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/admin/costs/by-session")
def api_admin_costs_by_session():
    """Per-session cost table, sorted by total cost descending."""
    try:
        return get_session_cost_table()
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/admin/kb/breakdown")
def api_admin_kb_breakdown():
    """KB stats broken down by System → Source → doc count, chunk count, size."""
    try:
        return get_kb_breakdown()
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/admin/prompts/versions")
def api_admin_prompt_versions():
    """Which prompt versions are in use across recent sessions + meetings."""
    try:
        return get_prompt_versions()
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# EVALUATION FRAMEWORK — Feature 9
# ══════════════════════════════════════════════════════════════
 
class ABTestRequest(BaseModel):
    stage_key: str
    version_a: str
    version_b: str
    max_cases: int = 8
 
 
@app.post("/eval/run")
def api_run_evaluation(use_llm_judge: bool = False, max_cases: Optional[int] = None):
    """
    Run offline evaluation against the golden requirements dataset.
    use_llm_judge=True adds LLM-as-Judge groundedness check (costs tokens).
    use_llm_judge=False uses lexical overlap only (free, faster).
    """
    try:
        result = run_evaluation(use_llm_judge=use_llm_judge, max_cases=max_cases)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.post("/eval/ab-test")
def api_run_ab_test(req: ABTestRequest):
    """
    Capture baseline metrics for A/B prompt version comparison.
    Run with version_a first, update prompts.json, run again with version_b.
    """
    try:
        result = run_ab_test(
            stage_key=req.stage_key,
            version_a=req.version_a,
            version_b=req.version_b,
            max_cases=req.max_cases
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/eval/results")
def api_get_eval_results():
    """Return the most recent evaluation results from disk."""
    try:
        results = get_latest_results()
        if not results:
            return {"message": "No evaluation results yet. Run POST /eval/run first."}
        return results
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
@app.get("/sessions/{session_id}/eval/hallucination")
def api_get_hallucination_scores(session_id: str):
    """
    Return groundedness scores for all requirements in a session.
    Shows which requirements may contain unsupported claims.
    """
    try:
        session = load_session(session_id)
        stored = session.get("req_groundedness_scores")
        if stored:
            return {
                "session_id":         session_id,
                "hallucination_rate": session.get("req_hallucination_rate", 0),
                "verdict":            session.get("req_hallucination_verdict", "unknown"),
                "warning":            session.get("req_hallucination_warning"),
                "per_requirement":    stored,
                "source":             "stored"
            }
        # Run on-demand if not stored
        requirements = session.get("requirements", [])
        if not requirements:
            raise HTTPException(400, "No requirements found in session")
        from retriever import get_relevant_context, format_context_with_citations
        problem = session.get("problem_refined") or session.get("problem_raw", "")
        results = get_relevant_context(problem, top_k=5,
                                       system_name=session.get("system_filter"),
                                       source_type=session.get("source_filter"))
        kb_ctx = ""
        if results:
            kb_ctx, _ = format_context_with_citations(results)
        qa_ctx = format_qa_context(session)
        hall_result = check_requirements_batch(requirements, kb_ctx, qa_ctx)
        return {
            "session_id":         session_id,
            "hallucination_rate": hall_result["hallucination_rate"],
            "verdict":            hall_result["overall_verdict"],
            "warning":            hall_result["session_warning"],
            "per_requirement":    hall_result["per_requirement"],
            "source":             "computed"
        }
    except FileNotFoundError:
        raise HTTPException(404, f"Session '{session_id}' not found")
    except Exception as e:
        raise HTTPException(500, str(e))
 
 
# ══════════════════════════════════════════════════════════════
# SEMANTIC CACHE — Stats and management
# ══════════════════════════════════════════════════════════════
 
@app.get("/cache/stats")
def api_cache_stats():
    """
    Return live cache statistics for the Admin dashboard.
    Shows hit rate, cost saved, cached entries, threshold.
    """
    return get_cache_stats()
 
 
@app.delete("/cache")
def api_clear_cache():
    """
    Clear all cached BABOK results.
    Call this after a significant prompt version change to
    prevent stale cached results from being returned.
    """
    return clear_cache()
 
 
# ── Health ─────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "CoAnalytica Phase 1 running ✅"}