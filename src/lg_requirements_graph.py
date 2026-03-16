# lg_requirements_graph.py
# CoAnalytica — Feature 7 as a LangGraph Subgraph
#
# ═══════════════════════════════════════════════════════════════
# LANGGRAPH CONCEPTS 2, 3, 4: NODES, EDGES, ROUTING
# ═══════════════════════════════════════════════════════════════
#
# CONCEPT 2 — NODES
# ─────────────────
# Every tool from requirements_agent.py becomes a node.
# A node is just a Python function with this signature:
#
#   def node_name(state: RequirementsAgentState) -> dict:
#       # do work using state
#       return {"field_to_update": new_value}
#
# The return dict is a PARTIAL update — only changed fields.
# LangGraph merges it into the full state automatically.
#
# CONCEPT 3 — EDGES
# ─────────────────
# Edges define the path between nodes.
# Two types:
#
#   graph.add_edge("a", "b")              # always goes a → b
#   graph.add_conditional_edges(          # routing function decides
#       "b", routing_fn, {"x": "c", "y": END}
#   )
#
# CONCEPT 4 — ROUTING FUNCTIONS
# ──────────────────────────────
# The routing function reads state and returns a string.
# That string maps to the next node via the routing dict.
# This is where your if/break loop logic lives in LangGraph.
#
# Compare:
#
#   Hand-rolled:
#     if quality_score >= QUALITY_THRESHOLD: break
#     if iteration >= MAX_ITERATIONS: break
#
#   LangGraph:
#     def should_reflect(state) -> str:
#         if state["quality_score"] >= QUALITY_THRESHOLD: return "done"
#         if state["iteration"] >= MAX_ITERATIONS:        return "done"
#         return "reflect"
#
# ═══════════════════════════════════════════════════════════════
# GRAPH TOPOLOGY
# ═══════════════════════════════════════════════════════════════
#
#   START
#     ↓
#   [initialise_node]       ← sets up working_reqs, iteration=1
#     ↓
#   [kb_search_node]        ← Tool 1: KB retrieval (no GPT)
#     ↓
#   [meeting_crossref_node] ← Tool 3: meeting decisions (GPT, once)
#     ↓
#   [babok_check_node]      ← Tool 2: BABOK quality check (GPT)
#     ↓
#   should_reflect()        ← routing function
#     ├── "done"    → [compile_result_node] → END
#     └── "reflect" → [reflection_node] → [babok_check_node]
#                              ↑ loop max 3 times ↑
 
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from langgraph.graph import StateGraph, END
from lg_state import RequirementsAgentState
 
# Import the actual tool functions from the hand-rolled agent.
# We REUSE the tool logic — we're only changing the orchestration layer.
from requirements_agent import (
    _tool_kb_search,
    _tool_babok_check,
    _tool_meeting_crossref,
    _tool_reflection,
    _extract_issues_text,
    _score_to_confidence,
    QUALITY_THRESHOLD,
    MAX_ITERATIONS,
)
from session_manager import load_session, update_session
from prompt_manager import get_prompt_version
from telemetry import (
    agent_span, tool_span, llm_span,
    record_llm_usage, record_quality_score,
    record_reflection_triggered, get_tracer,
)
 
 
# ══════════════════════════════════════════════════════════════
# NODES
# ══════════════════════════════════════════════════════════════
# Each node is a pure function: (state) → partial state update dict.
# Nodes do NOT mutate state. They return what changed.
 
def initialise_node(state: RequirementsAgentState) -> dict:
    """
    NODE: Initialise agent working state.
 
    Why a separate init node?
    In LangGraph you cannot pass complex default values when
    you invoke the graph — you need an explicit node that sets
    up derived state from the raw inputs.
 
    This replaces the working_reqs setup block at the top of
    validate_requirements() in the hand-rolled agent.
    """
    requirements = state["requirements"]
 
    # Build working copy — use edited_text if BA has already edited
    working_reqs = []
    for r in requirements:
        working_reqs.append({
            **r,
            "effective_text": r["edited_text"] if r.get("edited_text") else r["text"],
        })
 
    print(f"\n🤖 [LangGraph] Requirements Validation Agent — {len(working_reqs)} requirements")
 
    # OTel: record agent start event on current span (set by caller)
    tracer = get_tracer()
    current_span = tracer.start_span.__self__ if hasattr(tracer.start_span, '__self__') else None
    from opentelemetry import trace as _trace
    ctx_span = _trace.get_current_span()
    if ctx_span and ctx_span.is_recording():
        ctx_span.set_attribute("coanalytica.session_id",          state["session_id"])
        ctx_span.set_attribute("coanalytica.agent.name",          "requirements_validation")
        ctx_span.set_attribute("coanalytica.requirements.count",  len(working_reqs))
 
    # PARTIAL STATE UPDATE — return only what changed
    return {
        "current_reqs":    working_reqs,
        "iteration":       1,
        "total_tokens_in":  0,
        "total_tokens_out": 0,
        "total_cost":       0.0,
        "previous_issues":  "None — first iteration",
        "kb_context":       "",
        "meeting_result":   {},
        "babok_result":     {},
        "quality_score":    0,
        "suggested_fixes":  {},
        "validation_result": {},
    }
 
 
