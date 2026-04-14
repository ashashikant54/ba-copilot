# lg_state.py
# CoAnalytica — LangGraph State Definitions
#
# ═══════════════════════════════════════════════════════════════
# LANGGRAPH CONCEPT 1: STATE
# ═══════════════════════════════════════════════════════════════
#
# What is State in LangGraph?
# ───────────────────────────
# State is a TypedDict — a typed Python dictionary — that flows
# through every node in the graph. Think of it as the "memory"
# of the agent for one execution run.
#
# Rules:
#   - Every node READS from state
#   - Every node RETURNS only the fields it changed (partial update)
#   - LangGraph merges those partial updates back into the full state
#   - State is immutable inside a node — you never mutate it, you
#     return a new dict with the updated fields
#
# Why TypedDict instead of a regular dict?
#   - Type safety: you know exactly what fields exist
#   - IDE autocompletion works correctly
#   - LangGraph uses the type annotations to validate state transitions
#   - Makes the agent self-documenting — the state IS the spec
#
# Structure in this file:
#   RequirementsAgentState  → state for Feature 7 subgraph
#   BRDReviewAgentState     → state for Feature 8 subgraph
#   CoAnalyticaState        → combined state for the coordinator graph
#     (the coordinator state contains both sub-states plus
#      the multi-agent coordination fields)
 
from typing import TypedDict, Optional, List, Dict, Any
 
 
# ══════════════════════════════════════════════════════════════
# FEATURE 7 — Requirements Validation Agent State
# ══════════════════════════════════════════════════════════════
 
class RequirementsAgentState(TypedDict):
    """
    State for the Requirements Validation subgraph.
 
    Flows through these nodes:
      kb_search → babok_check → [reflect → babok_check]* → meeting_crossref
 
    Fields prefixed with _ are internal agent working state.
    Fields without _ are outputs surfaced to the coordinator.
    """
 
    # ── Inputs (set before subgraph runs) ─────────────────────
    session_id:          str
    org_id:              Optional[str]   # Phase 2 Sprint 4 — tenant scope
    requirements:        List[dict]   # full requirement objects from session
    system_filter:       Optional[str]
    source_filter:       Optional[str]
 
    # ── Tool 1 output: KB Search ───────────────────────────────
    kb_context:          str          # raw KB chunks retrieved
 
    # ── Tool 2 output: BABOK Check (updated each iteration) ───
    babok_result:        Dict[str, Any]  # full GPT response
    quality_score:       int             # overall_quality_score extracted
    previous_issues:     str             # passed to next iteration
 
    # ── Reflection loop control ────────────────────────────────
    iteration:           int          # current iteration (starts at 1)
    current_reqs:        List[dict]   # working copy — updated by reflection
 
    # ── Tool 3 output: Meeting Cross-reference ─────────────────
    meeting_result:      Dict[str, Any]
 
    # ── Final outputs (written when agent completes) ───────────
    suggested_fixes:     Dict[str, str]  # req_id → improved text
    validation_result:   Dict[str, Any]  # full result dict saved to session
 
    # ── Observability accumulators ─────────────────────────────
    total_tokens_in:     int
    total_tokens_out:    int
    total_cost:          float
 
 
# ══════════════════════════════════════════════════════════════
# FEATURE 8 — BRD Review Agent State
# ══════════════════════════════════════════════════════════════
 
class BRDReviewAgentState(TypedDict):
    """
    State for the BRD Review subgraph.
 
    Flows through these nodes:
      traceability → stakeholder_alignment → brd_quality
      → [brd_reflect → brd_quality]* → compile_result
 
    Multi-agent coordination:
      f7_quality_score is read from the coordinator state and
      used to set the effective_threshold for this agent.
    """
 
    # ── Inputs ─────────────────────────────────────────────────
    session_id:          str
    org_id:              Optional[str]   # Phase 2 Sprint 4 — tenant scope
    brd_text:            str          # current brd_draft from session
    approved_reqs:       List[dict]   # accepted + edited requirements
    analysis_stakeholders: List[dict] # from Stage 3 session data
 
    # ── Multi-agent coordination input ─────────────────────────
    # Set by coordinator BEFORE this subgraph runs.
    # If F7 score < 70, effective_threshold is raised to 80.
    f7_quality_score:    Optional[int]
    effective_threshold: int          # computed from f7_quality_score
 
    # ── Tool 1 output: Traceability Check ─────────────────────
    traceability:        Dict[str, Any]
 
    # ── Tool 2 output: BRD Quality Check (per iteration) ──────
    quality_result:      Dict[str, Any]
    brd_quality_score:   int
    previous_issues:     str
 
    # ── Reflection loop control ────────────────────────────────
    brd_iteration:       int
    current_brd:         str          # working copy — updated by reflection
 
    # ── Tool 3 output: Stakeholder Alignment ──────────────────
    stakeholder_result:  Dict[str, Any]
 
    # ── Final outputs ──────────────────────────────────────────
    suggested_section_fixes: List[dict]
    improved_brd:            Optional[str]
    brd_review_result:       Dict[str, Any]
 
    # ── Observability ──────────────────────────────────────────
    brd_tokens_in:       int
    brd_tokens_out:      int
    brd_cost:            float
 
 
# ══════════════════════════════════════════════════════════════
# COORDINATOR — Combined State
# ══════════════════════════════════════════════════════════════
 
class CoAnalyticaState(TypedDict):
    """
    Top-level state for the coordinator graph.
 
    The coordinator graph has two nodes — each is a compiled subgraph:
      validate_requirements  → RequirementsValidationGraph
      review_brd             → BRDReviewGraph
 
    The coordinator routes between them based on which agent
    the caller wants to run.
 
    Multi-agent coordination fields:
      f7_quality_score is written by the requirements agent
      and read by the BRD review agent to adjust its threshold.
      This is the state-based handoff between the two agents.
    """
 
    # ── Session context ─────────────────────────────────────────
    session_id:       str
    org_id:           Optional[str]    # Phase 2 Sprint 4 — tenant scope
    agent_to_run:     str    # "validate_requirements" | "review_brd" | "both"
 
    # ── Feature 7 outputs (written by requirements agent) ──────
    f7_result:        Optional[Dict[str, Any]]
    f7_quality_score: Optional[int]   # ← coordination field read by F8
 
    # ── Feature 8 outputs (written by BRD review agent) ────────
    f8_result:        Optional[Dict[str, Any]]
    f8_quality_score: Optional[int]
 
    # ── Error handling ──────────────────────────────────────────
    error:            Optional[str]