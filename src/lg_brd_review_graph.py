# lg_brd_review_graph.py
# CoAnalytica — Feature 8 as a LangGraph Subgraph
#
# This file follows the same patterns as lg_requirements_graph.py.
# Reading lg_requirements_graph.py first is recommended.
#
# ═══════════════════════════════════════════════════════════════
# GRAPH TOPOLOGY
# ═══════════════════════════════════════════════════════════════
#
#   START
#     ↓
#   [initialise_brd_node]    ← reads F7 score, sets effective_threshold
#     ↓
#   [traceability_node]      ← Tool 1: REQ-xxx coverage check (Python)
#     ↓
#   [stakeholder_node]       ← Tool 3: stakeholder alignment (GPT, once)
#     ↓
#   [brd_quality_node]       ← Tool 2: 6-dimension quality check (GPT)
#     ↓
#   should_reflect_brd()     ← routing function
#     ├── "done"    → [compile_brd_result_node] → END
#     └── "reflect" → [brd_reflection_node] → [brd_quality_node]
#                              ↑ loop max 3 times ↑
#
# KEY DIFFERENCE FROM F7 GRAPH:
#   initialise_brd_node reads f7_quality_score from state and
#   computes effective_threshold (75 normally, 80 if F7 was poor).
#   This is the multi-agent coordination happening inside LangGraph.
 
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from langgraph.graph import StateGraph, END
from lg_state import BRDReviewAgentState
 
# Reuse tool functions from the hand-rolled BRD Review Agent
from brd_review_agent import (
    _tool_traceability_check,
    _tool_brd_quality_check,
    _tool_stakeholder_alignment,
    _tool_brd_reflection,
    _apply_section_rewrites,
    _diff_brd_sections,
    _extract_section_issues_text,
    _format_approved_reqs,
    _score_to_confidence,
    BRD_QUALITY_THRESHOLD,
    MAX_ITERATIONS,
)
from session_manager import load_session, update_session
from prompt_manager import get_prompt_version
from telemetry import (
    agent_span, tool_span, llm_span,
    record_llm_usage, record_quality_score,
    record_reflection_triggered, record_agent_coordination,
    get_tracer,
)
 
 
# ══════════════════════════════════════════════════════════════
# NODES
# ══════════════════════════════════════════════════════════════
 
def initialise_brd_node(state: BRDReviewAgentState) -> dict:
    """
    NODE: Initialise BRD review state.
 
    Multi-agent coordination happens HERE.
 
    Reads f7_quality_score (written by the F7 subgraph into
    CoAnalyticaState, then passed into this state by the
    coordinator). If F7 score was poor, threshold is raised.
 
    This replaces the threshold computation block at the top of
    review_brd() in the hand-rolled agent.
    """
    f7_score = state.get("f7_quality_score")
 
    if f7_score is not None:
        print(f"\n🔗 [LangGraph] Multi-agent coordination: F7 score was {f7_score}/100")
        effective_threshold = 80 if f7_score < 70 else BRD_QUALITY_THRESHOLD
        if f7_score < 70:
            print(f"   ⚠️  Raising BRD threshold to {effective_threshold}")
        # OTel: record coordination event so it's visible in App Insights
        from opentelemetry import trace as _trace
        ctx_span = _trace.get_current_span()
        if ctx_span and ctx_span.is_recording():
            record_agent_coordination(ctx_span, f7_score, effective_threshold)
    else:
        effective_threshold = BRD_QUALITY_THRESHOLD
 
    print(f"\n🤖 [LangGraph] BRD Review Agent — threshold: {effective_threshold}")
 
    return {
        "effective_threshold": effective_threshold,
        "brd_iteration":       1,
        "current_brd":         state["brd_text"],
        "previous_issues":     "None — first iteration",
        "traceability":        {},
        "quality_result":      {},
        "brd_quality_score":   0,
        "stakeholder_result":  {},
        "suggested_section_fixes": [],
        "improved_brd":        None,
        "brd_review_result":   {},
        "brd_tokens_in":       0,
        "brd_tokens_out":      0,
        "brd_cost":            0.0,
    }
 
 
def traceability_node(state: BRDReviewAgentState) -> dict:
    """
    NODE: Tool 1 — Traceability Check (Python only, no GPT).
 
    Checks every approved REQ-xxx ID appears in the BRD.
    Fast and deterministic — no LLM needed.
    """
    with tool_span("traceability_check", state["session_id"]) as span:
        print(f"\n🔧 [LangGraph] Tool 1: Traceability Check")
        traceability = _tool_traceability_check(
            state["current_brd"], state["approved_reqs"]
        )
        covered = len(traceability["covered_ids"])
        missing = len(traceability["missing_ids"])
        print(f"   Covered: {covered} | Missing: {missing}")
        span.set_attribute("coanalytica.traceability.covered",     covered)
        span.set_attribute("coanalytica.traceability.missing",     missing)
        span.set_attribute("coanalytica.traceability.coverage_pct",
                           traceability.get("coverage_pct", 0))
 
    return {"traceability": traceability}
 
 
