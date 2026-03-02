# requirements_module.py
# Stage 5 of the BA Copilot workflow.
#
# WHAT IT DOES:
#   1. Reads all gathered context — problem, analysis, gap answers
#   2. Extracts discrete numbered business requirements
#   3. Each requirement is tagged with source and confidence
#   4. BA reviews each: Accept | Edit | Reject
#   5. Only accepted/edited requirements go into the BRD
#
# ZERO HALLUCINATION:
#   Every requirement must trace back to either:
#     (a) A retrieved knowledge base chunk, OR
#     (b) A BA answer from Stage 2 or Stage 4
#   Requirements without evidence are flagged, not invented.

import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import get_relevant_context, format_context_with_citations
from session_manager import (
    load_session, update_session,
    STAGE_REQUIREMENTS, STAGE_BRD_PREVIEW
)

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── Prompt: Extract Requirements ───────────────────────────────
REQUIREMENTS_PROMPT = """
You are a senior Business Analyst extracting business requirements.

Extract ALL discrete business requirements from the provided context.
Each requirement must be:
- Specific and testable
- Written in the format: "The system shall..."
- Traced to a source (knowledge base, BA answer, or gap answer)
- Categorised by type

REQUIREMENT TYPES:
  Functional    — what the system must DO
  Non-Functional — performance, security, usability, compliance
  Integration   — how systems must connect and exchange data
  Data          — what data must be captured, stored, or processed
  Process       — business process rules and workflows

STRICT RULES:
- Only extract requirements supported by the provided context
- If a requirement is inferred but not explicit → confidence: "Medium"
- If directly stated in context → confidence: "High"
- Never invent requirements not supported by evidence
- Number requirements as REQ-001, REQ-002 etc.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REFINED PROBLEM STATEMENT:
{problem}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPACTED SYSTEMS:
{systems}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPACTED STAKEHOLDERS:
{stakeholders}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXISTING PROCESS AND PAIN POINTS:
{process}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CLARIFICATION ANSWERS (Stage 2):
{clarification_answers}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GAP ANSWERS (Stage 4):
{gap_answers}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KNOWLEDGE BASE CONTEXT:
{kb_context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond with ONLY a JSON array. No preamble, no markdown.

Format:
[
  {{
    "id": "REQ-001",
    "type": "Functional | Non-Functional | Integration | Data | Process",
    "text": "The system shall...",
    "rationale": "Why this requirement exists",
    "source": "Knowledge base | Clarification answer | Gap answer",
    "confidence": "High | Medium | Low",
    "status": "pending",
    "edited_text": ""
  }}
]
"""


# ── Functions ──────────────────────────────────────────────────
def extract_requirements(session_id):
    """
    Stage 5a: Extract all business requirements from the
    accumulated context across Stages 2, 3 and 4.
    Returns list of requirement objects.
    """
    session = load_session(session_id)
    problem = session.get("problem_refined") or session.get("problem_raw")

    # Format systems
    systems_text = ""
    for s in session.get("impacted_systems", []):
        systems_text += f"- {s['name']} | Impact: {s['impact_level']} | {s['reason']}\n"

    # Format stakeholders
    stakeholders_text = ""
    for st in session.get("impacted_stakeholders", []):
        stakeholders_text += (
            f"- {st['name']} ({st['team']}) | "
            f"{st['involvement']} | {st['reason']}\n"
        )

    # Format process
    process_text = ""
    for p in session.get("existing_process", []):
        process_text += f"Step {p['step']}: {p['action']}\n"
        if p.get("pain_point"):
            process_text += f"  Pain point: {p['pain_point']}\n"

    # Format clarification Q&A
    clarification_text = ""
    questions = session.get("clarifying_questions", [])
    answers   = session.get("clarifying_answers", {})
    for q in questions:
        ans = answers.get(q["id"], "Not answered")
        clarification_text += f"Q: {q['question']}\nA: {ans}\n\n"

    # Format gap Q&A
    gap_text = ""
    gap_questions = session.get("gap_questions", [])
    gap_answers   = session.get("gap_answers", {})
    for q in gap_questions:
        ans = gap_answers.get(q["id"], "Not answered")
        if ans.strip():
            gap_text += f"Q: {q['question']}\nA: {ans}\n\n"

    # Get fresh knowledge base context
    print(f"\n🔍 Searching knowledge base for requirements context...")
    results = get_relevant_context(
        question=problem,
        top_k=5,
        system_name=session.get("system_filter"),
        source_type=session.get("source_filter")
    )
    kb_context = "No relevant documents found."
    if results:
        ctx, _ = format_context_with_citations(results)
        kb_context = ctx

    print(f"🧠 Extracting business requirements...")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior Business Analyst extracting "
                    "precise, testable requirements. "
                    "Respond with valid JSON only — no markdown, "
                    "no explanation, no preamble."
                )
            },
            {
                "role": "user",
                "content": REQUIREMENTS_PROMPT.format(
                    problem=problem,
                    systems=systems_text or "No systems identified",
                    stakeholders=stakeholders_text or "No stakeholders identified",
                    process=process_text or "No process steps mapped",
                    clarification_answers=clarification_text or "None",
                    gap_answers=gap_text or "None",
                    kb_context=kb_context
                )
            }
        ],
        temperature=0.1,
        max_tokens=2500
    )

    raw = response.choices[0].message.content.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    requirements = json.loads(raw.strip())

    # Ensure all have correct default status fields
    for req in requirements:
        req["status"]      = "pending"
        req["edited_text"] = ""

    update_session(session_id, {
        "stage":        STAGE_REQUIREMENTS,
        "requirements": requirements
    })

    print(f"✅ Extracted {len(requirements)} requirements")

    # Show breakdown by type
    types = {}
    for r in requirements:
        t = r.get("type", "Unknown")
        types[t] = types.get(t, 0) + 1
    for t, count in types.items():
        print(f"   {t}: {count}")

    return requirements


