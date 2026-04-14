# brd_review_agent.py
# CoAnalytica — Feature 8: BRD Review Agent
#
# MULTI-AGENT COORDINATION:
#   This agent is the second agent in CoAnalytica.
#   Feature 7 (Requirements Validation Agent) validates requirements → Stage 5
#   Feature 8 (BRD Review Agent) validates the BRD built from those requirements → Stage 6
#
#   Coordination happens through session state:
#   - This agent reads agent_validation_score from Feature 7
#   - If requirements were poor quality going in, BRD review raises threshold
#   - Suggested fixes from Feature 7 that the BA applied are reflected in
#     the approved requirements this agent evaluates against
#
# TOOLS:
#   Tool 1: _tool_traceability_check()     — Python only, no GPT call
#     Checks every approved REQ-xxx ID appears in the BRD text.
#     Returns precise list of covered and missing requirement IDs.
#     Also extracts the stakeholder section from the BRD for Tool 3.
#
#   Tool 2: _tool_brd_quality_check()      — 1 GPT call per iteration
#     Evaluates BRD against 6 BABOK-aligned dimensions:
#     completeness, requirement coverage, clarity,
#     assumption validity, risk identification, success metrics.
#
#   Tool 3: _tool_stakeholder_alignment()  — 1 GPT call (once only)
#     Compares BRD stakeholder table against Stage 3 session stakeholders.
#     Finds missing stakeholders, wrong RACI levels, additions.
#
# REFLECTION LOOP:
#   If overall_quality_score < BRD_QUALITY_THRESHOLD (75):
#     → Run _tool_brd_reflection() to rewrite weak sections
#     → Re-run Tool 2 on improved BRD text to verify improvement
#     → Repeat up to MAX_ITERATIONS (3) times
#
# OUTPUT (saved to session + returned to API):
#   quality_score, passed, iterations, section_issues,
#   traceability_gaps, stakeholder_gaps, suggested_section_fixes,
#   confidence, summary
#
# NOTE on threshold:
#   BRD threshold (75) is higher than requirements threshold (70)
#   because a BRD is an outbound document reviewed by stakeholders —
#   quality standards are stricter.
 
import os
import sys
import re
import json
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from dotenv import load_dotenv
from openai import OpenAI
 
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
from session_manager import load_session, update_session
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
# ── Constants ──────────────────────────────────────────────────
BRD_QUALITY_THRESHOLD = 75   # Higher than requirements threshold (70)
MAX_ITERATIONS        = 3    # Max reflection loops
 
 
# ══════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════
 
