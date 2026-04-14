# clarification_module.py
# Stage 2 of the BA Copilot workflow.
#
# WHAT IT DOES:
#   1. Takes the rough problem statement
#   2. Searches the knowledge base for related context
#   3. Identifies what is ambiguous, missing, or unmeasurable
#   4. Generates 3-5 targeted clarifying questions
#   5. When BA answers → rewrites as a measurable problem statement
#
# ZERO HALLUCINATION APPROACH:
#   Questions are grounded in what the knowledge base DOES and
#   DOES NOT contain. Every question states why it is being asked.
#
# PROMPTS:
#   All prompt text lives in prompts.json — never hardcoded here.
#   QUESTION_PROMPT → prompts.json: stages.clarification
#   REFINE_PROMPT   → prompts.json: stages.clarification_refine
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
    STAGE_CLARIFICATION, STAGE_ANALYSIS
)
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
 
# ── Functions ──────────────────────────────────────────────────
def generate_clarifying_questions(session_id, org_id=None):
    """
    Stage 2a: Generate clarifying questions grounded in
    what the knowledge base does and does not contain.
    """
    session = load_session(session_id, org_id=org_id)
    problem = session["problem_raw"]

    print(f"\n🔍 Searching knowledge base for context...")
    results = get_relevant_context(
        question=problem,
        top_k=3,
        system_name=session.get("system_filter"),
        source_type=session.get("source_filter"),
        org_id=org_id,
    )
 
    context = "No relevant documents found in the knowledge base."
    if results:
        ctx, _ = format_context_with_citations(results)
        context = ctx
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "clarification")
    model_cfg  = get_model_config("stages", "clarification")
    prompt_ver = get_prompt_version("stages", "clarification")
 
    print(f"🧠 Generating clarifying questions ({model_cfg['model']}, prompt v{prompt_ver})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"],
        messages=[
            {
                "role": "system",
                "content": prompt_cfg["system"]
            },
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    problem=problem,
                    context=context
                )
            }
        ]
    )
 
    raw = response.choices[0].message.content.strip()
 
    # Token + cost tracking
    usage         = response.usage
    input_tokens  = usage.prompt_tokens     if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    call_cost     = estimate_cost(input_tokens, output_tokens)
    print(f"   📊 {input_tokens}in/{output_tokens}out tokens | ${call_cost:.6f}")
 
    # Strip markdown code fences if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
 
    questions = json.loads(raw.strip())
 
    update_session(session_id, {
        "stage":                STAGE_CLARIFICATION,
        "clarifying_questions": questions,
        "clarifying_answers":   {},
        # Observability
        "clarification_prompt_version": prompt_ver,
        "clarification_tokens_in":      input_tokens,
        "clarification_tokens_out":     output_tokens,
        "clarification_cost_usd":       call_cost,
    }, org_id=org_id)

    print(f"✅ Generated {len(questions)} clarifying questions")
    return questions


def save_answers(session_id, answers, org_id=None):
    """
    Stage 2b: Save BA's answers.
    answers = {"Q1": "answer text", "Q2": "answer text"}
    """
    session  = load_session(session_id, org_id=org_id)
    existing = session.get("clarifying_answers", {})
    existing.update(answers)
    update_session(session_id, {"clarifying_answers": existing}, org_id=org_id)
    print(f"✅ Saved {len(answers)} answer(s)")
    return existing


def refine_problem_statement(session_id, org_id=None):
    """
    Stage 2c: Rewrite the problem as a precise,
    measurable statement using only what the BA provided.
    """
    session   = load_session(session_id, org_id=org_id)
    problem   = session["problem_raw"]
    questions = session.get("clarifying_questions", [])
    answers   = session.get("clarifying_answers", {})
 
    # Build Q&A text block
    qa_pairs = ""
    for q in questions:
        answer = answers.get(q["id"], "Not answered")
        qa_pairs += f"Q ({q['id']}): {q['question']}\nA: {answer}\n\n"
 
    # Get context
    results = get_relevant_context(
        problem, top_k=3,
        system_name=session.get("system_filter"),
        source_type=session.get("source_filter"),
        org_id=org_id,
    )
    context = "No relevant documents found."
    if results:
        ctx, _ = format_context_with_citations(results)
        context = ctx
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "clarification_refine")
    model_cfg  = get_model_config("stages", "clarification_refine")
    prompt_ver = get_prompt_version("stages", "clarification_refine")
 
    print(f"🧠 Refining problem statement ({model_cfg['model']}, prompt v{prompt_ver})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"],
        messages=[
            {
                "role": "system",
                "content": prompt_cfg["system"]
            },
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    problem=problem,
                    qa_pairs=qa_pairs,
                    context=context
                )
            }
        ]
    )
 
    refined = response.choices[0].message.content.strip()
 
    # Token + cost tracking
    usage         = response.usage
    input_tokens  = usage.prompt_tokens     if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    call_cost     = estimate_cost(input_tokens, output_tokens)
    print(f"   📊 {input_tokens}in/{output_tokens}out tokens | ${call_cost:.6f}")
 
    update_session(session_id, {
        "problem_refined":  refined,
        "problem_approved": False,
        # Observability
        "refine_prompt_version": prompt_ver,
        "refine_tokens_in":      input_tokens,
        "refine_tokens_out":     output_tokens,
        "refine_cost_usd":       call_cost,
    }, org_id=org_id)

    print(f"✅ Problem statement refined")
    return refined


def approve_problem(session_id, approved=True, manual_edit=None, org_id=None):
    """
    Stage 2d: BA approves or manually edits the refined problem.
    Advances to Stage 3 when approved.
    """
    updates = {"problem_approved": approved}

    if manual_edit:
        updates["problem_refined"] = manual_edit
        print(f"✅ Problem updated with BA's edit")

    if approved:
        updates["stage"] = STAGE_ANALYSIS
        print(f"✅ Problem approved — advancing to Stage 3: Analysis")

    return update_session(session_id, updates, org_id=org_id)
 
 
# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from session_manager import create_session
 
    print("=" * 55)
    print("TEST: Clarification Module — Full Stage 2 Flow")
    print("=" * 55)
 
    # Step 1: Create session
    print("\n── Step 1: Create session")
    session = create_session(
        problem_raw="Our HR department is struggling with a slow "
                    "manual employee onboarding process. New employees "
                    "often start without proper system access and HR "
                    "spends too much time on administrative tasks.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]
    print(f"   Session ID: {sid}")
 
    # Step 2: Generate questions
    print("\n── Step 2: Generate clarifying questions")
    questions = generate_clarifying_questions(sid)
    print(f"\n   Questions generated:")
    for q in questions:
        print(f"\n   [{q['id']}] {q['question']}")
        print(f"        Why      : {q['why_asking']}")
        print(f"        In docs  : {not q['not_found_in_docs']}")
 
    # Step 3: Simulate BA answering using actual Q ids
    print("\n── Step 3: BA answers questions")
    answers = {q["id"]: f"Sample answer to: {q['question'][:50]}" for q in questions}
    save_answers(sid, answers)
 
    # Step 4: Refine
    print("\n── Step 4: Refine problem statement")
    refined = refine_problem_statement(sid)
    print(f"\n   Refined:\n   {refined}")
 
    # Step 5: Approve
    print("\n── Step 5: BA approves")
    approve_problem(sid, approved=True)
 
    session = load_session(sid)
    print(f"\n   Stage: {session['stage']} — {session['stage_name']}")
    print("\n✅ Clarification Module working!")