def kb_search_node(state: RequirementsAgentState) -> dict:
    """
    NODE: Tool 1 — KB Search.
 
    Calls the existing _tool_kb_search() from requirements_agent.py.
    Returns kb_context string added to state.
    No GPT call — pure Python retrieval.
    """
    session = load_session(state["session_id"])
 
    with tool_span("kb_search", state["session_id"]) as span:
        print(f"\n🔧 [LangGraph] Tool 1: KB Search")
        kb_context = _tool_kb_search(state["current_reqs"], session)
        print(f"   KB context: {len(kb_context)} chars")
        span.set_attribute("coanalytica.kb.context_chars", len(kb_context))
        span.set_attribute("coanalytica.kb.has_results",   len(kb_context) > 50)
 
    return {"kb_context": kb_context}
 
 
def meeting_crossref_node(state: RequirementsAgentState) -> dict:
    """
    NODE: Tool 3 — Meeting Cross-reference.
 
    Runs ONCE (not in the reflection loop) because meeting decisions
    don't change between iterations. In the graph topology it runs
    before the babok_check loop, not inside it.
 
    Compare to hand-rolled: it ran in parallel with the loop.
    In LangGraph: explicit sequential placement before the loop.
    """
    session = load_session(state["session_id"])
 
    with tool_span("meeting_crossref", state["session_id"]) as span:
        print(f"\n🔧 [LangGraph] Tool 3: Meeting Cross-reference")
        meeting_result, t_in, t_out, cost = _tool_meeting_crossref(
            state["current_reqs"], session
        )
        conflicts = len(meeting_result.get("conflicts", []))
        missing   = len(meeting_result.get("missing_requirements", []))
        print(f"   Conflicts: {conflicts}")
        span.set_attribute("coanalytica.meeting.conflicts",           conflicts)
        span.set_attribute("coanalytica.meeting.missing_requirements",missing)
        if t_in:
            span.set_attribute(
                "gen_ai.usage.input_tokens",  t_in)
            span.set_attribute(
                "gen_ai.usage.output_tokens", t_out)
            span.set_attribute("coanalytica.cost.usd", cost)
 
    # Accumulate observability counters
    return {
        "meeting_result":   meeting_result,
        "total_tokens_in":  state["total_tokens_in"]  + t_in,
        "total_tokens_out": state["total_tokens_out"] + t_out,
        "total_cost":       state["total_cost"]       + cost,
    }
 
 
def babok_check_node(state: RequirementsAgentState) -> dict:
    """
    NODE: Tool 2 — BABOK Quality Check.
 
    This node runs on EVERY iteration of the reflection loop.
    It reads current_reqs (which gets updated by reflection_node)
    so each iteration evaluates the improved requirements.
 
    The iteration counter is read from state — no loop variable needed.
    LangGraph manages the loop by routing back to this node.
    """
    session = load_session(state["session_id"])
 
    iteration = state["iteration"]
    with tool_span("babok_check", state["session_id"], iteration=iteration) as span:
        print(f"\n🔧 [LangGraph] Tool 2: BABOK Check (iteration {iteration}/{MAX_ITERATIONS})")
        babok_result, t_in, t_out, cost = _tool_babok_check(
            state["current_reqs"],
            state["kb_context"],
            session,
            iteration,
            state["previous_issues"]
        )
        quality_score = babok_result.get("overall_quality_score", 0)
        print(f"   Quality score: {quality_score}/100")
 
        # OTel: record quality score and token usage
        record_quality_score(
            span, quality_score, QUALITY_THRESHOLD,
            quality_score >= QUALITY_THRESHOLD
        )
        span.set_attribute("gen_ai.usage.input_tokens",  t_in)
        span.set_attribute("gen_ai.usage.output_tokens", t_out)
        span.set_attribute("coanalytica.cost.usd",       cost)
 
        # Emit reflection event if score is below threshold
        if quality_score < QUALITY_THRESHOLD and iteration < MAX_ITERATIONS:
            record_reflection_triggered(span, iteration, quality_score)
 
    return {
        "babok_result":     babok_result,
        "quality_score":    quality_score,
        "total_tokens_in":  state["total_tokens_in"]  + t_in,
        "total_tokens_out": state["total_tokens_out"] + t_out,
        "total_cost":       state["total_cost"]       + cost,
    }
 
 