def review_brd(session_id: str, org_id: str = None) -> dict:
    """
    Main agent entry point. Reviews the current brd_draft in the session.
    Returns full review result dict.
    """
    session = load_session(session_id, org_id=org_id)
 
    brd_text = session.get("brd_draft", "").strip()
    if not brd_text:
        raise ValueError(
            "No BRD draft found. Please generate the BRD preview first."
        )
 
    # Get approved requirements (accepted + edited, not rejected)
    all_reqs = session.get("requirements", [])
    approved_reqs = [
        r for r in all_reqs
        if r.get("status") in ("accepted", "edited")
    ]
 
    if not approved_reqs:
        raise ValueError(
            "No approved requirements found. "
            "Please complete Stage 5 requirements review first."
        )
 
    # Multi-agent coordination: read Feature 7 result
    f7_score = session.get("agent_validation_score")
    if f7_score is not None:
        print(f"\n🔗 Multi-agent context: Feature 7 requirements score was {f7_score}/100")
        # If requirements were borderline, apply stricter BRD threshold
        effective_threshold = BRD_QUALITY_THRESHOLD
        if f7_score < 70:
            effective_threshold = 80
            print(f"   ⚠️  Requirements were below threshold — raising BRD threshold to {effective_threshold}")
    else:
        effective_threshold = BRD_QUALITY_THRESHOLD
        print(f"\n🤖 BRD Review Agent starting (no Feature 7 data — threshold: {effective_threshold})")
 
    print(f"   Session: {session_id}")
    print(f"   BRD length: {len(brd_text):,} chars")
    print(f"   Approved requirements: {len(approved_reqs)}")
 
    # ── Observability accumulators ──────────────────────────────
    total_tokens_in  = 0
    total_tokens_out = 0
    total_cost       = 0.0
 
    # ── Tool 1: Traceability Check (Python, no GPT) ────────────
    print(f"\n🔧 Tool 1: Checking requirement traceability...")
    traceability = _tool_traceability_check(brd_text, approved_reqs)
    print(f"   Covered: {len(traceability['covered_ids'])} | "
          f"Missing: {len(traceability['missing_ids'])}")
 
    # ── Tool 3: Stakeholder Alignment (GPT, runs once) ─────────
    print(f"\n🔧 Tool 3: Checking stakeholder alignment...")
    stakeholder_result, t_in, t_out, cost = _tool_stakeholder_alignment(
        brd_text, session
    )
    total_tokens_in  += t_in
    total_tokens_out += t_out
    total_cost       += cost
    print(f"   Missing from BRD: {len(stakeholder_result.get('missing_from_brd', []))}")
    print(f"   Wrong involvement: {len(stakeholder_result.get('wrong_involvement', []))}")
 
    # ── Reflection loop: Tool 2 + optional brd_reflection ──────
    current_brd     = brd_text          # working copy — gets updated each iteration
    quality_result  = {}
    previous_issues = "None — first iteration"
    final_iteration = 1
 
    for iteration in range(1, MAX_ITERATIONS + 1):
        final_iteration = iteration
        print(f"\n🔧 Tool 2: BRD quality check (iteration {iteration}/{MAX_ITERATIONS})...")
 
        quality_result, t_in, t_out, cost = _tool_brd_quality_check(
            current_brd, approved_reqs, traceability, session,
            iteration, previous_issues
        )
        total_tokens_in  += t_in
        total_tokens_out += t_out
        total_cost       += cost
 
        quality_score = quality_result.get("overall_quality_score", 0)
        print(f"   Quality score: {quality_score}/100 (threshold: {effective_threshold})")
 
        # Decision: stop or reflect
        if quality_score >= effective_threshold:
            print(f"   ✅ Threshold met — stopping")
            break
 
        if iteration == MAX_ITERATIONS:
            print(f"   ⚠️  Max iterations reached — returning best result")
            break
 
        # Score below threshold — reflect and rewrite
        print(f"   🔄 Score {quality_score} < {effective_threshold} — rewriting weak sections...")
        issues_text     = _extract_section_issues_text(quality_result)
        previous_issues = issues_text
 
        reflection_result, t_in, t_out, cost = _tool_brd_reflection(
            current_brd, approved_reqs, issues_text, quality_score,
            effective_threshold, session
        )
        total_tokens_in  += t_in
        total_tokens_out += t_out
        total_cost       += cost
 
        # Apply section rewrites to current_brd
        improved_sections = reflection_result.get("improved_sections", [])
        if improved_sections:
            current_brd = _apply_section_rewrites(current_brd, improved_sections)
            print(f"   Sections rewritten: {len(improved_sections)}")
        else:
            print(f"   No sections rewritten — continuing")
 
    # ── Build suggested_section_fixes ──────────────────────────
    # Sections where the agent has an improved version
    suggested_section_fixes = []
    if current_brd != brd_text:
        # BRD text changed — collect what changed
        final_reflection = quality_result  # last iteration context
        suggested_section_fixes = _diff_brd_sections(brd_text, current_brd)
 
    final_score = quality_result.get("overall_quality_score", 0)
 
    result = {
        "quality_score":           final_score,
        "passed":                  final_score >= effective_threshold,
        "threshold_used":          effective_threshold,
        "iterations":              final_iteration,
        "dimension_scores":        quality_result.get("dimension_scores", {}),
        "section_issues":          quality_result.get("section_issues", []),
        "traceability": {
            "covered_ids":         traceability["covered_ids"],
            "missing_ids":         traceability["missing_ids"],
            "coverage_pct":        traceability["coverage_pct"],
        },
        "stakeholder_alignment":   stakeholder_result,
        "suggested_section_fixes": suggested_section_fixes,
        "improved_brd":            current_brd if current_brd != brd_text else None,
        "confidence":              _score_to_confidence(final_score),
        "summary":                 quality_result.get("summary", ""),
        "f7_requirements_score":   f7_score,
    }
 
    # ── Save to session ─────────────────────────────────────────
    prompt_ver = get_prompt_version("stages", "agent_brd_quality")
    update_session(session_id, {
        "brd_review_result":     result,
        "brd_review_score":      final_score,
        "brd_review_iterations": final_iteration,
        "brd_review_prompt_ver": prompt_ver,
        "brd_review_tokens_in":  total_tokens_in,
        "brd_review_tokens_out": total_tokens_out,
        "brd_review_cost_usd":   round(total_cost, 6),
    }, org_id=org_id)
 
    print(f"\n✅ BRD Review Agent complete: "
          f"score={final_score}, iterations={final_iteration}, "
          f"fixes={len(suggested_section_fixes)}, cost=${total_cost:.6f}")
 
    return result
 
 
