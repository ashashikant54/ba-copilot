# gap_module.py
# Stage 4 of the BA Copilot workflow — Gap Filling with HITL.
#
# WHAT IT DOES:
#   1. Reviews the Stage 3 analysis for gaps and uncertainties
#   2. Generates specific questions per system and stakeholder
#   3. BA answers questions by typing OR uploading new documents
#   4. AI re-analyses with new information
#   5. AI scores clarity (0-100) and suggests when enough to proceed
#   6. BA confirms to advance — Human in the Loop checkpoint
#
# ZERO HALLUCINATION:
#   Every gap question must state:
#     - Which system or stakeholder should answer it
#     - Why it is needed for the BRD
#     - Which document was expected to contain it
#
# PROMPTS:
#   All prompt text lives in prompts.json — never hardcoded here.
#   GAP_PROMPT     → prompts.json: stages.gap_analysis
#   CLARITY_PROMPT → prompts.json: stages.gap_clarity
#   Edit prompts without touching this file.
 
import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
 
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import get_relevant_context, format_context_with_citations
from session_manager import (
    load_session, update_session,
    STAGE_GAP_FILLING, STAGE_REQUIREMENTS
)
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
 
# ── Functions ──────────────────────────────────────────────────
def generate_gap_questions(session_id, org_id=None):
    """
    Stage 4a: Analyse the Stage 3 output and generate
    targeted gap questions. Returns list of question objects.
    """
    session = load_session(session_id, org_id=org_id)
    problem = session.get("problem_refined") or session.get("problem_raw")
 
    # Format systems for prompt
    systems_text = ""
    for s in session.get("impacted_systems", []):
        flag = " [NEEDS CLARIFICATION]" if s.get("needs_clarification") else ""
        systems_text += (
            f"- {s['name']} | {s['impact_level']} impact | "
            f"In scope: {s.get('in_scope', True)}{flag}\n"
            f"  Reason: {s['reason']}\n"
        )
    if not systems_text:
        systems_text = "No systems identified yet"
 
    # Format stakeholders for prompt
    stakeholders_text = ""
    for st in session.get("impacted_stakeholders", []):
        flag = " [NEEDS CLARIFICATION]" if st.get("needs_clarification") else ""
        stakeholders_text += (
            f"- {st['name']} ({st['team']}) | "
            f"{st['impact_level']} impact | "
            f"{st['involvement']}{flag}\n"
        )
    if not stakeholders_text:
        stakeholders_text = "No stakeholders identified yet"
 
    # Format process for prompt
    process_text = ""
    for p in session.get("existing_process", []):
        flag = " [NEEDS CLARIFICATION]" if p.get("needs_clarification") else ""
        process_text += f"Step {p['step']}: {p['action']}{flag}\n"
        if p.get("pain_point"):
            process_text += f"  Pain point: {p['pain_point']}\n"
    if not process_text:
        process_text = "No process steps mapped yet"
 
    # Items flagged as needing clarification
    flagged = []
    for s in session.get("impacted_systems", []):
        if s.get("needs_clarification"):
            flagged.append(f"System: {s['name']}")
    for st in session.get("impacted_stakeholders", []):
        if st.get("needs_clarification"):
            flagged.append(f"Stakeholder: {st['name']}")
    for p in session.get("existing_process", []):
        if p.get("needs_clarification"):
            flagged.append(f"Process step {p['step']}: {p['action']}")
    flagged_text = "\n".join(flagged) if flagged else "None flagged"
 
    # Already asked questions (from Stage 2)
    already_asked = ""
    for q in session.get("clarifying_questions", []):
        already_asked += f"- {q['question']}\n"
    if not already_asked:
        already_asked = "None"
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "gap_analysis")
    model_cfg  = get_model_config("stages", "gap_analysis")
    prompt_ver = get_prompt_version("stages", "gap_analysis")
 
    print(f"🧠 Generating gap questions ({model_cfg['model']}, prompt v{prompt_ver})...")
 
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
                    systems=systems_text,
                    stakeholders=stakeholders_text,
                    process=process_text,
                    flagged=flagged_text,
                    already_asked=already_asked
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
 
    questions = json.loads(raw.strip())
 
    # Initialise all answers as empty
    gap_answers = {q["id"]: "" for q in questions}
 
    update_session(session_id, {
        "stage":         STAGE_GAP_FILLING,
        "gap_questions": questions,
        "gap_answers":   gap_answers,
        # Observability
        "gap_prompt_version": prompt_ver,
        "gap_tokens_in":      input_tokens,
        "gap_tokens_out":     output_tokens,
        "gap_cost_usd":       call_cost,
    }, org_id=org_id)

    print(f"✅ Generated {len(questions)} gap questions")
    return questions


