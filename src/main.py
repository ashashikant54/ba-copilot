# main.py
# This is the web server for BA Copilot.
# It creates two things:
#   1. An API endpoint that generates BRDs
#   2. Serves the HTML page to the browser
#
# How it works:
#   Browser → sends problem statement → FastAPI → generator.py → BRD → Browser

import os
import sys
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from generator import generate_brd
from retriever import load_and_index_document

# Create the FastAPI app
app = FastAPI(title="BA Copilot MVP")


# ── Data Models ───────────────────────────────────────────────
# This defines what data the API expects to receive
class BRDRequest(BaseModel):
    problem: str          # The business problem typed by the user


class BRDResponse(BaseModel):
    brd: str              # The generated BRD
    status: str           # "success" or "error"


# ── API Endpoints ─────────────────────────────────────────────

@app.get("/")
def serve_home():
    """Serve the main HTML page when someone opens the browser."""
    return FileResponse("static/index.html")


@app.post("/generate", response_model=BRDResponse)
def generate(request: BRDRequest):
    """
    Main endpoint — receives a problem statement, returns a BRD.

    The browser sends:  { "problem": "Our HR process is slow..." }
    This returns:       { "brd": "## 1. EXECUTIVE SUMMARY...", "status": "success" }
    """
    # Validate the input isn't empty
    if not request.problem.strip():
        raise HTTPException(
            status_code=400,
            detail="Please enter a business problem"
        )

    if len(request.problem.strip()) < 20:
        raise HTTPException(
            status_code=400,
            detail="Please describe your problem in more detail (at least 20 characters)"
        )

    try:
        print(f"\n🌐 Web request received")
        print(f"   Problem: {request.problem[:80]}...")

        # Call our generator from Step 7
        brd = generate_brd(request.problem)

        return BRDResponse(brd=brd, status="success")

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/index-document")
def index_document(file_path: str):
    """Index a new document into ChromaDB."""
    try:
        load_and_index_document(file_path)
        return {"status": "success", "message": f"Indexed: {file_path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    """Simple check that the server is running."""
    return {"status": "BA Copilot is running ✅"}