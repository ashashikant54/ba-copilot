# hallucination_detector.py
# CoAnalytica — Real-time Hallucination Detection
#
# ═══════════════════════════════════════════════════════════════
# WHAT THIS FILE DOES
# ═══════════════════════════════════════════════════════════════
#
# Runs a lexical groundedness check on every extracted requirement
# to detect potential hallucinations before the BA sees them.
#
# APPROACH: Lexical Overlap (no GPT call)
# ─────────────────────────────────────────
# Checks whether key content — especially numbers, system names,
# and specific technical terms — from each requirement actually
# appears in the source context (KB chunks + Q&A answers).
#
# Why lexical rather than semantic?
#   - Zero cost — no GPT call needed
#   - Fast — runs inline during requirements extraction
#   - Catches the highest-risk hallucinations: invented SLAs,
#     invented system names, invented quantities
#   - Semantic hallucinations (paraphrased fabrications) are
#     caught by the LLM-as-Judge in the offline eval runner
#
# OUTPUT per requirement:
#   groundedness_score     0.0 to 1.0
#   verdict                "grounded" | "partial" | "ungrounded"
#   flagged_terms          list of terms not found in context
#   warning                human-readable warning if ungrounded
#
# SESSION INTEGRATION:
#   Results saved to session record as req_groundedness_scores.
#   Admin dashboard shows per-session hallucination rate.
#   OTel span attribute: coanalytica.hallucination.rate
 
import re
from typing import Optional
 
 
# ── Patterns that indicate high-risk content ──────────────────
# Numbers with units (SLAs, timeframes, percentages, quantities)
NUMBER_PATTERN = re.compile(
    r'\b\d+(?:\.\d+)?(?:\s*(?:second|minute|hour|day|week|month|year|'
    r'percent|%|ms|KB|MB|GB|TB|users|requests|calls|retries|'
    r'times|x|hours/year|uptime|availability))s?\b',
    re.IGNORECASE
)
 
# Proper nouns that look like system/product names
# (CamelCase, ALL_CAPS, or known patterns)
SYSTEM_NAME_PATTERN = re.compile(
    r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b|'   # CamelCase: ServiceNow, WorkDay
    r'\b[A-Z]{2,}\b|'                        # ACRONYMS: HRIS, VPN, SSO
    r'\bv\d+(?:\.\d+)*\b',                  # versions: v3.2, v2
    re.MULTILINE
)
 
 
def check_requirement_groundedness(
    requirement_text: str,
    source_context:   str,
    qa_context:       str = "",
) -> dict:
    """
    Check whether a single requirement is grounded in its source context.
 
    Args:
        requirement_text: The effective text of the requirement
                         (edited_text if BA edited, otherwise text)
        source_context:  KB chunks retrieved during the session
        qa_context:      Clarification + gap Q&A answers
 
    Returns dict with:
        groundedness_score  float 0.0-1.0
        verdict             str
        flagged_terms       list of terms not found in context
        warning             str or None
    """
    if not requirement_text or not requirement_text.strip():
        return {
            "groundedness_score": 1.0,
            "verdict":            "grounded",
            "flagged_terms":      [],
            "warning":            None
        }
 
    combined_context = f"{source_context}\n{qa_context}".lower()
    req_lower        = requirement_text.lower()
 
    flagged_terms = []
 
    # ── Check 1: Numbers with units ────────────────────────────
    # If a requirement contains "within 2 hours" or "99.99% uptime",
    # those specific values must appear somewhere in context.
    numbers_in_req = NUMBER_PATTERN.findall(requirement_text)
    for num_term in numbers_in_req:
        if num_term.lower() not in combined_context:
            flagged_terms.append({
                "term":   num_term,
                "reason": "numeric value not found in source context"
            })
 
    # ── Check 2: System/product names ─────────────────────────
    # CamelCase names and acronyms that appear in the requirement
    # but not in context are likely hallucinated.
    # Skip common English words and standard technical terms.
    ALLOWED_PATTERNS = {
        "The", "This", "When", "If", "All", "Any", "Each",
        "REST", "API", "JSON", "XML", "SQL", "HTTP", "HTTPS",
        "UI", "UX", "DB", "ID", "BA", "HR", "IT", "PM",
        "SLA", "SLO", "KPI", "OKR", "MVP", "POC",
    }
    system_names_in_req = SYSTEM_NAME_PATTERN.findall(requirement_text)
    for name in system_names_in_req:
        if name in ALLOWED_PATTERNS:
            continue
        if len(name) < 3:
            continue
        if name.lower() not in combined_context:
            flagged_terms.append({
                "term":   name,
                "reason": "system/product name not found in source context"
            })
 
    # ── Compute score ──────────────────────────────────────────
    total_checks = len(numbers_in_req) + len(
        [n for n in system_names_in_req if n not in ALLOWED_PATTERNS and len(n) >= 3]
    )
 
    if total_checks == 0:
        # No checkable terms — assume grounded (no evidence of hallucination)
        score   = 1.0
        verdict = "grounded"
        warning = None
    else:
        flagged_count = len(flagged_terms)
        score   = max(0.0, 1.0 - (flagged_count / total_checks))
        if score >= 0.8:
            verdict = "grounded"
            warning = None
        elif score >= 0.5:
            verdict = "partial"
            warning = (
                f"{flagged_count} term(s) not found in source context: "
                f"{', '.join(t['term'] for t in flagged_terms)}"
            )
        else:
            verdict = "ungrounded"
            warning = (
                f"⚠️ Potential hallucination: {flagged_count} term(s) "
                f"not supported by source context: "
                f"{', '.join(t['term'] for t in flagged_terms)}"
            )
 
    return {
        "groundedness_score": round(score, 3),
        "verdict":            verdict,
        "flagged_terms":      flagged_terms,
        "warning":            warning
    }
 
 