def update_requirement_status(session_id, req_id, status, edited_text=""):
    """
    Stage 5b: BA accepts, edits or rejects a single requirement.

    status options:
      "accepted" — BA approves as-is
      "edited"   — BA modified the text (edited_text contains new version)
      "rejected" — BA removes this requirement from the BRD
    """
    session      = load_session(session_id)
    requirements = session.get("requirements", [])

    for req in requirements:
        if req["id"] == req_id:
            req["status"]      = status
            req["edited_text"] = edited_text
            break

    update_session(session_id, {"requirements": requirements})

    label = {
        "accepted": "✅ Accepted",
        "edited":   "✏️  Edited",
        "rejected": "❌ Rejected"
    }.get(status, status)

    print(f"   {label}: {req_id}")
    return requirements


def bulk_update_requirements(session_id, updates):
    """
    Update multiple requirements at once.
    updates = [{"id": "REQ-001", "status": "accepted", "edited_text": ""}]
    """
    session      = load_session(session_id)
    requirements = session.get("requirements", [])

    update_map = {u["id"]: u for u in updates}

    for req in requirements:
        if req["id"] in update_map:
            u = update_map[req["id"]]
            req["status"]      = u.get("status", req["status"])
            req["edited_text"] = u.get("edited_text", req["edited_text"])

    update_session(session_id, {"requirements": requirements})
    print(f"✅ Updated {len(updates)} requirements")
    return requirements


def get_requirements_summary(session_id):
    """Return a count summary of requirement statuses."""
    session      = load_session(session_id)
    requirements = session.get("requirements", [])

    summary = {
        "total":    len(requirements),
        "pending":  sum(1 for r in requirements if r["status"] == "pending"),
        "accepted": sum(1 for r in requirements if r["status"] == "accepted"),
        "edited":   sum(1 for r in requirements if r["status"] == "edited"),
        "rejected": sum(1 for r in requirements if r["status"] == "rejected"),
    }
    summary["reviewed"] = (
        summary["accepted"] +
        summary["edited"] +
        summary["rejected"]
    )
    summary["all_reviewed"] = summary["pending"] == 0
    return summary