# ══════════════════════════════════════════════════════════════
# TOOL 1 — TRACEABILITY CHECK (Python only, no GPT call)
# ══════════════════════════════════════════════════════════════
 
def _tool_traceability_check(brd_text: str, approved_reqs: list) -> dict:
    """
    Check every approved REQ-xxx ID appears in the BRD text.
    Also extract the stakeholder section for Tool 3.
    Returns traceability dict with covered/missing IDs and coverage %.
    """
    covered_ids = []
    missing_ids = []
 
    for req in approved_reqs:
        req_id = req["id"]  # e.g. "REQ-001"
        # Check both the ID and common variations (REQ-1, req-001 etc.)
        pattern = re.compile(re.escape(req_id), re.IGNORECASE)
        if pattern.search(brd_text):
            covered_ids.append(req_id)
        else:
            missing_ids.append({
                "req_id":  req_id,
                "type":    req.get("type", ""),
                "text":    (req["edited_text"] if req.get("edited_text")
                            else req.get("text", ""))[:100]
            })
 
    total       = len(approved_reqs)
    covered_n   = len(covered_ids)
    coverage_pct = round((covered_n / total * 100) if total > 0 else 0)
 
    # Build human-readable traceability findings for Tool 2 prompt
    findings_lines = [f"Coverage: {covered_n}/{total} requirements ({coverage_pct}%)"]
    if missing_ids:
        findings_lines.append(f"\nMISSING from BRD ({len(missing_ids)}):")
        for m in missing_ids:
            findings_lines.append(f"  [{m['req_id']}] ({m['type']}) {m['text']}")
    else:
        findings_lines.append("All approved requirements appear in BRD ✅")
 
    return {
        "covered_ids":    covered_ids,
        "missing_ids":    missing_ids,
        "coverage_pct":   coverage_pct,
        "findings_text":  "\n".join(findings_lines)
    }
 
 
# ══════════════════════════════════════════════════════════════
# TOOL 2 — BRD QUALITY CHECK (1 GPT call per iteration)
# ══════════════════════════════════════════════════════════════
 
def _tool_brd_quality_check(
    brd_text:            str,
    approved_reqs:       list,
    traceability:        dict,
    session:             dict,
    iteration:           int,
    previous_issues:     str
) -> tuple:
    """
    Evaluates BRD against 6 quality dimensions.
    Returns (result_dict, tokens_in, tokens_out, cost).
    """
    prompt_cfg = get_prompt("stages", "agent_brd_quality")
    model_cfg  = get_model_config("stages", "agent_brd_quality")
    prompt_ver = get_prompt_version("stages", "agent_brd_quality")
 
    # Format approved requirements for prompt
    reqs_text = _format_approved_reqs(approved_reqs)
    print(f"   Running BRD quality check (prompt v{prompt_ver}, {model_cfg['model']})...")
 
    # Escape { and } in brd_text so Python .format() doesn't mistake them
    # for format variables. BRD contains markdown tables and traceability
    # matrices with curly braces that would cause KeyError otherwise.
    safe_brd = brd_text[:8000].replace("{", "{{").replace("}", "}}")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    iteration=iteration,
                    approved_requirements=reqs_text,
                    traceability_findings=traceability["findings_text"],
                    brd_text=safe_brd,
                    previous_issues=previous_issues
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
        "overall_quality_score": 0,
        "dimension_scores":      {},
        "section_issues":        [],
        "summary":               "Parse error in BRD quality check"
    })
    return result, tokens_in, tokens_out, cost
 
 
