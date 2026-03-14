# observability.py
# CoAnalytica — Feature #5: Observability Dashboard
#
# Aggregates token usage, cost, and platform stats across:
#   - All sessions  (9 stage calls × N sessions)
#   - All meetings  (1 AI call per meeting)
#   - Knowledge Base (doc + chunk counts from document_registry)
#   - Prompt versions (which prompt versions are being used)
#
# Called exclusively by /admin/* endpoints in main.py.
# Read-only — never writes to any store.
 
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from session_manager    import list_sessions, load_session
from meeting_module     import list_meetings, load_meeting
from document_registry  import get_all_documents
 
 
# ── Stage map ─────────────────────────────────────────────────
# (session_field_prefix, display_label, category_group)
STAGES = [
    ("clarification", "Clarify Questions",  "analyse"),
    ("refine",        "Refine Statement",   "analyse"),
    ("analysis",      "System Analysis",    "analyse"),
    ("graph",         "System Graph",       "analyse"),
    ("gap",           "Gap Questions",      "analyse"),
    ("clarity",       "Clarity Assess",     "analyse"),
    ("requirements",  "Requirements",       "analyse"),
    ("brd",           "BRD Preview",        "analyse"),
    ("stories",       "User Stories",       "analyse"),
]
 
# ── Helpers ────────────────────────────────────────────────────
def _f(val):
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0
 
def _i(val):
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0
 
 
# ══════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════
 
def get_platform_overview() -> dict:
    sessions_list  = list_sessions()
    total_sessions = len(sessions_list)
    completed      = sum(1 for s in sessions_list if _i(s.get("stage")) == 8)
 
    session_cost    = 0.0
    session_tok_in  = 0
    session_tok_out = 0
 
    for s in sessions_list:
        try:
            sess = load_session(s["session_id"])
            for sk, _, _ in STAGES:
                session_cost    += _f(sess.get(f"{sk}_cost_usd"))
                session_tok_in  += _i(sess.get(f"{sk}_tokens_in"))
                session_tok_out += _i(sess.get(f"{sk}_tokens_out"))
        except Exception:
            pass
 
    meetings_list  = list_meetings()
    total_meetings = len(meetings_list)
    kb_stored      = sum(1 for m in meetings_list if m.get("kb_stored"))
 
    meeting_cost    = 0.0
    meeting_tok_in  = 0
    meeting_tok_out = 0
 
    for m in meetings_list:
        try:
            meeting = load_meeting(m["meeting_id"])
            meeting_cost    += _f(meeting.get("estimated_cost_usd"))
            meeting_tok_in  += _i(meeting.get("input_tokens"))
            meeting_tok_out += _i(meeting.get("output_tokens"))
        except Exception:
            pass
 
    docs         = get_all_documents()
    total_docs   = len(docs)
    total_chunks = sum(_i(d.get("chunks")) for d in docs)
    total_cost   = session_cost + meeting_cost
 
    return {
        "sessions": {
            "total":     total_sessions,
            "completed": completed,
            "active":    total_sessions - completed,
        },
        "meetings": {
            "total":     total_meetings,
            "kb_stored": kb_stored,
        },
        "knowledge_base": {
            "total_docs":   total_docs,
            "total_chunks": total_chunks,
        },
        "cost": {
            "total_usd":        round(total_cost, 6),
            "sessions_usd":     round(session_cost, 6),
            "meetings_usd":     round(meeting_cost, 6),
            "tokens_in_total":  session_tok_in  + meeting_tok_in,
            "tokens_out_total": session_tok_out + meeting_tok_out,
        },
    }
 
 