def reflection_node(state: RequirementsAgentState) -> dict:
    """
    NODE: Reflection — only reached when quality_score < threshold.
 
    Generates improved requirement text, updates current_reqs,
    increments the iteration counter.
 
    In the hand-rolled agent this was inside the for loop body.
    In LangGraph it's a separate node — the loop is expressed
    as edges (babok_check → reflect → babok_check).
    """
    session = load_session(state["session_id"])
 
    issues_text = _extract_issues_text(state["babok_result"])
 
    with tool_span("reflection", state["session_id"],
                   iteration=state["iteration"]) as span:
        print(f"\n🔄 [LangGraph] Reflection — rewriting requirements...")
 
        reflection_result, t_in, t_out, cost = _tool_reflection(
            state["current_reqs"],
            issues_text,
            state["quality_score"],
            session
        )
 
        # Apply improvements to current_reqs
        improved_map = {
            r["req_id"]: r["improved_text"]
            for r in reflection_result.get("improved_requirements", [])
            if r.get("improved_text")
        }
        updated_reqs = []
        for r in state["current_reqs"]:
            improved = improved_map.get(r["id"])
            updated_reqs.append({
                **r,
                "effective_text": improved if improved else r["effective_text"],
            })
 
        improvements = len(improved_map)
        print(f"   Improvements: {improvements}")
        span.set_attribute("coanalytica.reflection.improvements", improvements)
        span.set_attribute("coanalytica.reflection.score_before", state["quality_score"])
        span.set_attribute("gen_ai.usage.input_tokens",  t_in)
        span.set_attribute("gen_ai.usage.output_tokens", t_out)
        span.set_attribute("coanalytica.cost.usd",       cost)
 
    return {
        "current_reqs":     updated_reqs,
        "iteration":        state["iteration"] + 1,
        "previous_issues":  issues_text,
        "total_tokens_in":  state["total_tokens_in"]  + t_in,
        "total_tokens_out": state["total_tokens_out"] + t_out,
        "total_cost":       state["total_cost"]       + cost,
    }
 
 
def compile_result_node(state: RequirementsAgentState) -> dict:
    """
    NODE: Compile final result and save to session.
 
    This is the terminal node — runs once after the loop exits.
    Equivalent to the result compilation block at the bottom of
    validate_requirements() in the hand-rolled agent.
    """
    # Compute suggested_fixes by diffing original vs final
    original_map = {r["id"]: r["text"] for r in state["requirements"]}
    suggested_fixes = {}
    for r in state["current_reqs"]:
        orig = original_map.get(r["id"], "")
        if r["effective_text"] != orig:
            suggested_fixes[r["id"]] = r["effective_text"]
 
    final_score = state["quality_score"]
    f7_result = {
        "quality_score":         final_score,
        "passed":                final_score >= QUALITY_THRESHOLD,
        "iterations":            state["iteration"],
        "requirement_scores":    state["babok_result"].get("requirement_scores", []),
        "meeting_conflicts":     state["meeting_result"].get("conflicts", []),
        "missing_from_meetings": state["meeting_result"].get("missing_requirements", []),
        "meeting_aligned":       state["meeting_result"].get("aligned_req_ids", []),
        "suggested_fixes":       suggested_fixes,
        "confidence":            _score_to_confidence(final_score),
        "summary":               state["babok_result"].get("summary", ""),
        "meeting_summary":       state["meeting_result"].get("summary", ""),
    }
 
    # Save to session
    prompt_ver = get_prompt_version("stages", "agent_babok_check")
    update_session(state["session_id"], {
        "agent_validation_result":     f7_result,
        "agent_validation_score":      final_score,
        "agent_validation_iterations": state["iteration"],
        "agent_prompt_version":        prompt_ver,
        "agent_tokens_in":             state["total_tokens_in"],
        "agent_tokens_out":            state["total_tokens_out"],
        "agent_cost_usd":              round(state["total_cost"], 6),
    })
 
    print(f"\n✅ [LangGraph] F7 complete: score={final_score}, "
          f"fixes={len(suggested_fixes)}, cost=${state['total_cost']:.6f}")
 
    # OTel: record final agent metrics on current span
    from opentelemetry import trace as _trace
    ctx_span = _trace.get_current_span()
    if ctx_span and ctx_span.is_recording():
        ctx_span.set_attribute("coanalytica.agent.final_score",    final_score)
        ctx_span.set_attribute("coanalytica.agent.total_cost_usd", round(state["total_cost"], 6))
        ctx_span.set_attribute("coanalytica.agent.total_tokens",
                                state["total_tokens_in"] + state["total_tokens_out"])
        ctx_span.set_attribute("coanalytica.agent.fixes_suggested", len(suggested_fixes))
        ctx_span.set_attribute("coanalytica.agent.iterations",      state["iteration"])
 
    return {
        "suggested_fixes":    suggested_fixes,
        "validation_result":  f7_result,
    }
 
 
