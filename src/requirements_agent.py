# requirements_agent.py
# CoAnalytica — Feature 7: Requirements Validation Agent
#
# Architecture: Observe → Plan → Act loop (max 3 iterations)
#
# TOOLS:
#   Tool 1: _tool_kb_search()        — Python only, no GPT call
#     Searches knowledge base for evidence related to or contradicting
#     each requirement. Returns raw KB context for Tool 2 to analyse.
#
#   Tool 2: _tool_babok_check()      — 1 GPT call per iteration
#     Evaluates all requirements against BABOK quality dimensions:
#     completeness, testability, unambiguity, atomicity, consistency.
#     Also flags contradictions found in KB context.
#
#   Tool 3: _tool_meeting_crossref() — 1 GPT call (once, not per iteration)
#     Loads all meeting decisions from Azure Blob.
#     Cross-references requirements against decisions — finds conflicts
#     and decisions that have no corresponding requirement.
#
# REFLECTION LOOP:
#   If overall_quality_score < QUALITY_THRESHOLD (70):
#     → Run _tool_reflection() to generate improved requirement text
#     → Re-run Tool 2 on improved text to verify score improvement
#     → Repeat up to MAX_ITERATIONS (3) times
#
# OUTPUT (saved to session + returned to API):
#   quality_score, passed, iterations, per-requirement issues,
#   meeting conflicts, suggested fixes, confidence, summary
#
# OBSERVABILITY:
#   All token counts and costs saved to session record with agent_ prefix.
 
import os
import sys
import json
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from dotenv import load_dotenv
from openai import OpenAI
 
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
from retriever import get_relevant_context, format_context_with_citations
from session_manager import load_session, update_session
from meeting_module import list_meetings, load_meeting
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
# ── Constants ──────────────────────────────────────────────────
QUALITY_THRESHOLD = 70   # Score below this triggers reflection
MAX_ITERATIONS    = 3    # Max reflection loops before giving up
 
 
# ══════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════
 
