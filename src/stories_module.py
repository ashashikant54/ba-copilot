# stories_module.py
# Stage 7 of the BA Copilot workflow.
#
# WHAT IT DOES:
#   1. Takes the approved final BRD from Stage 6
#   2. Breaks it into ADO-ready user stories
#   3. Each story links back to its parent REQ-xxx
#   4. Stories follow the standard As a / I want / So that format
#   5. Each story includes acceptance criteria
#
# ADO-READY FORMAT:
#   Title, Description, Acceptance Criteria, Story Points estimate,
#   Priority, Tags — ready to paste directly into Azure DevOps
#
# PROMPTS:
#   All prompt text lives in prompts.json — never hardcoded here.
#   STORIES_PROMPT → prompts.json: stages.user_stories
#   Edit prompts without touching this file.
 
import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
 
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from session_manager import (
    load_session, update_session,
    STAGE_USER_STORIES, STAGE_COMPLETE
)
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
 
# ── Functions ──────────────────────────────────────────────────
def generate_user_stories(session_id, org_id=None):
    """
    Stage 7a: Generate ADO-ready user stories from the approved BRD.
    Returns list of user story objects.
    """
    session = load_session(session_id, org_id=org_id)
    brd     = session.get("brd_final") or session.get("brd_draft")
 
    if not brd:
        raise ValueError(
            "No approved BRD found. "
            "Please complete Stage 6 before generating user stories."
        )
 
    # Get only approved requirements
    all_reqs = session.get("requirements", [])
    approved = [r for r in all_reqs if r["status"] in ("accepted", "edited")]
 
    if not approved:
        raise ValueError("No approved requirements found.")
 
    # Format approved requirements for prompt
    req_text = ""
    for r in approved:
        text = r["edited_text"] if r["edited_text"] else r["text"]
        req_text += (
            f"[{r['id']}] ({r['type']}) {text}\n"
            f"   Rationale: {r['rationale']}\n\n"
        )
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "user_stories")
    model_cfg  = get_model_config("stages", "user_stories")
    prompt_ver = get_prompt_version("stages", "user_stories")
 
    print(f"\n🧠 Generating user stories from {len(approved)} approved requirements "
          f"({model_cfg['model']}, prompt v{prompt_ver})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {
                "role": "system",
                "content": prompt_cfg["system"]
            },
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    requirements=req_text,
                    brd=brd[:3000]  # Cap BRD length for token budget
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    raw = response.choices[0].message.content.strip()
 
    # Token + cost tracking
    usage         = response.usage
    input_tokens  = usage.prompt_tokens     if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    call_cost     = estimate_cost(input_tokens, output_tokens)
    print(f"   📊 {input_tokens}in/{output_tokens}out tokens | ${call_cost:.6f}")
 
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
 
    stories = json.loads(raw.strip())
 
    update_session(session_id, {
        "stage":        STAGE_USER_STORIES,
        "user_stories": stories,
        # Observability
        "stories_prompt_version": prompt_ver,
        "stories_tokens_in":      input_tokens,
        "stories_tokens_out":     output_tokens,
        "stories_cost_usd":       call_cost,
    }, org_id=org_id)
 
    print(f"✅ Generated {len(stories)} user stories")
 
    # Show breakdown by priority
    priorities = {}
    for s in stories:
        p = s.get("priority", "Unknown")
        priorities[p] = priorities.get(p, 0) + 1
    for p, count in priorities.items():
        print(f"   {p}: {count} stories")
 
    total_points = sum(s.get("story_points", 0) for s in stories)
    print(f"   Total story points: {total_points}")
 
    return stories
 
 
def get_stories_summary(session_id, org_id=None):
    """Return a summary of generated user stories."""
    session = load_session(session_id, org_id=org_id)
    stories = session.get("user_stories", [])
 
    if not stories:
        return {"total": 0}
 
    by_priority  = {}
    by_req       = {}
    total_points = 0
 
    for s in stories:
        p = s.get("priority", "Unknown")
        by_priority[p] = by_priority.get(p, 0) + 1
 
        req = s.get("parent_req", "Unknown")
        if req not in by_req:
            by_req[req] = []
        by_req[req].append(s["id"])
 
        total_points += s.get("story_points", 0)
 
    return {
        "total":        len(stories),
        "total_points": total_points,
        "by_priority":  by_priority,
        "by_req":       by_req,
    }
 
 
