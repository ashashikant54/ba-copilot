# main.py
# FastAPI web server for BA Copilot Phase 1.
# New endpoints added for:
#   - System management (add/remove systems and sources)
#   - Document upload with hierarchy (system + source)
#   - Document registry (view all indexed documents)
#   - BRD generation (existing, now with system/source filter)

import os
import sys
import shutil
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from generator import generate_brd
from retriever import load_and_index_document
from systems_manager import (
    get_all_systems, add_system, add_source,
    remove_system, remove_source
)
from document_registry import (
    register_document, get_registry_as_tree,
    get_all_documents, delete_document
)

app = FastAPI(title="BA Copilot — Phase 1")

# Ensure uploads folder exists
os.makedirs("uploads", exist_ok=True)

# Serve static files (HTML/CSS/JS)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Data Models ───────────────────────────────────────────────
class BRDRequest(BaseModel):
    problem:     str
    system_name: Optional[str] = None
    source_type: Optional[str] = None


class SystemRequest(BaseModel):
    system_name: str


class SourceRequest(BaseModel):
    system_name: str
    source_type: str


# ── Pages ─────────────────────────────────────────────────────
@app.get("/")
def serve_home():
    return FileResponse("static/index.html")


# ── System Management ─────────────────────────────────────────
@app.get("/systems")
def get_systems():
    """Return all systems and their source types."""
    return get_all_systems()


@app.post("/systems/add")
def create_system(request: SystemRequest):
    """Add a new system."""
    result = add_system(request.system_name.strip())
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.post("/systems/add-source")
def create_source(request: SourceRequest):
    """Add a source type to an existing system."""
    result = add_source(
        request.system_name.strip(),
        request.source_type.strip()
    )
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.delete("/systems/{system_name}")
def delete_system(system_name: str):
    """Remove a system."""
    result = remove_system(system_name)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["message"])
    return result


# ── Document Upload ───────────────────────────────────────────
@app.post("/upload")
async def upload_document(
    file:        UploadFile = File(...),
    system_name: str        = Form(...),
    source_type: str        = Form(...)
):
    """
    Upload a document and index it into Azure AI Search.

    Steps:
    1. Save uploaded file to uploads/ folder
    2. Load and extract text
    3. Chunk + embed + store in Azure AI Search
    4. Register in document_registry.json
    5. Return success with chunk count
    """
    # Validate file type
    allowed = [".txt", ".docx", ".pdf"]
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext}' not supported. Use: {allowed}"
        )

    # Validate system + source exist
    systems = get_all_systems()
    if system_name not in systems:
        raise HTTPException(
            status_code=400,
            detail=f"System '{system_name}' not found. Add it first."
        )
    if source_type not in systems[system_name]:
        raise HTTPException(
            status_code=400,
            detail=f"Source '{source_type}' not found in '{system_name}'."
        )

    # Save file to uploads folder
    file_path = f"uploads/{file.filename}"
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    file_size_kb = round(os.path.getsize(file_path) / 1024, 1)

    try:
        # Index into Azure AI Search
        chunks = load_and_index_document(
            file_path=file_path,
            system_name=system_name,
            source_type=source_type
        )

        # Register in document registry
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
        # Clean up uploaded file if indexing failed
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(status_code=500, detail=str(e))


# ── Document Registry ─────────────────────────────────────────
@app.get("/documents")
def get_documents():
    """Return all documents as a hierarchy tree."""
    return get_registry_as_tree()


@app.get("/documents/list")
def list_documents():
    """Return flat list of all documents."""
    return get_all_documents()


@app.delete("/documents/{doc_id}")
def remove_document(doc_id: str):
    """Remove a document from the registry."""
    result = delete_document(doc_id)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=result["message"])
    return result


# ── BRD Generation ────────────────────────────────────────────
@app.post("/generate")
def generate(request: BRDRequest):
    """Generate a BRD from a problem statement."""
    if not request.problem.strip():
        raise HTTPException(status_code=400, detail="Please enter a business problem")

    if len(request.problem.strip()) < 20:
        raise HTTPException(
            status_code=400,
            detail="Please describe your problem in more detail"
        )

    try:
        brd = generate_brd(
            problem_statement=request.problem,
            system_name=request.system_name,
            source_type=request.source_type
        )
        return {"brd": brd, "status": "success"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Health Check ──────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "BA Copilot Phase 1 running ✅"}