def validate_requirements(session_id: str) -> dict:
    """
    Main agent entry point.
    Validates all non-rejected requirements in the session.
    Returns full validation result dict.
    """
    session      = load_session(session_id)
    requirements = [
        r for r in session.get("requirements", [])
        if r.get("status") != "rejected"
    ]
 
    if not requirements:
        raise ValueError(
            "No requirements to validate. "
            "Please extract requirements first, or ensure at least one is not rejected."
        )
 
    print(f"\n🤖 Requirements Validation Agent starting...")
    print(f"   Session: {session_id}")
    print(f"   Requirements to validate: {len(requirements)}")
 
    # Build working list — use edited_text if BA has already edited a requirement
    working_reqs = []
    for r in requirements:
        working_reqs.append({
            "id":             r["id"],
            "type":           r.get("type", ""),
            "text":           r["text"],
            "rationale":      r.get("rationale", ""),
            "source":         r.get("source", ""),
            "confidence":     r.get("confidence", ""),
            "status":         r.get("status", "pending"),
            "effective_text": r["edited_text"] if r.get("edited_text") else r["text"],
        })
 
    # ── Observability accumulators ──────────────────────────────
    total_tokens_in  = 0
    total_tokens_out = 0
    total_cost       = 0.0
 
    # ── Tool 1: KB Search (Python only — no GPT call) ──────────
    print(f"\n🔧 Tool 1: Searching knowledge base...")
    kb_context = _tool_kb_search(working_reqs, session)
    print(f"   KB context: {len(kb_context)} chars")
 
    # ── Tool 3: Meeting Cross-reference (runs once) ─────────────
    print(f"\n🔧 Tool 3: Cross-referencing meeting decisions...")
    meeting_result, t_in, t_out, cost = _tool_meeting_crossref(
        working_reqs, session
    )
    total_tokens_in  += t_in
    total_tokens_out += t_out
    total_cost       += cost
    print(f"   Conflicts found: {len(meeting_result.get('conflicts', []))}")
    print(f"   Missing requirements from meetings: {len(meeting_result.get('missing_requirements', []))}")
 
    # ── Reflection loop: Tool 2 + optional _tool_reflection ────
    current_reqs    = working_reqs[:]   # copy — gets updated each iteration
    babok_result    = {}
    previous_issues = "None — first iteration"
    final_iteration = 1
 
    for iteration in range(1, MAX_ITERATIONS + 1):
        final_iteration = iteration
        print(f"\n🔧 Tool 2: BABOK quality check (iteration {iteration}/{MAX_ITERATIONS})...")
 
        babok_result, t_in, t_out, cost = _tool_babok_check(
            current_reqs, kb_context, session, iteration, previous_issues
        )
        total_tokens_in  += t_in
        total_tokens_out += t_out
        total_cost       += cost
 
        quality_score = babok_result.get("overall_quality_score", 0)
        print(f"   Quality score: {quality_score}/100")
 
        if quality_score >= QUALITY_THRESHOLD:
            print(f"   ✅ Threshold met ({quality_score} >= {QUALITY_THRESHOLD}) — stopping")
            break
 
        if iteration == MAX_ITERATIONS:
            print(f"   ⚠️  Max iterations reached — returning best result")
            break
 
        # Score below threshold — run reflection
        print(f"   🔄 Score {quality_score} < {QUALITY_THRESHOLD} — running reflection...")
        issues_for_reflection = _extract_issues_text(babok_result)
        previous_issues       = issues_for_reflection
 
        reflection_result, t_in, t_out, cost = _tool_reflection(
            current_reqs, issues_for_reflection, quality_score, session
        )
        total_tokens_in  += t_in
        total_tokens_out += t_out
        total_cost       += cost
 
        # Update current_reqs with improved text for next iteration
        improved_map = {
            r["req_id"]: r["improved_text"]
            for r in reflection_result.get("improved_requirements", [])
            if r.get("improved_text")
        }
 
        updated = 0
        current_reqs = []
        for r in working_reqs:
            improved = improved_map.get(r["id"])
            current_reqs.append({
                **r,
                "effective_text": improved if improved else r["effective_text"],
                "_was_improved":  bool(improved),
            })
            if improved:
                updated += 1
 
        print(f"   Requirements improved this iteration: {updated}")
 
    # ── Build suggested_fixes: req_id → improved text ──────────
    suggested_fixes = {}
    for r_orig, r_curr in zip(working_reqs, current_reqs):
        if r_curr["effective_text"] != r_orig["effective_text"]:
            suggested_fixes[r_orig["id"]] = r_curr["effective_text"]
 
    # ── Compile final result ────────────────────────────────────
    final_score = babok_result.get("overall_quality_score", 0)
 
    result = {
        "quality_score":          final_score,
        "passed":                 final_score >= QUALITY_THRESHOLD,
        "iterations":             final_iteration,
        "requirement_scores":     babok_result.get("requirement_scores", []),
        "meeting_conflicts":      meeting_result.get("conflicts", []),
        "missing_from_meetings":  meeting_result.get("missing_requirements", []),
        "meeting_aligned":        meeting_result.get("aligned_req_ids", []),
        "suggested_fixes":        suggested_fixes,
        "confidence":             _score_to_confidence(final_score),
        "summary":                babok_result.get("summary", ""),
        "meeting_summary":        meeting_result.get("summary", ""),
    }
 
    # ── Save to session ─────────────────────────────────────────
    prompt_ver = get_prompt_version("stages", "agent_babok_check")
    update_session(session_id, {
        "agent_validation_result":     result,
        "agent_validation_score":      final_score,
        "agent_validation_iterations": final_iteration,
        "agent_prompt_version":        prompt_ver,
        "agent_tokens_in":             total_tokens_in,
        "agent_tokens_out":            total_tokens_out,
        "agent_cost_usd":              round(total_cost, 6),
    })
 
    print(f"\n✅ Agent complete: score={final_score}, iterations={final_iteration}, "
          f"fixes={len(suggested_fixes)}, cost=${total_cost:.6f}")
 
    return result
 
 
# ══════════════════════════════════════════════════════════════
# TOOL 1 — KB SEARCH (Python only, no GPT call)
# ══════════════════════════════════════════════════════════════
 