# ══════════════════════════════════════════════════════════════
# TOOL 3 — STAKEHOLDER ALIGNMENT (1 GPT call, once only)
# ══════════════════════════════════════════════════════════════
 
def _tool_stakeholder_alignment(brd_text: str, session: dict) -> tuple:
    """
    Compares BRD stakeholder section against Stage 3 session stakeholders.
    Returns (result_dict, tokens_in, tokens_out, cost).
    """
    analysis_stakeholders = session.get("impacted_stakeholders", [])
 
    if not analysis_stakeholders:
        print("   No Stage 3 stakeholders found — skipping alignment check")
        return {
            "missing_from_brd":         [],
            "wrong_involvement":         [],
            "not_in_analysis":           [],
            "correctly_represented":     [],
            "summary": "No Stage 3 stakeholders available for comparison."
        }, 0, 0, 0.0
 
    prompt_cfg = get_prompt("stages", "agent_stakeholder_alignment")
    model_cfg  = get_model_config("stages", "agent_stakeholder_alignment")
    prompt_ver = get_prompt_version("stages", "agent_stakeholder_alignment")
 
    # Format analysis stakeholders
    analysis_text = "\n".join(
        f"- {s['name']} ({s.get('team','')}) | "
        f"Involvement: {s.get('involvement','')} | "
        f"Impact: {s.get('impact_level','')} | "
        f"Reason: {s.get('reason','')}"
        for s in analysis_stakeholders
    )
 
    # Extract stakeholder section from BRD (section 5)
    brd_stakeholder_section = _extract_brd_section(brd_text, "STAKEHOLDERS")
 
    print(f"   Checking stakeholder alignment (prompt v{prompt_ver})...")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    analysis_stakeholders=analysis_text,
                    brd_stakeholder_section=(
                        brd_stakeholder_section or "Stakeholder section not found in BRD"
                    ).replace("{", "{{").replace("}", "}}")
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
        "missing_from_brd":     [],
        "wrong_involvement":    [],
        "not_in_analysis":      [],
        "correctly_represented": [],
        "summary": "Parse error in stakeholder alignment"
    })
    return result, tokens_in, tokens_out, cost
 
 
# ══════════════════════════════════════════════════════════════
# REFLECTION — Rewrite weak BRD sections
# ══════════════════════════════════════════════════════════════
 
def _tool_brd_reflection(
    brd_text:          str,
    approved_reqs:     list,
    issues_text:       str,
    quality_score:     int,
    threshold:         int,
    session:           dict
) -> tuple:
    """
    Rewrites specific weak BRD sections to improve quality score.
    Returns (result_dict, tokens_in, tokens_out, cost).
    """
    prompt_cfg = get_prompt("stages", "agent_brd_reflection")
    model_cfg  = get_model_config("stages", "agent_brd_reflection")
    prompt_ver = get_prompt_version("stages", "agent_brd_reflection")
 
    reqs_text = _format_approved_reqs(approved_reqs)
    print(f"   Rewriting weak sections (prompt v{prompt_ver})...")
 
    # Escape { and } in brd_text — same reason as _tool_brd_quality_check
    safe_brd = brd_text[:8000].replace("{", "{{").replace("}", "}}")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    approved_requirements=reqs_text,
                    issues=issues_text,
                    brd_text=safe_brd,
                    quality_score=quality_score,
                    threshold=threshold
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
        "improved_sections":     [],
        "unchanged_sections":    [],
        "improvement_rationale": "Parse error in BRD reflection"
    })
    return result, tokens_in, tokens_out, cost
 
 
# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════
 
