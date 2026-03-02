# analysis_module.py
# Stage 3 of the BA Copilot workflow.
#
# WHAT IT DOES:
#   1. Reads the refined problem statement
#   2. Searches knowledge base for evidence
#   3. Identifies impacted systems WITH reasoning and evidence
#   4. Identifies stakeholders WITH impact level and involvement
#   5. Maps the existing E2E process from documents only
#   6. Generates a Mermaid.js diagram of all systems
#
# ZERO HALLUCINATION APPROACH:
#   Every system, stakeholder and process step must cite
#   a specific source chunk. If not found in docs →
#   flagged as "needs clarification" not invented.

import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import get_relevant_context, format_context_with_citations
from session_manager import (
    load_session, update_session,
    STAGE_ANALYSIS, STAGE_GAP_FILLING
)

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── Prompt: System & Stakeholder Analysis ─────────────────────
ANALYSIS_PROMPT = """
You are a senior Business Analyst performing a system and stakeholder analysis.

STRICT RULES — ZERO HALLUCINATION:
- Only identify systems mentioned or clearly implied in the provided context
- Only identify stakeholders or personas mentioned in the provided context
- Every system must have evidence — quote the source chunk
- If something is unclear, mark it as "needs_clarification: true"
- Never invent systems, stakeholders, or process steps

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REFINED PROBLEM STATEMENT:
{problem}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
KNOWLEDGE BASE CONTEXT:
{context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Respond with ONLY a JSON object. No preamble, no markdown.

Format:
{{
  "impacted_systems": [
    {{
      "name": "System name",
      "impact_level": "High | Medium | Low",
      "in_scope": true,
      "reason": "Why this system is impacted",
      "evidence": "Direct quote or paraphrase from context",
      "needs_clarification": false
    }}
  ],
  "impacted_stakeholders": [
    {{
      "name": "Stakeholder name or role",
      "team": "Which team/department",
      "impact_level": "High | Medium | Low",
      "involvement": "Responsible | Accountable | Consulted | Informed",
      "reason": "Why they are impacted",
      "needs_clarification": false
    }}
  ],
  "existing_process": [
    {{
      "step": 1,
      "action": "What happens",
      "actor": "Who does it",
      "system": "Which system is used",
      "pain_point": "What goes wrong here (if any)",
      "citation": "Source chunk reference",
      "needs_clarification": false
    }}
  ]
}}
"""


# ── Prompt: Mermaid Graph ──────────────────────────────────────
GRAPH_PROMPT = """
You are a technical diagram generator.
Create a Mermaid.js flowchart showing ALL systems and their relationships.

RULES:
- Include ALL systems from the analysis (both in-scope and out-of-scope)
- In-scope systems: use solid borders
- Out-of-scope systems: use dashed style
- Show data flow direction with arrows and labels
- Keep node names short (max 3 words)
- Use subgraphs to group related systems

Systems to diagram:
{systems}

Process flow:
{process}

Respond with ONLY the Mermaid diagram definition.
Start with: graph TD
No explanation, no markdown fences, just the diagram code.
"""


# ── Main Functions ─────────────────────────────────────────────
def run_analysis(session_id):
    """
    Stage 3 main function.
    Runs system + stakeholder + process analysis in one call.
    Returns the full analysis dict.
    """
    session = load_session(session_id)
    problem = session.get("problem_refined") or session.get("problem_raw")

    print(f"\n🔍 Searching knowledge base for analysis context...")
    results = get_relevant_context(
        question=problem,
        top_k=5,
        system_name=session.get("system_filter"),
        source_type=session.get("source_filter")
    )

    context = "No relevant documents found in the knowledge base."
    if results:
        ctx, _ = format_context_with_citations(results)
        context = ctx
        print(f"   Found {len(results)} relevant chunks")

    print(f"🧠 Running system & stakeholder analysis...")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a senior Business Analyst. "
                           "Analyse systems and stakeholders with evidence. "
                           "Respond with valid JSON only — no markdown, no explanation."
            },
            {
                "role": "user",
                "content": ANALYSIS_PROMPT.format(
                    problem=problem,
                    context=context
                )
            }
        ],
        temperature=0.1,
        max_tokens=2000
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if present
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    analysis = json.loads(raw.strip())

    # Save to session
    update_session(session_id, {
        "stage":                 STAGE_ANALYSIS,
        "impacted_systems":      analysis.get("impacted_systems", []),
        "impacted_stakeholders": analysis.get("impacted_stakeholders", []),
        "existing_process":      analysis.get("existing_process", []),
    })

    systems_count      = len(analysis.get("impacted_systems", []))
    stakeholders_count = len(analysis.get("impacted_stakeholders", []))
    process_count      = len(analysis.get("existing_process", []))

    print(f"✅ Analysis complete:")
    print(f"   Systems identified      : {systems_count}")
    print(f"   Stakeholders identified : {stakeholders_count}")
    print(f"   Process steps mapped    : {process_count}")

    return analysis