def check_requirements_batch(
    requirements:    list,
    kb_context:      str,
    qa_context:      str = "",
) -> dict:
    """
    Run hallucination check on all requirements in a session.
    Called after requirements extraction, before presenting to BA.
 
    Args:
        requirements: list of requirement dicts from session
        kb_context:   combined KB context used during extraction
        qa_context:   all clarification + gap Q&A answers
 
    Returns:
        {
          "per_requirement": {req_id: check_result},
          "hallucination_rate": float,  # 0.0 to 1.0
          "ungrounded_count": int,
          "partial_count": int,
          "grounded_count": int,
          "overall_verdict": str,
          "session_warning": str or None
        }
    """
    per_req   = {}
    ungrounded = 0
    partial    = 0
    grounded   = 0
 
    for req in requirements:
        if req.get("status") == "rejected":
            continue
 
        # Use edited_text if BA already improved the requirement
        effective_text = (
            req["edited_text"]
            if req.get("edited_text")
            else req.get("text", "")
        )
 
        result = check_requirement_groundedness(
            effective_text, kb_context, qa_context
        )
        per_req[req["id"]] = result
 
        if result["verdict"] == "ungrounded":
            ungrounded += 1
        elif result["verdict"] == "partial":
            partial += 1
        else:
            grounded += 1
 
    total = ungrounded + partial + grounded
    hallucination_rate = round(
        (ungrounded + partial * 0.5) / total if total > 0 else 0.0, 3
    )
 
    # Overall verdict
    if hallucination_rate == 0.0:
        overall = "clean"
        warning = None
    elif hallucination_rate <= 0.2:
        overall = "mostly_grounded"
        warning = (
            f"ℹ️ {partial} requirement(s) have terms not found in "
            f"source context. Review flagged items."
        )
    elif hallucination_rate <= 0.5:
        overall = "mixed"
        warning = (
            f"⚠️ {ungrounded + partial} requirement(s) may contain "
            f"unsupported claims. Please verify against source documents."
        )
    else:
        overall = "high_risk"
        warning = (
            f"🚨 High hallucination risk: {ungrounded} requirement(s) "
            f"contain claims not found in any source context. "
            f"Review carefully before approving."
        )
 
    return {
        "per_requirement":   per_req,
        "hallucination_rate": hallucination_rate,
        "ungrounded_count":  ungrounded,
        "partial_count":     partial,
        "grounded_count":    grounded,
        "total_checked":     total,
        "overall_verdict":   overall,
        "session_warning":   warning,
    }
 
 
def format_qa_context(session: dict) -> str:
    """
    Build a combined Q&A context string from a session for
    use in groundedness checking.
    """
    lines = []
 
    # Clarification Q&A
    questions = session.get("clarifying_questions", [])
    answers   = session.get("clarifying_answers", {})
    for q in questions:
        ans = answers.get(q["id"], "")
        if ans:
            lines.append(f"Q: {q['question']}\nA: {ans}")
 
    # Gap Q&A
    gap_questions = session.get("gap_questions", [])
    gap_answers   = session.get("gap_answers", {})
    for q in gap_questions:
        ans = gap_answers.get(q["id"], "")
        if ans:
            lines.append(f"Q: {q['question']}\nA: {ans}")
 
    return "\n\n".join(lines)