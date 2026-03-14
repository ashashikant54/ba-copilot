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
#
# PROMPTS:
#   All prompt text lives in prompts.json — never hardcoded here.
#   ANALYSIS_PROMPT → prompts.json: stages.analysis
#   GRAPH_PROMPT    → prompts.json: stages.analysis_graph
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
    STAGE_ANALYSIS, STAGE_GAP_FILLING
)
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
 
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
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "analysis")
    model_cfg  = get_model_config("stages", "analysis")
    prompt_ver = get_prompt_version("stages", "analysis")
 
    print(f"🧠 Running system & stakeholder analysis ({model_cfg['model']}, prompt v{prompt_ver})...")
 
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
                    context=context
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
        # Observability
        "analysis_prompt_version": prompt_ver,
        "analysis_tokens_in":      input_tokens,
        "analysis_tokens_out":     output_tokens,
        "analysis_cost_usd":       call_cost,
    })
 
    print(f"✅ Analysis complete:")
    print(f"   Systems identified      : {len(analysis.get('impacted_systems', []))}")
    print(f"   Stakeholders identified : {len(analysis.get('impacted_stakeholders', []))}")
    print(f"   Process steps mapped    : {len(analysis.get('existing_process', []))}")
 
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
 
    # Load prompt config from prompts.json
    prompt_cfg = get_prompt("stages", "analysis_graph")
    model_cfg  = get_model_config("stages", "analysis_graph")
    prompt_ver = get_prompt_version("stages", "analysis_graph")
 
    print(f"   Using prompt v{prompt_ver}...")
 
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
                    systems=systems_text,
                    process=process_text
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    graph = response.choices[0].message.content.strip()
 
    # Token + cost tracking
    usage         = response.usage
    input_tokens  = usage.prompt_tokens     if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    call_cost     = estimate_cost(input_tokens, output_tokens)
    print(f"   📊 {input_tokens}in/{output_tokens}out tokens | ${call_cost:.6f}")
 
    # Clean up if wrapped in fences
    if "```" in graph:
        graph = graph.split("```")[1]
        if graph.startswith("mermaid"):
            graph = graph[7:]
 
    graph = graph.strip()
 
    # Save to session
    update_session(session_id, {
        "system_graph": graph,
        # Observability
        "graph_prompt_version": prompt_ver,
        "graph_tokens_in":      input_tokens,
        "graph_tokens_out":     output_tokens,
        "graph_cost_usd":       call_cost,
    })
 
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
 
    print("\n── Step 1: Create session with refined problem")
    session = create_session(
        problem_raw="Our HR department is struggling with manual onboarding.",
        system_name="HR System",
        source_type="SharePoint"
    )
    sid = session["session_id"]
 
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
 
    print("\n── Step 3: Generate Mermaid system graph")
    graph = generate_system_graph(sid)
    print(f"\n   Mermaid diagram:\n")
    print(graph)
 
    print("\n✅ Analysis Module working!")