def _tool_kb_search(requirements: list, session: dict) -> str:
    """
    Search the knowledge base for context related to the requirements.
    Combines all requirement texts into a single compound query so we
    make one search call rather than N calls.
    Returns formatted context string for Tool 2 to analyse.
    """
    # Build a compound query from requirement texts
    req_texts = " ".join(r["effective_text"] for r in requirements[:5])  # cap at 5
    query = f"requirements validation: {req_texts[:300]}"
 
    try:
        results = get_relevant_context(
            question=query,
            top_k=5,
            system_name=session.get("system_filter"),
            source_type=session.get("source_filter")
        )
        if not results:
            return "No relevant knowledge base content found."
 
        context, _ = format_context_with_citations(results)
        return context
 
    except Exception as e:
        print(f"   ⚠️  KB search failed: {e} — continuing without KB context")
        return "Knowledge base search unavailable."
 
 
# ══════════════════════════════════════════════════════════════
# TOOL 2 — BABOK QUALITY CHECK (1 GPT call per iteration)
# ══════════════════════════════════════════════════════════════
 
def _tool_babok_check(
    requirements:     list,
    kb_context:       str,
    session:          dict,
    iteration:        int,
    previous_issues:  str
) -> tuple:
    """
    Evaluates requirements against BABOK quality dimensions.
    Returns (result_dict, tokens_in, tokens_out, cost).
    """
    prompt_cfg = get_prompt("stages", "agent_babok_check")
    model_cfg  = get_model_config("stages", "agent_babok_check")
    prompt_ver = get_prompt_version("stages", "agent_babok_check")
 
    reqs_text = _format_requirements_for_prompt(requirements)
 
    print(f"   Running BABOK check (prompt v{prompt_ver}, {model_cfg['model']})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    iteration=iteration,
                    requirements=reqs_text,
                    kb_context=kb_context or "No KB context available.",
                    previous_issues=previous_issues
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    raw          = response.choices[0].message.content.strip()
    usage        = response.usage
    tokens_in    = usage.prompt_tokens     if usage else 0
    tokens_out   = usage.completion_tokens if usage else 0
    cost         = estimate_cost(tokens_in, tokens_out)
 
    print(f"   📊 {tokens_in}in/{tokens_out}out tokens | ${cost:.6f}")
 
    result = _safe_parse_json(raw, default={
        "overall_quality_score": 0,
        "requirement_scores": [],
        "summary": "Parse error — could not evaluate requirements"
    })
 
    return result, tokens_in, tokens_out, cost
 
 
# ══════════════════════════════════════════════════════════════
# TOOL 3 — MEETING CROSS-REFERENCE (1 GPT call, once only)
# ══════════════════════════════════════════════════════════════
 
def _tool_meeting_crossref(requirements: list, session: dict) -> tuple:
    """
    Loads all meeting decisions and cross-references with requirements.
    Returns (result_dict, tokens_in, tokens_out, cost).
    If no meetings exist, returns empty result with zero cost.
    """
    # Load all meeting decisions
    meeting_decisions_text = _load_meeting_decisions()
 
    if not meeting_decisions_text:
        print("   No meeting records found — skipping cross-reference")
        return {
            "conflicts":             [],
            "missing_requirements":  [],
            "aligned_req_ids":       [],
            "summary":               "No meeting records available for cross-reference."
        }, 0, 0, 0.0
 
    prompt_cfg = get_prompt("stages", "agent_meeting_crossref")
    model_cfg  = get_model_config("stages", "agent_meeting_crossref")
    prompt_ver = get_prompt_version("stages", "agent_meeting_crossref")
 
    reqs_text = _format_requirements_for_prompt(requirements)
    print(f"   Cross-referencing against meeting decisions (prompt v{prompt_ver})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    requirements=reqs_text,
                    meeting_decisions=meeting_decisions_text
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    raw       = response.choices[0].message.content.strip()
    usage     = response.usage
    tokens_in  = usage.prompt_tokens     if usage else 0
    tokens_out = usage.completion_tokens if usage else 0
    cost       = estimate_cost(tokens_in, tokens_out)
 
    print(f"   📊 {tokens_in}in/{tokens_out}out tokens | ${cost:.6f}")
 
    result = _safe_parse_json(raw, default={
        "conflicts":            [],
        "missing_requirements": [],
        "aligned_req_ids":      [],
        "summary":              "Parse error in meeting cross-reference"
    })
 
    return result, tokens_in, tokens_out, cost
 
 
# ══════════════════════════════════════════════════════════════
# REFLECTION — Generate improved requirement text
# ══════════════════════════════════════════════════════════════
 
def _tool_reflection(
    requirements:  list,
    issues_text:   str,
    quality_score: int,
    session:       dict
) -> tuple:
    """
    Given current requirements and their issues, generates improved text.
    Returns (result_dict, tokens_in, tokens_out, cost).
    """
    prompt_cfg = get_prompt("stages", "agent_reflection")
    model_cfg  = get_model_config("stages", "agent_reflection")
    prompt_ver = get_prompt_version("stages", "agent_reflection")
 
    reqs_text = _format_requirements_for_prompt(requirements)
    print(f"   Generating improvements (prompt v{prompt_ver})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    requirements=reqs_text,
                    issues=issues_text,
                    quality_score=quality_score,
                    threshold=QUALITY_THRESHOLD
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    raw        = response.choices[0].message.content.strip()
    usage      = response.usage
    tokens_in  = usage.prompt_tokens     if usage else 0
    tokens_out = usage.completion_tokens if usage else 0
    cost       = estimate_cost(tokens_in, tokens_out)
 
    print(f"   📊 {tokens_in}in/{tokens_out}out tokens | ${cost:.6f}")
 
    result = _safe_parse_json(raw, default={
        "improved_requirements": [],
        "unchanged_req_ids":     [],
        "improvement_rationale": "Parse error in reflection"
    })
 
    improved = result.get("improved_requirements", [])
    print(f"   Improvements generated: {len(improved)}")
 
    return result, tokens_in, tokens_out, cost
 
 
# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════
 
def _load_meeting_decisions() -> str:
    """
    Load all meeting decisions from Azure Blob and format for prompt.
    Returns empty string if no meetings exist.
    """
    try:
        meetings_list = list_meetings()
        if not meetings_list:
            return ""
 
        lines = []
        for m_summary in meetings_list[:10]:  # cap at 10 most recent meetings
            try:
                meeting = load_meeting(m_summary["meeting_id"])
                decisions = meeting.get("decisions", [])
                if decisions:
                    lines.append(f"\n[Meeting: {meeting.get('title', 'Untitled')}]")
                    for d in decisions:
                        lines.append(
                            f"  Decision: {d.get('decision', '')}"
                            f" | Owner: {d.get('owner', 'TBD')}"
                        )
            except Exception:
                continue
 
        return "\n".join(lines) if lines else ""
 
    except Exception as e:
        print(f"   ⚠️  Could not load meeting decisions: {e}")
        return ""
 
 
def _format_requirements_for_prompt(requirements: list) -> str:
    """Format requirements list as clean text for prompt injection."""
    lines = []
    for r in requirements:
        effective = r.get("effective_text") or r.get("text", "")
        lines.append(
            f"[{r['id']}] ({r.get('type', 'Unknown')}) {effective}\n"
            f"  Source: {r.get('source', 'unknown')} | "
            f"Confidence: {r.get('confidence', 'unknown')}"
        )
    return "\n\n".join(lines)
 
 
def _extract_issues_text(babok_result: dict) -> str:
    """
    Extract all issues from BABOK result into a flat text summary
    for use in the reflection prompt.
    """
    lines = []
    for req_score in babok_result.get("requirement_scores", []):
        req_id = req_score.get("req_id", "?")
        for issue in req_score.get("issues", []):
            lines.append(
                f"[{req_id}] {issue.get('dimension','?').upper()} "
                f"({issue.get('severity','?')} severity): "
                f"{issue.get('description','')}"
            )
        for contra in req_score.get("kb_contradictions", []):
            lines.append(
                f"[{req_id}] KB CONTRADICTION: {contra.get('description','')}"
            )
    return "\n".join(lines) if lines else "No specific issues identified."
 
 
def _score_to_confidence(score: int) -> str:
    """Convert quality score to confidence label."""
    if score >= 85:
        return "High"
    elif score >= 70:
        return "Medium"
    elif score >= 50:
        return "Low"
    else:
        return "Very Low"
 
 
def _safe_parse_json(raw: str, default: dict) -> dict:
    """Parse JSON from GPT response, stripping markdown fences if present."""
    try:
        text = raw
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        print(f"   ⚠️  JSON parse error: {e}")
        return default