# session_manager.py
# UPDATED FOR AZURE CLOUD DEPLOYMENT
# 
# WHAT CHANGED FROM THE ORIGINAL:
#   - Sessions are no longer saved as files on your laptop/server's hard drive
#   - Sessions are now saved to Azure Blob Storage (cloud storage)
#   - Everything else (stage names, functions, logic) stays EXACTLY the same
#
# WHY WE CHANGED THIS:
#   - Azure App Service restarts regularly and wipes local files
#   - Azure Blob Storage keeps files permanently, even after restarts
#
# STAGES:
#   1 → Problem Definition
#   2 → Clarification
#   3 → System & Stakeholder Analysis
#   4 → Gap Filling (HITL)
#   5 → Requirements Review
#   6 → BRD Preview
#   7 → User Stories
#   8 → Complete

import json
import os
import uuid
from datetime import datetime

# ── NEW IMPORTS FOR AZURE BLOB STORAGE ────────────────────────
# These two lines are NEW — they load the Azure Blob Storage library
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

# ── CONFIGURATION ──────────────────────────────────────────────
# OLD CODE used a local folder:
#   SESSIONS_DIR = "sessions"
#
# NEW CODE reads the connection string from environment variable.
# You will set this environment variable in Azure App Service settings.
# On your laptop, you can set it in a .env file (see instructions below).
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
SESSIONS_CONTAINER = "sessions"   # This is the container name in Azure Blob Storage