def stakeholder_alignment_node(state: BRDReviewAgentState) -> dict:
    """
    NODE: Tool 3 — Stakeholder Alignment (GPT, runs once).
 
    Placed before the quality check loop so it runs once.
    Results flow into compile_brd_result_node at the end.
    """
    with tool_span("stakeholder_alignment", state["session_id"]) as span:
        print(f"\n🔧 [LangGraph] Tool 3: Stakeholder Alignment")
        session_stub = {"impacted_stakeholders": state["analysis_stakeholders"]}
        stakeholder_result, t_in, t_out, cost = _tool_stakeholder_alignment(
            state["current_brd"], session_stub
        )
        missing_stk  = len(stakeholder_result.get("missing_from_brd", []))
        wrong_stk    = len(stakeholder_result.get("wrong_involvement", []))
        print(f"   Missing: {missing_stk}")
        span.set_attribute("coanalytica.stakeholder.missing",          missing_stk)
        span.set_attribute("coanalytica.stakeholder.wrong_involvement", wrong_stk)
        if t_in:
            span.set_attribute("gen_ai.usage.input_tokens",  t_in)
            span.set_attribute("gen_ai.usage.output_tokens", t_out)
            span.set_attribute("coanalytica.cost.usd",       cost)
 
    return {
        "stakeholder_result": stakeholder_result,
        "brd_tokens_in":  state["brd_tokens_in"]  + t_in,
        "brd_tokens_out": state["brd_tokens_out"] + t_out,
        "brd_cost":       state["brd_cost"]       + cost,
    }
 
 
def brd_quality_node(state: BRDReviewAgentState) -> dict:
    """
    NODE: Tool 2 — BRD Quality Check (GPT, per iteration).
 
    Reads current_brd — updated by brd_reflection_node each
    iteration so each pass evaluates the improved BRD.
    """
    session = load_session(state["session_id"])
 
    brd_iteration = state["brd_iteration"]
    with tool_span("brd_quality_check", state["session_id"],
                   iteration=brd_iteration) as span:
        print(f"\n🔧 [LangGraph] Tool 2: BRD Quality (iteration {brd_iteration}/{MAX_ITERATIONS})")
        quality_result, t_in, t_out, cost = _tool_brd_quality_check(
            state["current_brd"],
            state["approved_reqs"],
            state["traceability"],
            session,
            brd_iteration,
            state["previous_issues"]
        )
        brd_quality_score = quality_result.get("overall_quality_score", 0)
        threshold = state["effective_threshold"]
        print(f"   Score: {brd_quality_score}/100 (threshold: {threshold})")
 
        record_quality_score(span, brd_quality_score, threshold,
                             brd_quality_score >= threshold)
        span.set_attribute("gen_ai.usage.input_tokens",  t_in)
        span.set_attribute("gen_ai.usage.output_tokens", t_out)
        span.set_attribute("coanalytica.cost.usd",       cost)
 
        # Record dimension scores as span attributes
        dims = quality_result.get("dimension_scores", {})
        for dim, score in dims.items():
            span.set_attribute(f"coanalytica.brd.dim.{dim}", score)
 
        if brd_quality_score < threshold and brd_iteration < MAX_ITERATIONS:
            record_reflection_triggered(span, brd_iteration, brd_quality_score)
 
    return {
        "quality_result":    quality_result,
        "brd_quality_score": brd_quality_score,
        "brd_tokens_in":     state["brd_tokens_in"]  + t_in,
        "brd_tokens_out":    state["brd_tokens_out"] + t_out,
        "brd_cost":          state["brd_cost"]       + cost,
    }
 
 
def brd_reflection_node(state: BRDReviewAgentState) -> dict:
    """
    NODE: BRD Reflection — rewrites weak sections.
 
    Only reached when brd_quality_score < effective_threshold.
    Applies section rewrites to current_brd and increments iteration.
    """
    session = load_session(state["session_id"])
 
    issues_text = _extract_section_issues_text(state["quality_result"])
    with tool_span("brd_reflection", state["session_id"],
                   iteration=state["brd_iteration"]) as span:
        print(f"\n🔄 [LangGraph] BRD Reflection — rewriting weak sections...")
 
        reflection_result, t_in, t_out, cost = _tool_brd_reflection(
            state["current_brd"],
            state["approved_reqs"],
            issues_text,
            state["brd_quality_score"],
            state["effective_threshold"],
            session
        )
 
        improved_sections = reflection_result.get("improved_sections", [])
        updated_brd = state["current_brd"]
        if improved_sections:
            updated_brd = _apply_section_rewrites(state["current_brd"], improved_sections)
            print(f"   Sections rewritten: {len(improved_sections)}")
 
        span.set_attribute("coanalytica.reflection.sections_rewritten", len(improved_sections))
        span.set_attribute("coanalytica.reflection.score_before", state["brd_quality_score"])
        span.set_attribute("gen_ai.usage.input_tokens",  t_in)
        span.set_attribute("gen_ai.usage.output_tokens", t_out)
        span.set_attribute("coanalytica.cost.usd",       cost)
 
    return {
        "current_brd":     updated_brd,
        "brd_iteration":   state["brd_iteration"] + 1,
        "previous_issues": issues_text,
        "brd_tokens_in":   state["brd_tokens_in"]  + t_in,
        "brd_tokens_out":  state["brd_tokens_out"] + t_out,
        "brd_cost":        state["brd_cost"]       + cost,
    }
 
 