def generate_system_graph(session_id):
    """
    Generate a Mermaid.js diagram from the analysed systems.
    Returns the Mermaid diagram string.
    """
    session = load_session(session_id)
    systems = session.get("impacted_systems", [])
    process = session.get("existing_process", [])

    if not systems:
        return "graph TD\n    A[No systems identified yet]"

    print(f"🗺️  Generating system graph...")

    # Format systems for the prompt
    systems_text = ""
    for s in systems:
        scope = "IN SCOPE" if s.get("in_scope") else "OUT OF SCOPE"
        systems_text += (f"- {s['name']} | {scope} | "
                        f"Impact: {s['impact_level']} | "
                        f"Reason: {s['reason']}\n")

    # Format process for the prompt
    process_text = ""
    for p in process:
        process_text += (f"Step {p['step']}: {p['action']} "
                        f"(Actor: {p['actor']}, "
                        f"System: {p['system']})\n")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You generate Mermaid.js diagram definitions. "
                           "Output only valid Mermaid code, nothing else."
            },
            {
                "role": "user",
                "content": GRAPH_PROMPT.format(
                    systems=systems_text,
                    process=process_text
                )
            }
        ],
        temperature=0.1,
        max_tokens=600
    )

    graph = response.choices[0].message.content.strip()

    # Clean up if wrapped in fences
    if "```" in graph:
        graph = graph.split("```")[1]
        if graph.startswith("mermaid"):
            graph = graph[7:]

    graph = graph.strip()

    # Save to session
    update_session(session_id, {"system_graph": graph})
    print(f"✅ System graph generated ({len(graph)} chars)")
    return graph


def approve_analysis(session_id):
    """
    BA approves the analysis and advances to Stage 4 (Gap Filling).
    """
    update_session(session_id, {"stage": STAGE_GAP_FILLING})
    print(f"✅ Analysis approved — advancing to Stage 4: Gap Filling")


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from session_manager import create_session, update_session, STAGE_ANALYSIS

    print("=" * 55)
    print("TEST: Analysis Module — Stage 3")
    print("=" * 55)

    # Create session with a pre-refined problem
    print("\n── Step 1: Create session with refined problem")
    session = create_session(
        problem_raw="Our HR department is struggling with manual onboarding.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]

    # Inject a refined problem to simulate Stage 2 being complete
    update_session(sid, {
        "stage": STAGE_ANALYSIS,
        "problem_refined": (
            "The HR department currently takes 14 days to onboard new "
            "employees using manual email and paper-based processes. "
            "50 new hires per month require access to Email, Slack, and "
            "HRIS on day 1 but provisioning delays average 4 days. "
            "Goal: reduce onboarding to 2 days with zero access delays."
        )
    })
    print(f"   Session: {sid}")

    # Step 2: Run analysis
    print("\n── Step 2: Run system & stakeholder analysis")
    analysis = run_analysis(sid)

    print("\n── Systems Identified:")
    for s in analysis.get("impacted_systems", []):
        scope = "✅ IN SCOPE" if s.get("in_scope") else "⬜ OUT OF SCOPE"
        print(f"\n   {scope} | {s['name']}")
        print(f"   Impact  : {s['impact_level']}")
        print(f"   Reason  : {s['reason']}")
        print(f"   Evidence: {s.get('evidence', 'N/A')[:80]}...")
        if s.get("needs_clarification"):
            print(f"   ⚠️  Needs clarification")

    print("\n── Stakeholders Identified:")
    for st in analysis.get("impacted_stakeholders", []):
        print(f"\n   {st['name']} ({st['team']})")
        print(f"   Impact      : {st['impact_level']}")
        print(f"   Involvement : {st['involvement']}")
        print(f"   Reason      : {st['reason']}")

    print("\n── Existing Process:")
    for p in analysis.get("existing_process", []):
        flag = " ⚠️" if p.get("needs_clarification") else ""
        print(f"\n   Step {p['step']}: {p['action']}{flag}")
        print(f"   Actor    : {p['actor']}")
        print(f"   System   : {p['system']}")
        if p.get("pain_point"):
            print(f"   Pain     : {p['pain_point']}")

    # Step 3: Generate graph
    print("\n── Step 3: Generate Mermaid system graph")
    graph = generate_system_graph(sid)
    print(f"\n   Mermaid diagram:\n")
    print(graph)

    print("\n✅ Analysis Module working!")