def export_stories_as_csv(session_id, org_id=None):
    """
    Export user stories as CSV string.
    Can be imported directly into Azure DevOps.
    """
    session = load_session(session_id, org_id=org_id)
    stories = session.get("user_stories", [])
 
    if not stories:
        return ""
 
    lines = [
        "ID,Parent Req,Title,As A,I Want,So That,"
        "Acceptance Criteria,Story Points,Priority,Tags,Notes"
    ]
 
    for s in stories:
        criteria = " | ".join(s.get("acceptance_criteria", []))
        tags     = ", ".join(s.get("tags", []))
 
        def esc(text):
            text = str(text).replace('"', '""')
            return f'"{text}"'
 
        lines.append(",".join([
            esc(s.get("id", "")),
            esc(s.get("parent_req", "")),
            esc(s.get("title", "")),
            esc(s.get("as_a", "")),
            esc(s.get("i_want", "")),
            esc(s.get("so_that", "")),
            esc(criteria),
            esc(s.get("story_points", "")),
            esc(s.get("priority", "")),
            esc(tags),
            esc(s.get("notes", "")),
        ]))
 
    return "\n".join(lines)
 
 
def mark_complete(session_id, org_id=None):
    """Mark the session as fully complete."""
    update_session(session_id, {"stage": STAGE_COMPLETE}, org_id=org_id)
    print(f"✅ Session complete — all 8 stages done!")
 
 
# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from session_manager import create_session, update_session, STAGE_USER_STORIES
 
    print("=" * 55)
    print("TEST: Stories Module — Stage 7")
    print("=" * 55)
 
    print("\n── Step 1: Create session with approved BRD")
    session = create_session(
        problem_raw="HR manual onboarding is slow.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]
 
    update_session(sid, {
        "stage": STAGE_USER_STORIES,
        "problem_refined": (
            "HR onboarding takes 14 days via manual email and paper. "
            "50 hires/month need Email, Slack, HRIS access on day 1 "
            "but provisioning delays average 4 days. "
            "Goal: reduce to 2 days with zero access delays."
        ),
        "requirements": [
            {
                "id": "REQ-001", "type": "Functional",
                "status": "accepted", "edited_text": "",
                "text": "The system shall automate the employee onboarding workflow end to end.",
                "rationale": "Eliminates manual email process",
                "source": "Knowledge base", "confidence": "High"
            },
            {
                "id": "REQ-002", "type": "Functional",
                "status": "edited",
                "text": "The system shall provision system access within 4 hours.",
                "edited_text": "The system shall provision all required system access within 2 hours of onboarding initiation.",
                "rationale": "Eliminates day-1 access delays",
                "source": "Gap answer", "confidence": "High"
            },
            {
                "id": "REQ-003", "type": "Non-Functional",
                "status": "accepted", "edited_text": "",
                "text": "The system shall support 50 concurrent onboarding processes.",
                "rationale": "Handles monthly hire volume",
                "source": "Clarification answer", "confidence": "High"
            },
            {
                "id": "REQ-004", "type": "Integration",
                "status": "accepted", "edited_text": "",
                "text": "The system shall integrate with HRIS, Email, and Slack APIs.",
                "rationale": "Day-1 access to all required systems",
                "source": "Knowledge base", "confidence": "High"
            },
        ],
        "brd_final": (
            "# Business Requirements Document\n\n"
            "## 1. EXECUTIVE SUMMARY\n"
            "This project automates the HR employee onboarding workflow "
            "to reduce cycle time from 14 days to 2 days.\n\n"
            "## 6. FUNCTIONAL REQUIREMENTS\n"
            "REQ-001: Automate onboarding workflow end to end.\n"
            "REQ-002: Provision access within 2 hours.\n\n"
            "## 7. NON-FUNCTIONAL REQUIREMENTS\n"
            "REQ-003: Support 50 concurrent onboarding processes.\n\n"
            "## 8. INTEGRATION REQUIREMENTS\n"
            "REQ-004: Integrate with HRIS, Email, and Slack APIs.\n"
        )
    })
    print(f"   Session: {sid}")
 
    print("\n── Step 2: Generate user stories")
    stories = generate_user_stories(sid)
    print(f"\n   Stories generated: {len(stories)}")
    for s in stories:
        print(f"\n   [{s['id']}] → {s['parent_req']}")
        print(f"   Title    : {s['title']}")
        print(f"   As a     : {s['as_a']}")
        print(f"   I want   : {s['i_want']}")
        print(f"   So that  : {s['so_that']}")
        print(f"   Points   : {s['story_points']} | Priority: {s['priority']}")
        print(f"   Tags     : {', '.join(s.get('tags', []))}")
        print(f"   Criteria :")
        for ac in s.get("acceptance_criteria", []):
            print(f"     - {ac}")
 
    print("\n── Step 3: Stories summary")
    summary = get_stories_summary(sid)
    print(f"   Total stories      : {summary['total']}")
    print(f"   Total story points : {summary['total_points']}")
    print(f"   By priority        : {summary['by_priority']}")
 
    print("\n── Step 4: Export as CSV")
    csv = export_stories_as_csv(sid)
    print(f"   CSV lines: {len(csv.splitlines())}")
    print(f"   First line: {csv.splitlines()[0]}")
 
    print("\n── Step 5: Mark session complete")
    mark_complete(sid)
 
    session = load_session(sid)
    print(f"   Stage: {session['stage']} — {session['stage_name']}")
    print("\n✅ Stories Module working!")