# ── HELPER: Get the Azure Blob Container ──────────────────────
# This replaces the old _ensure_dir() function.
# Instead of creating a local folder, we connect to Azure Blob Storage.
def _get_container():
    """Connect to Azure Blob Storage and return the sessions container."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError(
            "❌ AZURE_STORAGE_CONNECTION_STRING is not set!\n"
            "  On your laptop: add it to your .env file\n"
            "  On Azure: add it in App Service → Configuration → App Settings"
        )
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container = blob_service.get_container_client(SESSIONS_CONTAINER)
    # Create the container if it doesn't exist yet
    try:
        container.create_container()
    except Exception:
        pass  # Container already exists — that's fine
    return container


# ── HELPER: Save session to Azure ─────────────────────────────
# OLD CODE wrote to a file like: sessions/abc12345.json
# NEW CODE uploads to Azure Blob like: sessions container → abc12345.json
def _save_session(session):
    """Save session data to Azure Blob Storage."""
    session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blob_name = f"{session['session_id']}.json"         # e.g. "abc12345.json"
    data = json.dumps(session, indent=2)                # Convert to JSON text
    container = _get_container()
    container.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,       # overwrite=True means "update if it already exists"
        encoding="utf-8"
    )


# ── Stage Constants (UNCHANGED) ────────────────────────────────
STAGE_PROBLEM        = 1
STAGE_CLARIFICATION  = 2
STAGE_ANALYSIS       = 3
STAGE_GAP_FILLING    = 4
STAGE_REQUIREMENTS   = 5
STAGE_BRD_PREVIEW    = 6
STAGE_USER_STORIES   = 7
STAGE_COMPLETE       = 8

STAGE_NAMES = {
    1: "Problem Definition",
    2: "Clarification",
    3: "System & Stakeholder Analysis",
    4: "Gap Filling",
    5: "Requirements Review",
    6: "BRD Preview",
    7: "User Stories",
    8: "Complete"
}


# ── Create (UNCHANGED LOGIC) ───────────────────────────────────
def create_session(problem_raw, system_name=None, source_type=None):
    """Create a new analysis session. Called when BA submits problem."""
    session = {
        "session_id":   str(uuid.uuid4())[:8],
        "org_id":       "default",
        "created_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stage":        STAGE_PROBLEM,
        "stage_name":   STAGE_NAMES[STAGE_PROBLEM],
        "system_filter": system_name,
        "source_filter": source_type,

        # Stage 1
        "problem_raw":              problem_raw,

        # Stage 2
        "clarifying_questions":     [],
        "clarifying_answers":       {},
        "problem_refined":          "",
        "problem_approved":         False,

        # Stage 3
        "impacted_systems":         [],
        "impacted_stakeholders":    [],
        "existing_process":         [],
        "system_graph":             "",

        # Stage 4
        "gap_questions":            [],
        "gap_answers":              {},
        "gap_documents":            [],
        "clarity_score":            0,
        "clarity_sufficient":       False,
        "clarity_confirmed":        False,

        # Stage 5
        "requirements":             [],

        # Stage 6
        "brd_draft":                "",
        "brd_approved":             False,
        "brd_final":                "",

        # Stage 7
        "user_stories":             [],
    }

    _save_session(session)
    print(f"✅ Session created: {session['session_id']}")
    return session


# ── Read / Write ───────────────────────────────────────────────
def load_session(session_id):
    """Load a session by ID from Azure Blob Storage."""
    # OLD CODE:
    #   with open("sessions/abc12345.json", "r") as f:
    #       return json.load(f)
    #
    # NEW CODE: download from Azure Blob Storage
    try:
        container = _get_container()
        blob = container.get_blob_client(f"{session_id}.json")
        data = blob.download_blob().readall()       # Download the file bytes
        return json.loads(data.decode("utf-8"))     # Convert bytes → JSON
    except ResourceNotFoundError:
        raise FileNotFoundError(f"Session '{session_id}' not found in Azure Blob Storage")


def update_session(session_id, updates):
    """Update specific fields in a session. (UNCHANGED LOGIC)"""
    session = load_session(session_id)
    session.update(updates)
    if "stage" in updates:
        session["stage_name"] = STAGE_NAMES.get(updates["stage"], "Unknown")
    _save_session(session)
    return session


def advance_stage(session_id):
    """Move session to the next stage. (UNCHANGED LOGIC)"""
    session = load_session(session_id)
    current = session["stage"]
    if current >= STAGE_COMPLETE:
        return session
    return update_session(session_id, {"stage": current + 1})


# ── List / Delete ──────────────────────────────────────────────
def list_sessions():
    """Return all sessions from Azure Blob Storage, newest first."""
    # OLD CODE: looped over files in local sessions/ folder
    # NEW CODE: lists blobs in the Azure sessions container
    container = _get_container()
    sessions = []
    for blob_item in container.list_blobs():                # List all .json files
        if blob_item.name.endswith(".json"):
            try:
                blob = container.get_blob_client(blob_item.name)
                data = blob.download_blob().readall()
                s = json.loads(data.decode("utf-8"))
                sessions.append({
                    "session_id":  s["session_id"],
                    "stage":       s["stage"],
                    "stage_name":  s["stage_name"],
                    "created_at":  s["created_at"],
                    "updated_at":  s["updated_at"],
                    "problem_raw": s["problem_raw"][:80] + "..."
                                   if len(s["problem_raw"]) > 80
                                   else s["problem_raw"]
                })
            except Exception:
                pass
    sessions.sort(key=lambda x: x["updated_at"], reverse=True)
    return sessions


def delete_session(session_id):
    """Delete a session from Azure Blob Storage."""
    # OLD CODE: os.remove("sessions/abc12345.json")
    # NEW CODE: delete the blob from Azure
    try:
        container = _get_container()
        container.delete_blob(f"{session_id}.json")
        return {"success": True}
    except ResourceNotFoundError:
        return {"success": False, "message": "Session not found"}


def get_session_summary(session_id):
    """Return a human-readable progress summary. (UNCHANGED LOGIC)"""
    session = load_session(session_id)
    stage   = session["stage"]

    summary = {
        "session_id":      session["session_id"],
        "stage":           stage,
        "stage_name":      STAGE_NAMES[stage],
        "progress_pct":    round((stage / STAGE_COMPLETE) * 100),
        "created_at":      session["created_at"],
        "problem_raw":     session["problem_raw"],
        "problem_refined": session.get("problem_refined", ""),
    }

    if stage >= STAGE_CLARIFICATION:
        q = len(session.get("clarifying_questions", []))
        a = len(session.get("clarifying_answers", {}))
        summary["clarification"] = f"{a}/{q} questions answered"

    if stage >= STAGE_ANALYSIS:
        summary["systems_identified"] = len(session.get("impacted_systems", []))
        summary["stakeholders_identified"] = len(session.get("impacted_stakeholders", []))

    if stage >= STAGE_GAP_FILLING:
        g  = len(session.get("gap_questions", []))
        ga = len(session.get("gap_answers", {}))
        summary["gaps"]          = f"{ga}/{g} gaps answered"
        summary["clarity_score"] = session.get("clarity_score", 0)

    if stage >= STAGE_REQUIREMENTS:
        reqs = session.get("requirements", [])
        summary["requirements"] = {
            "total":    len(reqs),
            "accepted": sum(1 for r in reqs if r["status"] == "accepted"),
            "edited":   sum(1 for r in reqs if r["status"] == "edited"),
            "rejected": sum(1 for r in reqs if r["status"] == "rejected"),
        }

    return summary

# ── Revert Session to Previous Stage ──────────────────────────
def revert_session(session_id: str, target_stage: int) -> dict:
    """Revert session to a previous stage, clearing all data after it."""
    session = load_session(session_id)

    clear_map = {
        2: ['clarifying_questions', 'clarifying_answers', 'problem_refined',
            'problem_approved', 'impacted_systems', 'impacted_stakeholders',
            'existing_process', 'system_graph', 'gap_questions', 'gap_answers',
            'gap_documents', 'clarity_score', 'clarity_sufficient', 'clarity_confirmed',
            'requirements', 'brd_draft', 'brd_approved', 'brd_final', 'user_stories'],
        3: ['impacted_systems', 'impacted_stakeholders', 'existing_process',
            'system_graph', 'gap_questions', 'gap_answers', 'gap_documents',
            'clarity_score', 'clarity_sufficient', 'clarity_confirmed',
            'requirements', 'brd_draft', 'brd_approved', 'brd_final', 'user_stories'],
        4: ['gap_questions', 'gap_answers', 'gap_documents', 'clarity_score',
            'clarity_sufficient', 'clarity_confirmed', 'requirements',
            'brd_draft', 'brd_approved', 'brd_final', 'user_stories'],
        5: ['requirements', 'brd_draft', 'brd_approved', 'brd_final', 'user_stories'],
        6: ['brd_draft', 'brd_approved', 'brd_final', 'user_stories'],
    }

    for field in clear_map.get(target_stage, []):
        session[field] = None

    session['stage']      = target_stage
    session['stage_name'] = STAGE_NAMES.get(target_stage, 'Unknown')

    _save_session(session)
    return session

# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Session Manager (Azure Blob Storage)")
    print("=" * 55)

    session = create_session(
        problem_raw="Our HR department is struggling with a slow "
                    "manual onboarding process using emails and "
                    "paper forms. New employees start without proper access.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]
    print(f"Session ID : {sid}")
    print(f"Stage      : {session['stage']} — {session['stage_name']}")

    print("\n── Advancing to Stage 2 with sample data...")
    update_session(sid, {
        "stage": STAGE_CLARIFICATION,
        "clarifying_questions": [
            {"id": "Q1", "question": "What is current onboarding duration?",
             "why_asking": "Baseline metric", "not_found_in_docs": True},
            {"id": "Q2", "question": "Which systems need day-1 access?",
             "why_asking": "Scope IT integrations", "not_found_in_docs": False},
        ],
        "clarifying_answers": {
            "Q1": "14 days",
            "Q2": "Email, Slack, HRIS"
        },
        "problem_refined": "HR onboarding currently takes 14 days due to "
                           "manual email and paper processes. Goal: reduce to 2 days.",
        "problem_approved": True
    })

    print("\n── Session Summary:")
    summary = get_session_summary(sid)
    for k, v in summary.items():
        print(f"   {k}: {v}")

    print("\n── All Sessions:")
    for s in list_sessions():
        print(f"   [{s['session_id']}] Stage {s['stage']}: {s['stage_name']}")

    print("\n✅ Session Manager working with Azure Blob Storage!")
