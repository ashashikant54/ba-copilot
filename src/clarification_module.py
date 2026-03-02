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

import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import get_relevant_context, format_context_with_citations
from session_manager import (
    load_session, update_session,
    STAGE_CLARIFICATION, STAGE_ANALYSIS
)

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── Prompt: Generate Questions ─────────────────────────────────
QUESTION_PROMPT = """
You are a senior Business Analyst conducting a problem analysis.
Your job is to ask TARGETED clarifying questions before writing any requirements.

You have been given:
1. A rough problem statement from the BA
2. Context retrieved from the knowledge base (existing documents)

INSTRUCTIONS:
- Study what the knowledge base DOES contain about this problem
- Identify what is MISSING, AMBIGUOUS or UNMEASURABLE
- Generate exactly 3-5 clarifying questions
- Each question must be specific and answerable
- Do NOT ask about things already clearly answered in the context
- Focus on: metrics, timelines, scope boundaries, key stakeholders,
  system names, and success criteria

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ROUGH PROBLEM STATEMENT:
{problem}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KNOWLEDGE BASE CONTEXT (what we already know):
{context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond with ONLY a JSON array. No preamble, no explanation, just JSON.

Format:
[
  {{
    "id": "Q1",
    "question": "The specific question text",
    "why_asking": "What this answer will clarify",
    "not_found_in_docs": true
  }},
  {{
    "id": "Q2",
    "question": "...",
    "why_asking": "...",
    "not_found_in_docs": false
  }}
]
"""


# ── Prompt: Refine Problem ─────────────────────────────────────
REFINE_PROMPT = """
You are a senior Business Analyst.
Rewrite the problem statement to be precise, measurable, and unambiguous.

RULES:
- Use ONLY information from the original statement and the BA's answers
- Make it measurable — include numbers, timeframes, volumes where provided
- Structure: Current State → Pain → Impact → Goal
- Maximum 4 sentences
- Never invent facts not provided by the BA

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ORIGINAL PROBLEM:
{problem}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUESTIONS AND ANSWERS:
{qa_pairs}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KNOWLEDGE BASE CONTEXT:
{context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Write ONLY the refined problem statement — no headers, no bullets.
"""


# ── Functions ──────────────────────────────────────────────────
def generate_clarifying_questions(session_id):
    """
    Stage 2a: Generate clarifying questions grounded in
    what the knowledge base does and does not contain.
    """
    session = load_session(session_id)
    problem = session["problem_raw"]

    print(f"\n🔍 Searching knowledge base for context...")
    results = get_relevant_context(
        question=problem,
        top_k=3,
        system_name=session.get("system_filter"),
        source_type=session.get("source_filter")
    )

    context = "No relevant documents found in the knowledge base."
    if results:
        ctx, _ = format_context_with_citations(results)
        context = ctx

    print(f"🧠 Generating clarifying questions...")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a senior Business Analyst. "
                           "Ask precise evidence-based clarifying questions. "
                           "Respond with valid JSON only — no markdown, no explanation."
            },
            {
                "role": "user",
                "content": QUESTION_PROMPT.format(
                    problem=problem,
                    context=context
                )
            }
        ],
        temperature=0.2,
        max_tokens=800
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    questions = json.loads(raw.strip())

    update_session(session_id, {
        "stage":                STAGE_CLARIFICATION,
        "clarifying_questions": questions,
        "clarifying_answers":   {}
    })

    print(f"✅ Generated {len(questions)} clarifying questions")
    return questions


def save_answers(session_id, answers):
    """
    Stage 2b: Save BA's answers.
    answers = {"Q1": "answer text", "Q2": "answer text"}
    """
    session  = load_session(session_id)
    existing = session.get("clarifying_answers", {})
    existing.update(answers)
    update_session(session_id, {"clarifying_answers": existing})
    print(f"✅ Saved {len(answers)} answer(s)")
    return existing


def refine_problem_statement(session_id):
    """
    Stage 2c: Rewrite the problem as a precise,
    measurable statement using only what the BA provided.
    """
    session   = load_session(session_id)
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
        source_type=session.get("source_filter")
    )
    context = "No relevant documents found."
    if results:
        ctx, _ = format_context_with_citations(results)
        context = ctx

    print(f"🧠 Refining problem statement...")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a senior Business Analyst. "
                           "Write precise measurable problem statements. "
                           "Never invent facts not provided."
            },
            {
                "role": "user",
                "content": REFINE_PROMPT.format(
                    problem=problem,
                    qa_pairs=qa_pairs,
                    context=context
                )
            }
        ],
        temperature=0.1,
        max_tokens=400
    )

    refined = response.choices[0].message.content.strip()

    update_session(session_id, {
        "problem_refined": refined,
        "problem_approved": False
    })

    print(f"✅ Problem statement refined")
    return refined


def approve_problem(session_id, approved=True, manual_edit=None):
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

    return update_session(session_id, updates)


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