def _apply_section_rewrites(brd_text: str, improved_sections: list) -> str:
    """
    Apply section rewrites to BRD text.
    Finds each section by its header and replaces until the next ##.
    Falls back gracefully if section not found.
    """
    current = brd_text
    for section in improved_sections:
        header   = section.get("section_header", "").strip()
        new_text = section.get("improved_text", "").strip()
        if not header or not new_text:
            continue
 
        # Find section start
        header_escaped = re.escape(header)
        match = re.search(header_escaped, current, re.IGNORECASE)
        if not match:
            print(f"   ⚠️  Section not found for rewrite: {header}")
            continue
 
        start = match.start()
 
        # Find next ## section header (or end of document)
        next_section = re.search(r'\n##\s', current[match.end():])
        if next_section:
            end = match.end() + next_section.start()
        else:
            end = len(current)
 
        # Replace old section content with new
        current = current[:start] + header + "\n" + new_text + "\n" + current[end:]
        print(f"   ✏️  Rewrote: {header}")
 
    return current
 
 
def _diff_brd_sections(original: str, improved: str) -> list:
    """
    Compare original and improved BRD, return list of changed sections
    with original and improved text for display in UI.
    """
    changes = []
    # Split both into sections by ## headers
    orig_sections = re.split(r'(?=\n## )', original)
    impr_sections = re.split(r'(?=\n## )', improved)
 
    # Match sections by header line
    orig_map = {}
    for s in orig_sections:
        header_match = re.match(r'\n?(## .*?)[\n$]', s)
        if header_match:
            orig_map[header_match.group(1).strip()] = s.strip()
 
    for s in impr_sections:
        header_match = re.match(r'\n?(## .*?)[\n$]', s)
        if not header_match:
            continue
        header = header_match.group(1).strip()
        orig   = orig_map.get(header, "")
        if s.strip() != orig:
            changes.append({
                "section":       header,
                "original_text": orig[:300],
                "improved_text": s.strip()[:300],
            })
 
    return changes
 
 
def _extract_brd_section(brd_text: str, section_keyword: str) -> str:
    """Extract a specific section from the BRD by keyword in its header."""
    pattern = re.compile(
        rf'(##\s+\d*\.?\s*{re.escape(section_keyword)}.*?)(?=\n##\s|\Z)',
        re.IGNORECASE | re.DOTALL
    )
    match = pattern.search(brd_text)
    return match.group(1).strip() if match else ""
 
 
def _extract_section_issues_text(quality_result: dict) -> str:
    """Flatten section issues into plain text for reflection prompt."""
    lines = []
    for issue in quality_result.get("section_issues", []):
        lines.append(
            f"[{issue.get('section','?')}] "
            f"{issue.get('dimension','?').upper()} "
            f"({issue.get('severity','?')} severity): "
            f"{issue.get('description','')}"
        )
    return "\n".join(lines) if lines else "No specific issues identified."
 
 
def _format_approved_reqs(approved_reqs: list) -> str:
    """Format approved requirements list for prompt injection."""
    lines = []
    for r in approved_reqs:
        effective = r["edited_text"] if r.get("edited_text") else r["text"]
        lines.append(
            f"[{r['id']}] ({r.get('type','')}) {effective}\n"
            f"  Rationale: {r.get('rationale','')}"
        )
    return "\n\n".join(lines)
 
 
def _score_to_confidence(score: int) -> str:
    if score >= 85:
        return "High"
    elif score >= 75:
        return "Medium"
    elif score >= 60:
        return "Low"
    else:
        return "Very Low"
 
 
def _safe_parse_json(raw: str, default: dict) -> dict:
    """
    Parse JSON from LLM response. ALWAYS returns a dict — never a string.
    Handles markdown fences, partial JSON, and unexpected return types.
    """
    if not isinstance(raw, str):
        print(f"   ⚠️  _safe_parse_json got non-string: {type(raw)}")
        return default
    try:
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            for part in parts[1::2]:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                try:
                    result = json.loads(part)
                    if isinstance(result, dict):
                        return result
                except Exception:
                    continue
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        print(f"   ⚠️  JSON parsed but not a dict: {type(result)}")
        return default
    except Exception as e:
        print(f"   ⚠️  JSON parse error: {e} | raw[:100]: {raw[:100]}")
        return default