# lg_coordinator.py
# CoAnalytica — Main Coordinator Graph
#
# ═══════════════════════════════════════════════════════════════
# LANGGRAPH CONCEPT 5: SUBGRAPHS
# ═══════════════════════════════════════════════════════════════
#
# A subgraph is a compiled LangGraph used as a node inside
# a larger graph. This is how multi-agent systems are built:
#
#   coordinator_graph
#     ↓
#     ├── node: "validate_requirements" = compiled F7 subgraph
#     └── node: "review_brd"            = compiled F8 subgraph
#
# When the coordinator executes "validate_requirements", it
# invokes the ENTIRE F7 subgraph as a single step from the
# coordinator's perspective. The subgraph runs all its own
# nodes internally.
#
# State handoff between subgraphs:
# ─────────────────────────────────
# Subgraphs have their own state types (RequirementsAgentState,
# BRDReviewAgentState). The coordinator has CoAnalyticaState.
#
# The bridge nodes (run_requirements_agent, run_brd_review_agent)
# extract fields from CoAnalyticaState, build the subgraph-specific
# input state, invoke the subgraph, then write results back to
# CoAnalyticaState. This is explicit — LangGraph does NOT
# automatically map fields between different state types.
#
# ═══════════════════════════════════════════════════════════════
# LANGGRAPH CONCEPT 6: COMPILE AND INVOKE
# ═══════════════════════════════════════════════════════════════
#
# compile() turns the graph definition into an executable.
# invoke() runs it with an initial state dict.
#
# Usage pattern:
#
#   graph = build_coanalytica_graph()
#   result = graph.invoke({
#       "session_id":   session_id,
#       "agent_to_run": "validate_requirements",
#       ...
#   })
#   f7_result = result["f7_result"]
#
# ═══════════════════════════════════════════════════════════════
# COORDINATOR TOPOLOGY
# ═══════════════════════════════════════════════════════════════
#
#   START
#     ↓
#   [route_agent_node]      ← reads agent_to_run from state
#     ↓
#   route_to_agent()        ← routing function
#     ├── "requirements" → [run_requirements_agent] → END
#     ├── "brd"          → [run_brd_review_agent]   → END
#     └── "both"         → [run_requirements_agent]
#                                     ↓
#                          [run_brd_review_agent]    → END
 
import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from langgraph.graph import StateGraph, END
from lg_state import CoAnalyticaState, RequirementsAgentState, BRDReviewAgentState
from lg_requirements_graph import build_requirements_validation_graph
from lg_brd_review_graph    import build_brd_review_graph
from session_manager import load_session
 
 
# ── Build subgraphs once at module load ────────────────────────
# compile() is expensive — do it once, reuse across requests.
# These are module-level compiled graphs.
_requirements_graph = None
_brd_review_graph   = None
 
def _get_requirements_graph():
    global _requirements_graph
    if _requirements_graph is None:
        print("   Compiling Requirements Validation subgraph...")
        _requirements_graph = build_requirements_validation_graph()
    return _requirements_graph
 
def _get_brd_review_graph():
    global _brd_review_graph
    if _brd_review_graph is None:
        print("   Compiling BRD Review subgraph...")
        _brd_review_graph = build_brd_review_graph()
    return _brd_review_graph
 
 
# ══════════════════════════════════════════════════════════════
# COORDINATOR NODES
# ══════════════════════════════════════════════════════════════
 
def route_agent_node(state: CoAnalyticaState) -> dict:
    """
    NODE: Entry point — just logs and passes through.
    Routing happens in the conditional edge after this node.
    """
    print(f"\n🎯 [LangGraph Coordinator] agent_to_run: {state['agent_to_run']}")
    return {}  # no state changes needed
 
 
