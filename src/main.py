# main.py
# BA Copilot — Phase 1 FastAPI Server
# UPDATED FOR AZURE CLOUD DEPLOYMENT
#
# WHAT CHANGED FROM THE ORIGINAL:
#   1. Removed:  import shutil           (no longer saving files to disk)
#   2. Added:    import tempfile         (built-in Python — creates a safe temp file)
#   3. Removed:  os.makedirs("uploads")  (no longer need the uploads folder)
#   4. Updated:  /upload endpoint        (reads file into memory instead of saving to disk)
#
# EVERYTHING ELSE IS 100% UNCHANGED
#
# WHY WE CHANGED THE UPLOAD ENDPOINT:
#   OLD: file arrives → saved to "uploads/file.pdf" on disk → read from disk → index
#   NEW: file arrives → read into memory as bytes → write to temp file → index → auto-delete
#
#   The key difference: temp files are automatically deleted after use.
#   Azure's disk is ephemeral anyway, so we never rely on it for storage.
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
import tempfile                  # NEW: built-in Python library, no install needed
                                 # Creates a temporary file that auto-deletes when done
                                 # REMOVED: import shutil (no longer needed)
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import Optional

# NEW: Load environment variables from .env file on your laptop
# On Azure, these are set in App Service → Configuration → App Settings
from dotenv import load_dotenv
load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

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

# REMOVED: os.makedirs("uploads", exist_ok=True)
# We no longer need the uploads folder — files are handled in memory

app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Request Models (UNCHANGED) ─────────────────────────────────
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


# ── Pages (UNCHANGED) ──────────────────────────────────────────
@app.get("/")
def serve_home():
    return FileResponse("static/index.html")


# ══════════════════════════════════════════════════════════════
# SESSION MANAGEMENT (UNCHANGED)
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
# STAGE 2 — CLARIFICATION (UNCHANGED)
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/clarify/questions")
def api_generate_questions(session_id: str):
    try:
        questions = generate_clarifying_questions(session_id)
        return {"questions": questions}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/clarify/answers")
def api_save_answers(session_id: str, req: AnswersRequest):
    try:
        save_answers(session_id, req.answers)
        return {"success": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sessions/{session_id}/clarify/refine")
def api_refine_problem(session_id: str):
    try:
        refined = refine_problem_statement(session_id)
        return {"problem_refined": refined}
    except Exception as e:
        raise HTTPException(500, str(e))


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
# STAGE 3 — ANALYSIS (UNCHANGED)
# ══════════════════════════════════════════════════════════════
@app.post("/sessions/{session_id}/analyse")
def api_run_analysis(session_id: str):
    try:
        analysis = run_analysis(session_id)
        return analysis
    except Exception as e:
        raise HTTPException(500, str(e))


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
# STAGE 4 — GAP FILLING (UNCHANGED)
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
# STAGE 5 — REQUIREMENTS REVIEW (UNCHANGED)
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


# ══════════════════════════════════════════════════════════════
# STAGE 6 — BRD PREVIEW (UNCHANGED)
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


# ══════════════════════════════════════════════════════════════
# STAGE 7 — USER STORIES (UNCHANGED)
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
# KNOWLEDGE BASE — SYSTEMS (UNCHANGED)
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
# KNOWLEDGE BASE — DOCUMENT UPLOAD  ← THIS IS THE ONLY CHANGED ENDPOINT
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

    # ── CHANGED SECTION ───────────────────────────────────────
    # OLD CODE saved the file to disk permanently:
    #   file_path = f"uploads/{file.filename}"
    #   with open(file_path, "wb") as f:
    #       shutil.copyfileobj(file.file, f)
    #   file_size_kb = round(os.path.getsize(file_path) / 1024, 1)
    #
    # NEW CODE reads the file into memory first, then creates a
    # temporary file that is automatically deleted when we're done.
    #
    # What is a temp file?
    #   A temporary file is like a sticky note — it exists just long
    #   enough for us to use it, then disappears automatically.
    #   This is perfect for Azure where we can't rely on disk storage.

    # Step 1: Read the uploaded file bytes into memory
    file_bytes = await file.read()

    # Step 2: Calculate file size from bytes (1024 bytes = 1 KB)
    file_size_kb = round(len(file_bytes) / 1024, 1)

    # Step 3: Create a temporary file with the correct extension
    # delete=False means we control when it's deleted (we do it in finally block)
    # suffix=ext means the temp file has the right extension e.g. ".pdf"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)

    try:
        # Step 4: Write the bytes to the temp file so retriever can read it
        tmp.write(file_bytes)
        tmp.flush()   # Make sure all bytes are written before we read
        tmp.close()   # Close the file so retriever can open it

        # Step 5: Index the document using temp file path (same as before)
        chunks = load_and_index_document(
            file_path=tmp.name,          # tmp.name = the temp file's path
            system_name=system_name,
            source_type=source_type
        )

        # Step 6: Register in Azure Blob Storage (via updated document_registry.py)
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
        # Step 7: Always delete the temp file when done, whether success or error
        # This is the "auto-cleanup" — like throwing away that sticky note
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
    # ── END OF CHANGED SECTION ────────────────────────────────


# ══════════════════════════════════════════════════════════════
# KNOWLEDGE BASE — DOCUMENT REGISTRY (UNCHANGED)
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


# ── Health (UNCHANGED) ─────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "BA Copilot Phase 1 running ✅"}