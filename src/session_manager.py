# session_manager.py
# The foundation of the entire 8-stage BA workflow.
# Stores the complete state of each analysis session on disk.
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

SESSIONS_DIR = "sessions"

# ── Stage Constants ────────────────────────────────────────────
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


def _ensure_dir():
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def _session_path(session_id):
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


def _save_session(session):
    session["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(_session_path(session["session_id"]), "w", encoding="utf-8") as f:
        json.dump(session, f, indent=2)


# ── Create ─────────────────────────────────────────────────────
def create_session(problem_raw, system_name=None, source_type=None):
    """Create a new analysis session. Called when BA submits problem."""
    _ensure_dir()

    session = {
        "session_id":   str(uuid.uuid4())[:8],
        "org_id":       "default",       # Phase 3: set from Azure AD login
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
    """Load a session by ID."""
    path = _session_path(session_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Session '{session_id}' not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_session(session_id, updates):
    """Update specific fields in a session."""
    session = load_session(session_id)
    session.update(updates)
    if "stage" in updates:
        session["stage_name"] = STAGE_NAMES.get(updates["stage"], "Unknown")
    _save_session(session)
    return session


def advance_stage(session_id):
    """Move session to the next stage."""
    session = load_session(session_id)
    current = session["stage"]
    if current >= STAGE_COMPLETE:
        return session
    return update_session(session_id, {"stage": current + 1})


# ── List / Delete ──────────────────────────────────────────────
def list_sessions():
    """Return all sessions, newest first."""
    _ensure_dir()
    sessions = []
    for filename in os.listdir(SESSIONS_DIR):
        if filename.endswith(".json"):
            try:
                with open(os.path.join(SESSIONS_DIR, filename)) as f:
                    s = json.load(f)
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
    path = _session_path(session_id)
    if os.path.exists(path):
        os.remove(path)
        return {"success": True}
    return {"success": False, "message": "Session not found"}


def get_session_summary(session_id):
    """Return a human-readable progress summary."""
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


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Session Manager")
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

    print("\n✅ Session Manager working!")