def get_cost_by_stage() -> list:
    totals = {
        sk: {"label": lbl, "category": cat,
             "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
        for sk, lbl, cat in STAGES
    }
 
    for s in list_sessions():
        try:
            sess = load_session(s["session_id"])
            for sk, _, _ in STAGES:
                tin  = _i(sess.get(f"{sk}_tokens_in"))
                tout = _i(sess.get(f"{sk}_tokens_out"))
                cost = _f(sess.get(f"{sk}_cost_usd"))
                if tin > 0 or tout > 0:
                    totals[sk]["tokens_in"]  += tin
                    totals[sk]["tokens_out"] += tout
                    totals[sk]["cost_usd"]   += cost
                    totals[sk]["calls"]      += 1
        except Exception:
            pass
 
    mtg = {"label": "Meetings", "category": "meetings",
           "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
    for m in list_meetings():
        try:
            meeting = load_meeting(m["meeting_id"])
            tin  = _i(meeting.get("input_tokens"))
            tout = _i(meeting.get("output_tokens"))
            cost = _f(meeting.get("estimated_cost_usd"))
            if tin > 0 or tout > 0:
                mtg["tokens_in"]  += tin
                mtg["tokens_out"] += tout
                mtg["cost_usd"]   += cost
                mtg["calls"]      += 1
        except Exception:
            pass
 
    result = [
        {"stage_key": sk, **v, "cost_usd": round(v["cost_usd"], 6)}
        for sk, v in totals.items()
    ]
    result.append({"stage_key": "meetings", **mtg,
                   "cost_usd": round(mtg["cost_usd"], 6)})
    return result
 
 
def get_session_cost_table() -> list:
    rows = []
    for s in list_sessions():
        try:
            sess       = load_session(s["session_id"])
            total_cost = sum(_f(sess.get(f"{sk}_cost_usd")) for sk, _, _ in STAGES)
            total_in   = sum(_i(sess.get(f"{sk}_tokens_in")) for sk, _, _ in STAGES)
            total_out  = sum(_i(sess.get(f"{sk}_tokens_out")) for sk, _, _ in STAGES)
 
            rows.append({
                "session_id":    sess.get("session_id", ""),
                "session_short": (sess.get("session_id", "")[:8] + "..."),
                "problem":       (sess.get("problem_raw") or "")[:70],
                "stage":         _i(sess.get("stage", 1)),
                "stage_name":    sess.get("stage_name", ""),
                "updated_at":    sess.get("updated_at", ""),
                "total_cost":    round(total_cost, 6),
                "tokens_in":     total_in,
                "tokens_out":    total_out,
                "stage_costs": {
                    sk: round(_f(sess.get(f"{sk}_cost_usd")), 6)
                    for sk, _, _ in STAGES
                },
            })
        except Exception:
            pass
 
    rows.sort(key=lambda x: x["total_cost"], reverse=True)
    return rows
 
 
def get_kb_breakdown() -> list:
    docs      = get_all_documents()
    breakdown: dict = {}
 
    for doc in docs:
        sys_name = doc.get("system_name", "Unknown")
        src_type = doc.get("source_type",  "Unknown")
        breakdown.setdefault(sys_name, {})
        breakdown[sys_name].setdefault(
            src_type, {"docs": 0, "chunks": 0, "size_kb": 0.0})
        breakdown[sys_name][src_type]["docs"]    += 1
        breakdown[sys_name][src_type]["chunks"]  += _i(doc.get("chunks"))
        breakdown[sys_name][src_type]["size_kb"] += _f(doc.get("file_size_kb"))
 
    result = []
    for sys_name, sources in breakdown.items():
        result.append({
            "system":       sys_name,
            "total_docs":   sum(v["docs"]   for v in sources.values()),
            "total_chunks": sum(v["chunks"] for v in sources.values()),
            "sources": [
                {"source":  src,
                 "docs":    vals["docs"],
                 "chunks":  vals["chunks"],
                 "size_kb": round(vals["size_kb"], 1)}
                for src, vals in sources.items()
            ],
        })
    return result
 
 
def get_prompt_versions() -> dict:
    versions: dict = {}
 
    for s in list_sessions()[:50]:
        try:
            sess = load_session(s["session_id"])
            for sk, lbl, _ in STAGES:
                v = sess.get(f"{sk}_prompt_version")
                if v:
                    versions.setdefault(sk, {"label": lbl, "versions": {}})
                    versions[sk]["versions"][v] = \
                        versions[sk]["versions"].get(v, 0) + 1
        except Exception:
            pass
 
    mtg_versions: dict = {}
    for m in list_meetings()[:50]:
        try:
            meeting = load_meeting(m["meeting_id"])
            v = meeting.get("prompt_version")
            if v:
                mtg_versions[v] = mtg_versions.get(v, 0) + 1
        except Exception:
            pass
 
    if mtg_versions:
        versions["meetings"] = {"label": "Meetings", "versions": mtg_versions}
 
    return versions