def run_requirements_agent(state: CoAnalyticaState) -> dict:
    """
    NODE: Invokes the F7 Requirements Validation subgraph.
 
    ─────────────────────────────────────────────────────────
    STATE BRIDGE: CoAnalyticaState → RequirementsAgentState
    ─────────────────────────────────────────────────────────
    The subgraph has its own state type. This node:
      1. Extracts what the subgraph needs from CoAnalyticaState
      2. Builds RequirementsAgentState
      3. Invokes the subgraph
      4. Reads results from subgraph output state
      5. Writes back to CoAnalyticaState
 
    This explicit bridging is a key LangGraph pattern.
    Subgraphs are independent — they don't know about the
    coordinator's state schema.
    ─────────────────────────────────────────────────────────
    """
    session = load_session(state["session_id"])
 
    # Get non-rejected requirements
    requirements = [
        r for r in session.get("requirements", [])
        if r.get("status") != "rejected"
    ]
 
    # Build F7 subgraph input state
    f7_input: RequirementsAgentState = {
        "session_id":     state["session_id"],
        "requirements":   requirements,
        "system_filter":  session.get("system_filter"),
        "source_filter":  session.get("source_filter"),
        # These will be populated by the subgraph nodes:
        "kb_context":        "",
        "babok_result":      {},
        "quality_score":     0,
        "previous_issues":   "",
        "iteration":         1,
        "current_reqs":      [],
        "meeting_result":    {},
        "suggested_fixes":   {},
        "validation_result": {},
        "total_tokens_in":   0,
        "total_tokens_out":  0,
        "total_cost":        0.0,
    }
 
    # Invoke the subgraph — this runs all F7 nodes internally
    f7_output = _get_requirements_graph().invoke(f7_input)
 
    # Extract results and write back to coordinator state
    f7_result       = f7_output.get("validation_result", {})
    f7_quality_score = f7_result.get("quality_score", 0)
 
    print(f"\n🔗 [Coordinator] F7 complete — score: {f7_quality_score}")
 
    # PARTIAL STATE UPDATE to CoAnalyticaState
    return {
        "f7_result":        f7_result,
        "f7_quality_score": f7_quality_score,
    }
 
 
def run_brd_review_agent(state: CoAnalyticaState) -> dict:
    """
    NODE: Invokes the F8 BRD Review subgraph.
 
    ─────────────────────────────────────────────────────────
    MULTI-AGENT COORDINATION:
    f7_quality_score is read from CoAnalyticaState here and
    passed into BRDReviewAgentState. The F8 subgraph then
    uses it in initialise_brd_node to set effective_threshold.
 
    This is the state-based handoff between two agents:
      F7 writes f7_quality_score to CoAnalyticaState
      Coordinator reads it and passes to F8 input state
      F8 uses it to adjust its threshold
    ─────────────────────────────────────────────────────────
    """
    session = load_session(state["session_id"])
 
    brd_text = session.get("brd_draft", "").strip()
    if not brd_text:
        return {"error": "No BRD draft found — generate BRD preview first"}
 
    approved_reqs = [
        r for r in session.get("requirements", [])
        if r.get("status") in ("accepted", "edited")
    ]
 
    # Build F8 subgraph input state
    # Note: f7_quality_score is passed in from CoAnalyticaState
    f8_input: BRDReviewAgentState = {
        "session_id":          state["session_id"],
        "brd_text":            brd_text,
        "approved_reqs":       approved_reqs,
        "analysis_stakeholders": session.get("impacted_stakeholders", []),
        # ← Multi-agent coordination field
        "f7_quality_score":    state.get("f7_quality_score"),
        # These will be populated by subgraph nodes:
        "effective_threshold": 75,
        "traceability":        {},
        "quality_result":      {},
        "brd_quality_score":   0,
        "previous_issues":     "",
        "brd_iteration":       1,
        "current_brd":         brd_text,
        "stakeholder_result":  {},
        "suggested_section_fixes": [],
        "improved_brd":        None,
        "brd_review_result":   {},
        "brd_tokens_in":       0,
        "brd_tokens_out":      0,
        "brd_cost":            0.0,
    }
 
    # Invoke the subgraph
    f8_output = _get_brd_review_graph().invoke(f8_input)
 
    f8_result       = f8_output.get("brd_review_result", {})
    f8_quality_score = f8_result.get("quality_score", 0)
 
    print(f"\n🔗 [Coordinator] F8 complete — score: {f8_quality_score}")
 
    return {
        "f8_result":        f8_result,
        "f8_quality_score": f8_quality_score,
    }
 
 
# ══════════════════════════════════════════════════════════════
# ROUTING FUNCTION
# ══════════════════════════════════════════════════════════════
 
def route_to_agent(state: CoAnalyticaState) -> str:
    """
    Routing function for the coordinator.
 
    Reads agent_to_run from state and returns the next node name.
 
    agent_to_run values:
      "validate_requirements" → run only F7
      "review_brd"            → run only F8
      "both"                  → run F7 then F8 sequentially
    """
    agent = state.get("agent_to_run", "")
 
    if agent == "validate_requirements":
        return "requirements"
    elif agent == "review_brd":
        return "brd"
    elif agent == "both":
        return "requirements"  # F7 first; F8 follows via normal edge
    else:
        return "error"
 
 
