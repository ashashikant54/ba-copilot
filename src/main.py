# main.py
# BA Copilot — Phase 1 FastAPI Server
#
# TABS:
#   Tab 1: Analyse      — 8-stage workflow
#   Tab 2: Knowledge Base — Systems + Upload + Documents
#   Tab 3: Sessions     — Resume past sessions
#
# API GROUPS:
#   /sessions/*         — workflow session management
#   /stage/*            — 8-stage workflow endpoints
#   /systems/*          — system and source management
#   /upload             — document upload
#   /documents/*        — document registry
#   /health             — health check

import os
import sys
import shutil
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from session_manager import (
    create_session, load_session, list_sessions,
    delete_session, get_session_summary
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

app = FastAPI(title="BA Copilot — Phase 1")
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Request Models ─────────────────────────────────────────────
class CreateSessionRequest(BaseModel):
    problem:     str
    system_name: Optional[str] = None
    source_type: Optional[str] = None

class AnswersRequest(BaseModel):
    answers: dict   # {Q1: "answer", Q2: "answer"}

class ApproveProblemRequest(BaseModel):
    approved:    bool = True
    manual_edit: Optional[str] = None

class FeedbackRequest(BaseModel):
    feedback: str

class RequirementUpdateRequest(BaseModel):
    req_id:      str
    status:      str   # accepted | edited | rejected
    edited_text: Optional[str] = ""

class BulkRequirementsRequest(BaseModel):
    updates: list  # [{id, status, edited_text}]

class GapAnswersRequest(BaseModel):
    answers: dict  # {G1: "answer", G2: "answer"}

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
# SESSION MANAGEMENT
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/create")
def api_create_session(req: CreateSessionRequest):
    """Stage 1 — Create a new analysis session."""
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


# ══════════════════════════════════════════════════════════════
# STAGE 2 — CLARIFICATION
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/clarify/questions")
def api_generate_questions(session_id: str):
    """Generate clarifying questions from the problem statement."""
    try:
        questions = generate_clarifying_questions(session_id)
        return {"questions": questions}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/clarify/answers")
def api_save_answers(session_id: str, req: AnswersRequest):
    """Save BA's answers to clarifying questions."""
    try:
        save_answers(session_id, req.answers)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/clarify/refine")
def api_refine_problem(session_id: str):
    """Refine the problem statement after answers are saved."""
    try:
        refined = refine_problem_statement(session_id)
        return {"problem_refined": refined}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/clarify/approve")
def api_approve_problem(session_id: str, req: ApproveProblemRequest):
    """BA approves or edits the refined problem. Advances to Stage 3."""
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
    """Run system, stakeholder and process analysis."""
    try:
        analysis = run_analysis(session_id)
        return analysis
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/analyse/graph")
def api_generate_graph(session_id: str):
    """Generate Mermaid.js system graph."""
    try:
        graph = generate_system_graph(session_id)
        return {"graph": graph}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/analyse/approve")
def api_approve_analysis(session_id: str):
    """BA approves the analysis. Advances to Stage 4."""
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
    """Generate gap questions from the analysis."""
    try:
        questions = generate_gap_questions(session_id)
        return {"questions": questions}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/gaps/answers")
def api_save_gap_answers(session_id: str, req: GapAnswersRequest):
    """Save BA's answers to gap questions."""
    try:
        save_gap_answers(session_id, req.answers)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/gaps/assess")
def api_assess_clarity(session_id: str):
    """AI assesses clarity level and recommends whether to proceed."""
    try:
        assessment = assess_clarity(session_id)
        return assessment
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/gaps/confirm")
def api_confirm_gaps(session_id: str):
    """BA confirms sufficient clarity. Advances to Stage 5."""
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
    """Extract all business requirements from accumulated context."""
    try:
        requirements = extract_requirements(session_id)
        return {"requirements": requirements}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.put("/sessions/{session_id}/requirements/update")
def api_update_requirement(session_id: str, req: RequirementUpdateRequest):
    """BA accepts, edits or rejects a single requirement."""
    try:
        update_requirement_status(
            session_id,
            req.req_id,
            req.status,
            req.edited_text or ""
        )
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.put("/sessions/{session_id}/requirements/bulk")
def api_bulk_update(session_id: str, req: BulkRequirementsRequest):
    """Update multiple requirements at once."""
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
    """Advance to Stage 6 — all requirements must be reviewed first."""
    try:
        advance_to_brd(session_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════
# STAGE 6 — BRD PREVIEW
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/brd/generate")
def api_generate_brd(session_id: str):
    """Generate BRD from approved requirements only."""
    try:
        brd = generate_brd_preview(session_id)
        return {"brd": brd}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/brd/approve")
def api_approve_brd(session_id: str):
    """BA approves BRD. Advances to Stage 7."""
    try:
        approve_brd(session_id)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/brd/regenerate")
def api_regenerate_brd(session_id: str, req: FeedbackRequest):
    """Regenerate BRD with BA feedback."""
    try:
        brd = regenerate_brd(session_id, req.feedback)
        return {"brd": brd}
    except Exception as e:
        raise HTTPException(500, str(e))


# ══════════════════════════════════════════════════════════════
# STAGE 7 — USER STORIES
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/stories/generate")
def api_generate_stories(session_id: str):
    """Generate ADO-ready user stories from approved BRD."""
    try:
        stories = generate_user_stories(session_id)
        return {"stories": stories}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sessions/{session_id}/stories/csv")
def api_export_csv(session_id: str):
    """Export user stories as CSV for Azure DevOps import."""
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
    """Mark session as fully complete."""
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

    file_path = f"uploads/{file.filename}"
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    file_size_kb = round(os.path.getsize(file_path) / 1024, 1)

    try:
        chunks = load_and_index_document(
            file_path=file_path,
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
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(500, str(e))


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


# ── Health ─────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "BA Copilot Phase 1 running ✅"}