# session_manager.py
# UPDATED FOR AZURE CLOUD DEPLOYMENT + PHASE 2 MULTI-TENANCY (Sprint 1)
#
# WHAT CHANGED FROM THE ORIGINAL:
#   - Sessions are no longer saved as files on your laptop/server's hard drive
#   - Sessions are now saved to Azure Blob Storage (cloud storage)
#   - Phase 2 Sprint 1: every session operation is org_id aware
#
# PHASE 2 BLOB PATH CONTRACT:
#   Sprint 1 moves session blobs under an org prefix:
#     BEFORE:  sessions/{session_id}.json
#     AFTER:   sessions/{org_id}/{session_id}.json
#   Legacy root-path blobs remain readable via a read-through fallback in
#   load_session() — marked with LEGACY FALLBACK comments so it can be removed
#   in a future sprint once all pilot blobs live under their org prefix.
#
# org_id DEFAULTS:
#   Every public function accepts org_id with a default of "default". That
#   preserves Phase 1 behaviour exactly — callers that don't pass org_id
#   (i.e. main.py today, before JWT middleware lands in Sprint 3) behave as
#   they always did. DEV_MODE=true in .env is the explicit pilot flag; the
#   resolver below is the single seam where future auth will inject the
#   authenticated user's org_id.
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

from dotenv import load_dotenv
load_dotenv()   # match codebase convention — every module loads .env at import time

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

DEFAULT_ORG_ID = "default"


# ── HELPER: Resolve org_id (Phase 2 multi-tenancy seam) ───────
# Today this always returns "default" — Phase 1 behaviour is preserved.
# When JWT middleware lands in Sprint 3 (A8.6), the None branch below
# becomes "raise 401 unless DEV_MODE". Every function in this module routes
# through here, so the auth change will be a one-line edit.
def _resolve_org_id(org_id):
    """Return the effective org_id. Defaults to 'default' in pilot / DEV_MODE."""
    if org_id:
        return org_id
    # DEV_MODE=true bypasses auth and pins to the default org (see CLAUDE.md A8 / rule 5)
    if os.environ.get("DEV_MODE", "").lower() == "true":
        return DEFAULT_ORG_ID
    return DEFAULT_ORG_ID


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
# Sprint 1: writes now go under sessions/{org_id}/{session_id}.json.
# org_id is read from the session dict (every session has one — either set by
# create_session or preserved from a Phase 1 legacy blob loaded via fallback).
def _save_session(session):
    """Save session data to Azure Blob Storage under its org_id prefix."""
    session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    org_id    = _resolve_org_id(session.get("org_id"))
    session["org_id"] = org_id   # normalise back onto the dict in case it was missing
    blob_name = f"{org_id}/{session['session_id']}.json"
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


# ── Create ─────────────────────────────────────────────────────
def create_session(problem_raw, system_name=None, source_type=None, org_id=None):
    """Create a new analysis session. Called when BA submits problem.

    org_id defaults to 'default' via _resolve_org_id. In Sprint 3 this
    parameter will be populated by the JWT middleware from the authenticated
    user's claims; for now callers can omit it and Phase 1 behaviour holds.
    """
    org_id = _resolve_org_id(org_id)
    session = {
        "session_id":   str(uuid.uuid4())[:8],
        "org_id":       org_id,
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
def load_session(session_id, org_id=None):
    """Load a session by ID from Azure Blob Storage.

    Primary path:  sessions/{org_id}/{session_id}.json
    LEGACY FALLBACK: sessions/{session_id}.json  (Phase 1 root-path blobs)

    Option A migration per Sprint 1 — read-through fallback only, no rewrite.
    Legacy blobs stay at root until their session is updated through normal use,
    at which point _save_session() writes them under the org prefix. This block
    is marked LEGACY FALLBACK so it can be deleted in a future sprint once
    pilot blobs are confirmed migrated.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()

    # Primary path — Sprint 1 contract
    try:
        blob = container.get_blob_client(f"{org_id}/{session_id}.json")
        data = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except ResourceNotFoundError:
        pass   # fall through to legacy lookup

    # ── LEGACY FALLBACK (remove once all Phase 1 blobs are migrated) ──
    try:
        blob = container.get_blob_client(f"{session_id}.json")
        data = blob.download_blob().readall()
        session = json.loads(data.decode("utf-8"))
        # Stamp org_id in memory so a subsequent _save_session writes to the new path.
        session.setdefault("org_id", DEFAULT_ORG_ID)
        return session
    except ResourceNotFoundError:
        raise FileNotFoundError(
            f"Session '{session_id}' not found in Azure Blob Storage "
            f"(checked {org_id}/{session_id}.json and legacy {session_id}.json)"
        )


def update_session(session_id, updates, org_id=None):
    """Update specific fields in a session."""
    session = load_session(session_id, org_id=org_id)
    session.update(updates)
    if "stage" in updates:
        session["stage_name"] = STAGE_NAMES.get(updates["stage"], "Unknown")
    _save_session(session)
    return session


def advance_stage(session_id, org_id=None):
    """Move session to the next stage."""
    session = load_session(session_id, org_id=org_id)
    current = session["stage"]
    if current >= STAGE_COMPLETE:
        return session
    return update_session(session_id, {"stage": current + 1}, org_id=session.get("org_id"))


# ── List / Delete ──────────────────────────────────────────────
def list_sessions(org_id=None):
    """Return all sessions for an org from Azure Blob Storage, newest first.

    Scoping is enforced at the blob list level via name_starts_with so no
    cross-org blobs are even downloaded. Legacy root-path blobs (Phase 1)
    are also included when org_id resolves to 'default', to keep the pilot's
    existing sessions visible while Option A migration is in force.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    sessions  = []
    seen_ids  = set()

    def _accumulate(blob_name):
        try:
            blob = container.get_blob_client(blob_name)
            data = blob.download_blob().readall()
            s = json.loads(data.decode("utf-8"))
            if s["session_id"] in seen_ids:
                return   # org-prefixed copy already captured
            seen_ids.add(s["session_id"])
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

    # Primary scope — sessions/{org_id}/*.json
    for blob_item in container.list_blobs(name_starts_with=f"{org_id}/"):
        if blob_item.name.endswith(".json"):
            _accumulate(blob_item.name)

    # ── LEGACY FALLBACK (remove once all Phase 1 blobs are migrated) ──
    # Only the default org inherits Phase 1 root-path blobs.
    if org_id == DEFAULT_ORG_ID:
        for blob_item in container.list_blobs():
            if blob_item.name.endswith(".json") and "/" not in blob_item.name:
                _accumulate(blob_item.name)

    sessions.sort(key=lambda x: x["updated_at"], reverse=True)
    return sessions


def delete_session(session_id, org_id=None):
    """Delete a session from Azure Blob Storage.

    Tries the org-prefixed path first; falls back to the legacy root path so
    Phase 1 blobs can still be cleaned up from the admin UI.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    try:
        container.delete_blob(f"{org_id}/{session_id}.json")
        return {"success": True}
    except ResourceNotFoundError:
        pass

    # ── LEGACY FALLBACK (remove once all Phase 1 blobs are migrated) ──
    try:
        container.delete_blob(f"{session_id}.json")
        return {"success": True}
    except ResourceNotFoundError:
        return {"success": False, "message": "Session not found"}


def get_session_summary(session_id, org_id=None):
    """Return a human-readable progress summary."""
    session = load_session(session_id, org_id=org_id)
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
def revert_session(session_id: str, target_stage: int, org_id=None) -> dict:
    """Revert session to a previous stage, clearing all data after it."""
    session = load_session(session_id, org_id=org_id)

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