# ══════════════════════════════════════════════════════════════
# ROUTING FUNCTION
# ══════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────
# CONCEPT 4: ROUTING FUNCTION
#
# This function is called by LangGraph after babok_check_node runs.
# It reads state and returns a STRING that maps to the next node.
#
# Hand-rolled equivalent:
#   if quality_score >= QUALITY_THRESHOLD: break
#   if iteration >= MAX_ITERATIONS:        break
#   # else: run reflection
#
# LangGraph: the routing dict in add_conditional_edges maps
# the returned string to the actual node name.
# ─────────────────────────────────────────────────────────────
 
def should_reflect_requirements(state: RequirementsAgentState) -> str:
    """
    Routing function: decides whether to reflect or finish.
 
    Returns:
      "reflect" → goes to reflection_node → loops back to babok_check
      "done"    → goes to compile_result_node → END
    """
    if state["quality_score"] >= QUALITY_THRESHOLD:
        print(f"   ✅ Threshold met ({state['quality_score']} >= {QUALITY_THRESHOLD})")
        return "done"
    if state["iteration"] >= MAX_ITERATIONS:
        print(f"   ⚠️  Max iterations ({MAX_ITERATIONS}) reached")
        return "done"
    return "reflect"
 
 
# ══════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ══════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────
# CONCEPT 3: EDGES
#
# Wiring the graph:
#   add_edge(a, b)                    — always a → b
#   set_entry_point(node)             — where START goes
#   add_conditional_edges(from, fn, map) — routing
#
# The loop is expressed as:
#   babok_check → [should_reflect] → reflect → babok_check
# ─────────────────────────────────────────────────────────────
 
def build_requirements_validation_graph():
    """
    Build and compile the Requirements Validation subgraph.
    Returns a compiled LangGraph that can be:
      - Invoked directly: graph.invoke(initial_state)
      - Used as a node inside the coordinator graph
    """
    # ── 1. Create the graph with our state type ────────────────
    graph = StateGraph(RequirementsAgentState)
 
    # ── 2. Register all nodes ──────────────────────────────────
    # graph.add_node(name, function)
    graph.add_node("initialise",       initialise_node)
    graph.add_node("kb_search",        kb_search_node)
    graph.add_node("meeting_crossref", meeting_crossref_node)
    graph.add_node("babok_check",      babok_check_node)
    graph.add_node("reflection",       reflection_node)
    graph.add_node("compile_result",   compile_result_node)
 
    # ── 3. Set entry point ────────────────────────────────────
    graph.set_entry_point("initialise")
 
    # ── 4. Add normal edges (always traverse) ─────────────────
    graph.add_edge("initialise",       "kb_search")
    graph.add_edge("kb_search",        "meeting_crossref")
    graph.add_edge("meeting_crossref", "babok_check")
    graph.add_edge("reflection",       "babok_check")   # ← loop back edge
    graph.add_edge("compile_result",   END)
 
    # ── 5. Add conditional edge (the reflection decision) ─────
    # After babok_check runs, call should_reflect_requirements().
    # Map its return value to the next node.
    graph.add_conditional_edges(
        "babok_check",                     # from this node
        should_reflect_requirements,        # call this routing function
        {
            "reflect": "reflection",        # "reflect" → reflection_node
            "done":    "compile_result",    # "done"    → compile_result_node
        }
    )
 
    # ── 6. Compile ─────────────────────────────────────────────
    # compile() validates the graph structure and returns an
    # executable. After this, the graph cannot be modified.
    return graph.compile()