def compile_brd_result_node(state: BRDReviewAgentState) -> dict:
    """
    NODE: Compile final BRD review result and save to session.
 
    Terminal node — runs once after the quality loop exits.
    """
    original_brd = state["brd_text"]
    final_brd    = state["current_brd"]
    final_score  = state["brd_quality_score"]
    threshold    = state["effective_threshold"]
 
    # Build section fix suggestions if BRD was improved
    suggested_fixes = []
    improved_brd    = None
    if final_brd != original_brd:
        suggested_fixes = _diff_brd_sections(original_brd, final_brd)
        improved_brd    = final_brd
 
    f8_result = {
        "quality_score":           final_score,
        "passed":                  final_score >= threshold,
        "threshold_used":          threshold,
        "iterations":              state["brd_iteration"],
        "dimension_scores":        state["quality_result"].get("dimension_scores", {}),
        "section_issues":          state["quality_result"].get("section_issues", []),
        "traceability": {
            "covered_ids":         state["traceability"].get("covered_ids", []),
            "missing_ids":         state["traceability"].get("missing_ids", []),
            "coverage_pct":        state["traceability"].get("coverage_pct", 0),
        },
        "stakeholder_alignment":   state["stakeholder_result"],
        "suggested_section_fixes": suggested_fixes,
        "improved_brd":            improved_brd,
        "confidence":              _score_to_confidence(final_score),
        "summary":                 state["quality_result"].get("summary", ""),
        "f7_requirements_score":   state.get("f7_quality_score"),
    }
 
    # Save to session
    prompt_ver = get_prompt_version("stages", "agent_brd_quality")
    update_session(state["session_id"], {
        "brd_review_result":     f8_result,
        "brd_review_score":      final_score,
        "brd_review_iterations": state["brd_iteration"],
        "brd_review_prompt_ver": prompt_ver,
        "brd_review_tokens_in":  state["brd_tokens_in"],
        "brd_review_tokens_out": state["brd_tokens_out"],
        "brd_review_cost_usd":   round(state["brd_cost"], 6),
    })
 
    print(f"\n✅ [LangGraph] F8 complete: score={final_score}, "
          f"fixes={len(suggested_fixes)}, cost=${state['brd_cost']:.6f}")
 
    return {
        "suggested_section_fixes": suggested_fixes,
        "improved_brd":            improved_brd,
        "brd_review_result":       f8_result,
    }
 
 
# ══════════════════════════════════════════════════════════════
# ROUTING FUNCTION
# ══════════════════════════════════════════════════════════════
 
def should_reflect_brd(state: BRDReviewAgentState) -> str:
    """
    Routing function for BRD quality loop.
 
    Reads effective_threshold from state — this value was set
    by initialise_brd_node using the F7 coordination score.
    The routing function itself is unaware of F7; it just reads
    the threshold. Coordination is encapsulated in init.
    """
    if state["brd_quality_score"] >= state["effective_threshold"]:
        print(f"   ✅ BRD threshold met ({state['brd_quality_score']} >= "
              f"{state['effective_threshold']})")
        return "done"
    if state["brd_iteration"] >= MAX_ITERATIONS:
        print(f"   ⚠️  Max iterations reached")
        return "done"
    return "reflect"
 
 
# ══════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ══════════════════════════════════════════════════════════════
 
def build_brd_review_graph():
    """
    Build and compile the BRD Review subgraph.
    Returns a compiled LangGraph.
    """
    graph = StateGraph(BRDReviewAgentState)
 
    # Register nodes
    graph.add_node("initialise_brd",      initialise_brd_node)
    graph.add_node("traceability",         traceability_node)
    graph.add_node("stakeholder_alignment",stakeholder_alignment_node)
    graph.add_node("brd_quality",          brd_quality_node)
    graph.add_node("brd_reflection",       brd_reflection_node)
    graph.add_node("compile_brd_result",   compile_brd_result_node)
 
    # Entry point
    graph.set_entry_point("initialise_brd")
 
    # Normal edges
    graph.add_edge("initialise_brd",       "traceability")
    graph.add_edge("traceability",          "stakeholder_alignment")
    graph.add_edge("stakeholder_alignment", "brd_quality")
    graph.add_edge("brd_reflection",        "brd_quality")   # loop back
    graph.add_edge("compile_brd_result",    END)
 
    # Conditional edge — reflection decision
    graph.add_conditional_edges(
        "brd_quality",
        should_reflect_brd,
        {
            "reflect": "brd_reflection",
            "done":    "compile_brd_result",
        }
    )
 
    return graph.compile()