def save_gap_answers(session_id, answers, org_id=None):
    """
    Stage 4b: Save BA's answers to gap questions.
    answers = {"G1": "answer", "G2": "answer"}
    """
    session  = load_session(session_id, org_id=org_id)
    existing = session.get("gap_answers", {})
    existing.update(answers)
    update_session(session_id, {"gap_answers": existing}, org_id=org_id)
 
    answered = sum(1 for v in existing.values() if v.strip())
    total    = len(session.get("gap_questions", []))
    print(f"✅ Answers saved: {answered}/{total} questions answered")
    return existing
 
 
def assess_clarity(session_id, org_id=None):
    """
    Stage 4c: AI assesses how much clarity has been achieved.
    Returns clarity assessment dict with score and recommendation.
    """
    session = load_session(session_id, org_id=org_id)
    problem = session.get("problem_refined") or session.get("problem_raw")
 
    gap_questions = session.get("gap_questions", [])
    gap_answers   = session.get("gap_answers", {})
 
    answered   = {k: v for k, v in gap_answers.items() if v.strip()}
    unanswered = [q for q in gap_questions
                  if not gap_answers.get(q["id"], "").strip()]
 
    # Build answers summary
    answers_summary = ""
    for q in gap_questions:
        ans    = gap_answers.get(q["id"], "")
        status = f"ANSWERED: {ans[:100]}" if ans.strip() else "NOT ANSWERED"
        answers_summary += f"[{q['id']}] {q['question'][:60]}...\n{status}\n\n"
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "gap_clarity")
    model_cfg  = get_model_config("stages", "gap_clarity")
    prompt_ver = get_prompt_version("stages", "gap_clarity")
 
    print(f"🧠 Assessing clarity ({model_cfg['model']}, prompt v{prompt_ver})...")
 
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
                    systems_count=len(session.get("impacted_systems", [])),
                    stakeholders_count=len(session.get("impacted_stakeholders", [])),
                    process_count=len(session.get("existing_process", [])),
                    gaps_total=len(gap_questions),
                    gaps_answered=len(answered),
                    gaps_unanswered=len(unanswered),
                    answers_summary=answers_summary
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
 
    assessment = json.loads(raw.strip())
 
    update_session(session_id, {
        "clarity_score":      assessment.get("clarity_score", 0),
        "clarity_sufficient": assessment.get("sufficient", False),
        # Observability
        "clarity_prompt_version": prompt_ver,
        "clarity_tokens_in":      input_tokens,
        "clarity_tokens_out":     output_tokens,
        "clarity_cost_usd":       call_cost,
    }, org_id=org_id)

    print(f"✅ Clarity score: {assessment.get('clarity_score')}% — "
          f"{assessment.get('recommendation')}")
    return assessment


def confirm_and_advance(session_id, org_id=None):
    """
    Stage 4d: BA confirms they have enough clarity to proceed.
    This is the Human-in-the-Loop checkpoint.
    Advances to Stage 5: Requirements.
    """
    update_session(session_id, {
        "clarity_confirmed": True,
        "stage":             STAGE_REQUIREMENTS
    }, org_id=org_id)
    print(f"✅ BA confirmed clarity — advancing to Stage 5: Requirements")
 
 
# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from session_manager import create_session, update_session, STAGE_GAP_FILLING
 
    print("=" * 55)
    print("TEST: Gap Module — Stage 4")
    print("=" * 55)
 
    print("\n── Step 1: Create session with analysis complete")
    session = create_session(
        problem_raw="Our HR department is struggling with manual onboarding.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]
 
    update_session(sid, {
        "stage": STAGE_GAP_FILLING,
        "problem_refined": (
            "HR onboarding takes 14 days via manual email and paper. "
            "50 hires/month need Email, Slack, HRIS access on day 1 "
            "but provisioning delays average 4 days."
        ),
        "clarifying_questions": [
            {"id": "Q1", "question": "What is current onboarding duration?"}
        ],
        "impacted_systems": [
            {
                "name": "HR Information System (HRIS)",
                "impact_level": "High",
                "in_scope": True,
                "reason": "Core system for employee records",
                "evidence": "New employees require HRIS access on day 1",
                "needs_clarification": False
            },
            {
                "name": "Email System",
                "impact_level": "High",
                "in_scope": True,
                "reason": "Communication system needed from day 1",
                "evidence": "Onboarding currently done via email",
                "needs_clarification": False
            },
            {
                "name": "IT Provisioning System",
                "impact_level": "Medium",
                "in_scope": True,
                "reason": "Provisions access to all systems",
                "evidence": "IT provisions access manually",
                "needs_clarification": True
            }
        ],
        "impacted_stakeholders": [
            {
                "name": "HR Manager",
                "team": "Human Resources",
                "impact_level": "High",
                "involvement": "Responsible",
                "reason": "Owns the onboarding process",
                "needs_clarification": False
            },
            {
                "name": "IT Department",
                "team": "Information Technology",
                "impact_level": "High",
                "involvement": "Responsible",
                "reason": "Manages system access provisioning",
                "needs_clarification": False
            }
        ],
        "existing_process": [
            {
                "step": 1,
                "action": "HR sends welcome email to new employee",
                "actor": "HR Manager",
                "system": "Email System",
                "pain_point": "Manual, no tracking",
                "citation": "Source 1",
                "needs_clarification": False
            },
            {
                "step": 2,
                "action": "IT receives access request via email",
                "actor": "IT Department",
                "system": "Email System",
                "pain_point": "Requests get lost, no SLA",
                "citation": "Source 1",
                "needs_clarification": True
            }
        ]
    })
    print(f"   Session: {sid}")
 
    print("\n── Step 2: Generate gap questions")
    questions = generate_gap_questions(sid)
    print(f"\n   Gap questions:")
    for q in questions:
        print(f"\n   [{q['id']}] {q['question']}")
        print(f"   System      : {q['directed_to_system']}")
        print(f"   Stakeholder : {q['directed_to_stakeholder']}")
        print(f"   Why needed  : {q['why_needed']}")
        print(f"   Priority    : {q['priority']}")
 
    print("\n── Step 3: BA answers gap questions")
    answers = {}
    for q in questions[:3]:
        answers[q["id"]] = f"Sample answer for: {q['question'][:50]}"
    save_gap_answers(sid, answers)
 
    print("\n── Step 4: Assess clarity")
    assessment = assess_clarity(sid)
    print(f"\n   Score          : {assessment['clarity_score']}%")
    print(f"   Sufficient     : {assessment['sufficient']}")
    print(f"   Recommendation : {assessment['recommendation']}")
    print(f"   Reasoning      : {assessment['reasoning'][:100]}...")
    if assessment.get("remaining_risks"):
        print(f"   Risks          :")
        for r in assessment["remaining_risks"]:
            print(f"     - {r}")
    if assessment.get("assumptions_to_note"):
        print(f"   Assumptions    :")
        for a in assessment["assumptions_to_note"]:
            print(f"     - {a}")
 
    print("\n── Step 5: BA confirms — proceed to requirements")
    confirm_and_advance(sid)
 
    session = load_session(sid)
    print(f"\n   Stage: {session['stage']} — {session['stage_name']}")
    print("\n✅ Gap Module working!")