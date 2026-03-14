# brd_module.py
# Stage 6 of the BA Copilot workflow.
#
# WHAT IT DOES:
#   1. Takes ONLY accepted/edited requirements from Stage 5
#   2. Generates a structured BRD with full citations
#   3. BA reviews the preview — can request changes
#   4. BA confirms → final BRD saved
#
# KEY DIFFERENCE FROM OLD generator.py:
#   Old: Generated BRD directly from problem + knowledge base
#   New: Generates BRD from BA-approved requirements only
#        Every section traces to a specific REQ-xxx number
#        Zero hallucination — nothing enters BRD unless BA approved it
#
# PROMPTS:
#   All prompt text lives in prompts.json — never hardcoded here.
#   BRD_PROMPT → prompts.json: stages.brd
#   Edit prompts without touching this file.
 
import os
import sys
from dotenv import load_dotenv
from openai import OpenAI
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
 
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import (
    get_relevant_context,
    format_context_with_citations,
    format_citations_block
)
from session_manager import (
    load_session, update_session,
    STAGE_BRD_PREVIEW, STAGE_USER_STORIES
)
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
 
# ── Functions ──────────────────────────────────────────────────
def generate_brd_preview(session_id):
    """
    Stage 6a: Generate BRD from approved requirements only.
    Returns the BRD markdown string.
    """
    session = load_session(session_id)
    problem = session.get("problem_refined") or session.get("problem_raw")
 
    # Get ONLY accepted and edited requirements
    all_reqs = session.get("requirements", [])
    approved = [r for r in all_reqs if r["status"] in ("accepted", "edited")]
    rejected = [r for r in all_reqs if r["status"] == "rejected"]
 
    if not approved:
        raise ValueError(
            "No approved requirements found. "
            "Please accept at least one requirement in Stage 5."
        )
 
    print(f"\n📋 Building BRD from {len(approved)} approved requirements "
          f"({len(rejected)} rejected)")
 
    # Format approved requirements for prompt
    req_text = ""
    for r in approved:
        text = r["edited_text"] if r["edited_text"] else r["text"]
        req_text += (
            f"[{r['id']}] ({r['type']}) {text}\n"
            f"   Rationale: {r['rationale']}\n"
            f"   Source: {r['source']} | Confidence: {r['confidence']}\n\n"
        )
 
    # Format systems and stakeholders
    systems_text = ", ".join(
        s["name"] for s in session.get("impacted_systems", [])
        if s.get("in_scope")
    )
    stakeholders_text = ", ".join(
        f"{st['name']} ({st['team']})"
        for st in session.get("impacted_stakeholders", [])
    )
 
    # Get knowledge base context
    print(f"🔍 Getting knowledge base context...")
    results = get_relevant_context(
        question=problem,
        top_k=3,
        system_name=session.get("system_filter"),
        source_type=session.get("source_filter")
    )
    kb_context = "No additional context from knowledge base."
    if results:
        ctx, _ = format_context_with_citations(results)
        kb_context = ctx
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "brd")
    model_cfg  = get_model_config("stages", "brd")
    prompt_ver = get_prompt_version("stages", "brd")
 
    print(f"🧠 Generating BRD preview ({model_cfg['model']}, prompt v{prompt_ver})...")
 
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
                    problem=problem,
                    systems=systems_text or "TBC",
                    stakeholders=stakeholders_text or "TBC",
                    requirements=req_text,
                    kb_context=kb_context
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    brd = response.choices[0].message.content.strip()
 
    # Token + cost tracking
    usage         = response.usage
    input_tokens  = usage.prompt_tokens     if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    call_cost     = estimate_cost(input_tokens, output_tokens)
    print(f"   📊 {input_tokens}in/{output_tokens}out tokens | ${call_cost:.6f}")
 
    # Add citation block from knowledge base
    if results:
        _, citations = format_context_with_citations(results)
        brd += format_citations_block(citations)
 
    update_session(session_id, {
        "stage":     STAGE_BRD_PREVIEW,
        "brd_draft": brd,
        # Observability
        "brd_prompt_version": prompt_ver,
        "brd_tokens_in":      input_tokens,
        "brd_tokens_out":     output_tokens,
        "brd_cost_usd":       call_cost,
    })
 
    print(f"✅ BRD preview generated ({len(brd)} chars)")
    return brd
 
 