def advance_to_brd(session_id):
    """
    BA clicks Generate BRD — advance to Stage 6.
    Only allowed when all requirements have been reviewed.
    """
    summary = get_requirements_summary(session_id)

    if not summary["all_reviewed"]:
        pending = summary["pending"]
        raise ValueError(
            f"{pending} requirement(s) still pending review. "
            f"Please Accept, Edit or Reject all requirements before generating the BRD."
        )

    accepted = summary["accepted"] + summary["edited"]
    if accepted == 0:
        raise ValueError(
            "No requirements accepted. "
            "Please accept at least one requirement to generate a BRD."
        )

    update_session(session_id, {"stage": STAGE_BRD_PREVIEW})
    print(f"✅ All requirements reviewed — advancing to Stage 6: BRD Preview")
    print(f"   Accepted: {summary['accepted']} | "
          f"Edited: {summary['edited']} | "
          f"Rejected: {summary['rejected']}")


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from session_manager import create_session, update_session, STAGE_REQUIREMENTS

    print("=" * 55)
    print("TEST: Requirements Module — Stage 5")
    print("=" * 55)

    # Create session with Stages 2-4 complete
    print("\n── Step 1: Create session with gaps answered")
    session = create_session(
        problem_raw="Our HR department is struggling with manual onboarding.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]

    update_session(sid, {
        "stage": STAGE_REQUIREMENTS,
        "problem_refined": (
            "HR onboarding takes 14 days via manual email and paper. "
            "50 hires/month need Email, Slack, HRIS access on day 1 "
            "but provisioning delays average 4 days. "
            "Goal: reduce to 2 days with zero access delays."
        ),
        "impacted_systems": [
            {"name": "HRIS", "impact_level": "High", "in_scope": True,
             "reason": "Core employee records system", "needs_clarification": False},
            {"name": "Email System", "impact_level": "High", "in_scope": True,
             "reason": "Day 1 communication tool", "needs_clarification": False},
            {"name": "IT Provisioning", "impact_level": "Medium", "in_scope": True,
             "reason": "Manages system access", "needs_clarification": False},
        ],
        "impacted_stakeholders": [
            {"name": "HR Manager", "team": "HR", "impact_level": "High",
             "involvement": "Responsible", "reason": "Owns onboarding process",
             "needs_clarification": False},
            {"name": "IT Department", "team": "IT", "impact_level": "High",
             "involvement": "Responsible", "reason": "Provisions access",
             "needs_clarification": False},
        ],
        "existing_process": [
            {"step": 1, "action": "HR sends welcome email", "actor": "HR Manager",
             "system": "Email", "pain_point": "Manual, no tracking",
             "citation": "Source 1", "needs_clarification": False},
            {"step": 2, "action": "IT receives access request",
             "actor": "IT", "system": "Email",
             "pain_point": "Requests get lost, no SLA",
             "citation": "Source 1", "needs_clarification": False},
        ],
        "clarifying_questions": [
            {"id": "Q1", "question": "What is current onboarding duration?"}
        ],
        "clarifying_answers": {"Q1": "14 days on average"},
        "gap_questions": [
            {"id": "G1", "question": "What is the SLA for IT access provisioning?"},
            {"id": "G2", "question": "Which systems need day-1 access?"},
        ],
        "gap_answers": {
            "G1": "Currently no SLA — target is 4 hours",
            "G2": "Email, Slack, HRIS, VPN"
        }
    })
    print(f"   Session: {sid}")

    # Extract requirements
    print("\n── Step 2: Extract requirements")
    requirements = extract_requirements(sid)

    print(f"\n   Requirements extracted: {len(requirements)}")
    for req in requirements:
        print(f"\n   [{req['id']}] {req['type']} | {req['confidence']} confidence")
        print(f"   {req['text']}")
        print(f"   Source: {req['source']}")

    # BA reviews — accept some, edit one, reject one
    print("\n── Step 3: BA reviews requirements")
    if len(requirements) >= 1:
        update_requirement_status(sid, requirements[0]["id"], "accepted")
    if len(requirements) >= 2:
        update_requirement_status(
            sid, requirements[1]["id"], "edited",
            edited_text="The system shall provision access within 2 hours of onboarding initiation."
        )
    if len(requirements) >= 3:
        update_requirement_status(sid, requirements[2]["id"], "rejected")

    # Accept all remaining pending ones
    session = load_session(sid)
    for req in session["requirements"]:
        if req["status"] == "pending":
            update_requirement_status(sid, req["id"], "accepted")

    # Summary
    print("\n── Step 4: Requirements summary")
    summary = get_requirements_summary(sid)
    print(f"   Total    : {summary['total']}")
    print(f"   Accepted : {summary['accepted']}")
    print(f"   Edited   : {summary['edited']}")
    print(f"   Rejected : {summary['rejected']}")
    print(f"   All reviewed: {summary['all_reviewed']}")

    # Advance to BRD
    print("\n── Step 5: Advance to BRD Preview")
    advance_to_brd(sid)

    session = load_session(sid)
    print(f"   Stage: {session['stage']} — {session['stage_name']}")
    print("\n✅ Requirements Module working!")