# ══════════════════════════════════════════════════════════════
# GRAPH BUILDER — COORDINATOR
# ══════════════════════════════════════════════════════════════
 
def build_coanalytica_graph():
    """
    Build and compile the main CoAnalytica coordinator graph.
 
    This is the top-level graph. It uses F7 and F8 subgraphs
    as nodes and coordinates between them via state.
 
    Returns a compiled LangGraph invokable with CoAnalyticaState.
    """
    graph = StateGraph(CoAnalyticaState)
 
    # ── Register nodes ────────────────────────────────────────
    graph.add_node("route",              route_agent_node)
    graph.add_node("run_f7",             run_requirements_agent)
    graph.add_node("run_f8",             run_brd_review_agent)
 
    # ── Entry point ────────────────────────────────────────────
    graph.set_entry_point("route")
 
    # ── Conditional routing after entry ──────────────────────
    graph.add_conditional_edges(
        "route",
        route_to_agent,
        {
            "requirements": "run_f7",   # validate_requirements only
            "brd":          "run_f8",   # review_brd only
            "error":        END,        # unknown agent
        }
    )
 
    # ── F7 → F8 sequential edge (used when agent_to_run="both") ─
    # When routing sends to run_f7, after F7 completes we need
    # to decide: go to F8 (if "both") or end (if just F7).
    # We use another conditional edge for this.
    graph.add_conditional_edges(
        "run_f7",
        lambda state: "run_f8" if state.get("agent_to_run") == "both" else END,
        {
            "run_f8": "run_f8",
            END:      END,
        }
    )
 
    graph.add_edge("run_f8", END)
 
    return graph.compile()
 
 
# ══════════════════════════════════════════════════════════════
# PUBLIC INTERFACE
# ══════════════════════════════════════════════════════════════
# These are the functions called by main.py endpoints.
# They mirror the interface of the hand-rolled agents.
 
_coordinator = None
 
def _get_coordinator():
    global _coordinator
    if _coordinator is None:
        print("   Compiling CoAnalytica coordinator graph...")
        _coordinator = build_coanalytica_graph()
    return _coordinator
 
 
def lg_validate_requirements(session_id: str) -> dict:
    """
    LangGraph version of validate_requirements().
    Called by POST /sessions/{id}/requirements/validate/lg
    """
    result = _get_coordinator().invoke({
        "session_id":     session_id,
        "agent_to_run":   "validate_requirements",
        "f7_result":      None,
        "f7_quality_score": None,
        "f8_result":      None,
        "f8_quality_score": None,
        "error":          None,
    })
 
    if result.get("error"):
        raise ValueError(result["error"])
 
    return result.get("f7_result", {})
 
 
def lg_review_brd(session_id: str) -> dict:
    """
    LangGraph version of review_brd().
    Called by POST /sessions/{id}/brd/review/lg
    """
    # Check if F7 was already run — pass its score for coordination
    from session_manager import load_session as _ls
    session = _ls(session_id)
    f7_score = session.get("agent_validation_score")
 
    result = _get_coordinator().invoke({
        "session_id":       session_id,
        "agent_to_run":     "review_brd",
        "f7_result":        None,
        "f7_quality_score": f7_score,  # coordination: use stored F7 score
        "f8_result":        None,
        "f8_quality_score": None,
        "error":            None,
    })
 
    if result.get("error"):
        raise ValueError(result["error"])
 
    return result.get("f8_result", {})
 
 
def lg_run_both_agents(session_id: str) -> dict:
    """
    Run F7 then F8 sequentially in one graph invocation.
    Called by POST /sessions/{id}/agents/run-all
    This is the full multi-agent pipeline in a single call.
    """
    result = _get_coordinator().invoke({
        "session_id":       session_id,
        "agent_to_run":     "both",
        "f7_result":        None,
        "f7_quality_score": None,
        "f8_result":        None,
        "f8_quality_score": None,
        "error":            None,
    })
 
    if result.get("error"):
        raise ValueError(result["error"])
 
    return {
        "f7_result": result.get("f7_result", {}),
        "f8_result": result.get("f8_result", {}),
    }