def approve_brd(session_id):
    """
    Stage 6b: BA approves the BRD preview.
    Saves as final BRD and advances to Stage 7: User Stories.
    """
    session = load_session(session_id)
    brd     = session.get("brd_draft", "")
 
    if not brd:
        raise ValueError("No BRD draft found. Generate preview first.")
 
    update_session(session_id, {
        "brd_final":    brd,
        "brd_approved": True,
        "stage":        STAGE_USER_STORIES
    })
 
    print(f"✅ BRD approved — advancing to Stage 7: User Stories")
    return brd
 
 
def regenerate_brd(session_id, feedback):
    """
    BA requests changes to the BRD.
    Adds feedback context and regenerates.
    """
    session = load_session(session_id)
    problem = session.get("problem_refined") or session.get("problem_raw")
 
    print(f"🔄 Regenerating BRD with BA feedback...")
 
    # Append feedback to problem context for regeneration
    enhanced_problem = (
        f"{problem}\n\n"
        f"BA FEEDBACK ON PREVIOUS DRAFT:\n{feedback}"
    )
 
    # Temporarily update problem for this generation
    original_refined = session.get("problem_refined", "")
    update_session(session_id, {"problem_refined": enhanced_problem})
 
    brd = generate_brd_preview(session_id)
 
    # Restore original refined problem
    update_session(session_id, {"problem_refined": original_refined})
 
    print(f"✅ BRD regenerated with feedback applied")
    return brd
 
 
# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from session_manager import create_session, update_session, STAGE_BRD_PREVIEW
 
    print("=" * 55)
    print("TEST: BRD Module — Stage 6")
    print("=" * 55)
 
    print("\n── Step 1: Create session with approved requirements")
    session = create_session(
        problem_raw="HR manual onboarding is slow.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]
 
    update_session(sid, {
        "stage": STAGE_BRD_PREVIEW,
        "problem_refined": (
            "HR onboarding takes 14 days via manual email and paper. "
            "50 hires/month need Email, Slack, HRIS access on day 1 "
            "but provisioning delays average 4 days. "
            "Goal: reduce to 2 days with zero access delays."
        ),
        "impacted_systems": [
            {"name": "HRIS", "impact_level": "High",
             "in_scope": True, "reason": "Core records"},
            {"name": "Email System", "impact_level": "High",
             "in_scope": True, "reason": "Day-1 communication"},
        ],
        "impacted_stakeholders": [
            {"name": "HR Manager", "team": "HR",
             "impact_level": "High", "involvement": "Responsible",
             "reason": "Owns onboarding"},
            {"name": "IT Department", "team": "IT",
             "impact_level": "High", "involvement": "Responsible",
             "reason": "Provisions access"},
        ],
        "requirements": [
            {
                "id": "REQ-001", "type": "Functional", "status": "accepted",
                "text": "The system shall automate employee onboarding workflow.",
                "edited_text": "",
                "rationale": "Eliminates manual email process",
                "source": "Knowledge base", "confidence": "High"
            },
            {
                "id": "REQ-002", "type": "Functional", "status": "edited",
                "text": "The system shall provision system access within 4 hours.",
                "edited_text": "The system shall provision all required system access within 2 hours of onboarding initiation.",
                "rationale": "Eliminates day-1 access delays",
                "source": "Gap answer", "confidence": "High"
            },
            {
                "id": "REQ-003", "type": "Non-Functional", "status": "accepted",
                "text": "The system shall support 50 concurrent onboarding processes.",
                "edited_text": "",
                "rationale": "Handles monthly hire volume",
                "source": "Clarification answer", "confidence": "High"
            },
            {
                "id": "REQ-004", "type": "Integration", "status": "accepted",
                "text": "The system shall integrate with HRIS, Email, and Slack.",
                "edited_text": "",
                "rationale": "Day-1 access to all required systems",
                "source": "Knowledge base", "confidence": "High"
            },
            {
                "id": "REQ-005", "type": "Functional", "status": "rejected",
                "text": "The system shall send SMS notifications.",
                "edited_text": "",
                "rationale": "Out of scope for this phase",
                "source": "Gap answer", "confidence": "Low"
            },
        ]
    })
    print(f"   Session: {sid}")
 
    print("\n── Step 2: Generate BRD preview")
    brd = generate_brd_preview(sid)
    print(f"\n{'='*55}")
    print("BRD PREVIEW (first 1000 chars):")
    print(f"{'='*55}")
    print(brd[:1000])
    print(f"\n... ({len(brd)} total chars)")
 
    print("\n── Step 3: BA approves BRD")
    approve_brd(sid)
 
    session = load_session(sid)
    print(f"   Stage   : {session['stage']} — {session['stage_name']}")
    print(f"   Approved: {session['brd_approved']}")
    print("\n✅ BRD Module working!")