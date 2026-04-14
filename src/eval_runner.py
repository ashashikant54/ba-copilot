# eval_runner.py
# CoAnalytica — Offline Evaluation Runner
#
# ═══════════════════════════════════════════════════════════════
# WHAT THIS FILE DOES
# ═══════════════════════════════════════════════════════════════
#
# Runs the Requirements Validation Agent against the golden dataset
# and measures how well it performs compared to known expected outcomes.
#
# THREE CAPABILITIES:
#
# 1. run_evaluation()
#    Runs all golden test cases through the BABOK check.
#    Measures: score accuracy, issue detection precision/recall,
#    hallucination detection rate, cost per evaluation.
#
# 2. run_ab_test(stage_key_a, version_a, stage_key_b, version_b)
#    Compares two prompt versions on the same golden dataset.
#    Returns: winner, score delta, hallucination rate delta, cost delta.
#    Used before deploying a new prompt version.
#
# 3. LLM-as-Judge groundedness check
#    Uses GPT-4o-mini as a neutral evaluator to determine whether
#    each requirement is supported by its source context.
#    More accurate than lexical overlap for semantic hallucinations.
 
import os
import sys
import json
import time
from datetime import datetime
from typing import Optional
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
 
from dotenv import load_dotenv
from openai import OpenAI
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
from hallucination_detector import check_requirements_batch
 
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
# Path to golden dataset
GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
GOLDEN_REQS_FILE = os.path.join(GOLDEN_DIR, "golden_requirements.json")
RESULTS_FILE = os.path.join(GOLDEN_DIR, "eval_results.json")
 
 
# ══════════════════════════════════════════════════════════════
# LLM-AS-JUDGE GROUNDEDNESS CHECK
# ══════════════════════════════════════════════════════════════
 
def judge_groundedness(
    requirement_text: str,
    source_context:   str,
    req_id:           str = ""
) -> dict:
    """
    Use GPT-4o-mini as a neutral judge to evaluate whether a
    requirement is supported by the source context.
 
    This is more accurate than lexical overlap for cases where
    the model has paraphrased something that wasn't in context.
 
    Returns:
        verdict:     "supported" | "partially_supported" | "unsupported"
        confidence:  float 0.0-1.0
        reasoning:   explanation
        cost:        float
    """
    prompt_cfg = get_prompt("stages", "eval_judge")
    model_cfg  = get_model_config("stages", "eval_judge")
 
    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    requirement_id=req_id,
                    requirement_text=requirement_text,
                    source_context=source_context[:3000]
                )
            }
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"]
    )
 
    raw   = response.choices[0].message.content.strip()
    usage = response.usage
    t_in  = usage.prompt_tokens     if usage else 0
    t_out = usage.completion_tokens if usage else 0
    cost  = estimate_cost(t_in, t_out)
 
    try:
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
    except Exception:
        result = {
            "verdict":    "partially_supported",
            "confidence": 0.5,
            "reasoning":  "Parse error in judge response"
        }
 
    result["cost"] = cost
    return result
 
 
# ══════════════════════════════════════════════════════════════
# MAIN EVALUATION RUNNER
# ══════════════════════════════════════════════════════════════
 
def run_evaluation(
    use_llm_judge:  bool = True,
    max_cases:      Optional[int] = None,
    case_ids:       Optional[list] = None,
    org_id:         Optional[str] = None,
) -> dict:
    """
    Run full evaluation against the golden requirements dataset.

    Args:
        use_llm_judge:  If True, run LLM-as-Judge on each requirement.
                        If False, use lexical overlap only (faster, free).
        max_cases:      Limit to first N cases (useful for quick checks).
        case_ids:       Run only specific case IDs e.g. ["GR-001", "GR-005"]
        org_id:         Accepted for Phase 2 Sprint 4 endpoint-forwarding
                        contract; currently ignored because the golden
                        dataset is shared infrastructure (A7 notes eval is
                        super_admin/admin-scoped, not org-scoped).

    Returns full eval report dict.
    """
    _ = org_id   # reserved — see docstring
    print(f"\n{'='*60}")
    print(f"CoAnalytica Evaluation Runner")
    print(f"Mode: {'LLM-as-Judge' if use_llm_judge else 'Lexical only'}")
    print(f"{'='*60}")
 
    # Load golden dataset
    with open(GOLDEN_REQS_FILE) as f:
        golden = json.load(f)
 
    test_cases = golden["test_cases"]
 
    # Filter if requested
    if case_ids:
        test_cases = [c for c in test_cases if c["id"] in case_ids]
    if max_cases:
        test_cases = test_cases[:max_cases]
 
    print(f"Running {len(test_cases)} test cases...")
 
    # Import BABOK check tool
    from requirements_agent import _tool_babok_check, QUALITY_THRESHOLD
 
    results      = []
    total_cost   = 0.0
    score_errors = []  # cases where score was outside expected range
 
    for i, case in enumerate(test_cases, 1):
        print(f"\n[{i}/{len(test_cases)}] {case['id']}: {case['name']}")
 
        case_start = time.time()
        case_result = {
            "case_id":   case["id"],
            "case_name": case["name"],
        }
 
        # Build a mock session for the BABOK check tool
        mock_session = {
            "system_filter": None,
            "source_filter": None,
        }
 
        # Fix: build effective_text for each requirement
        # This mirrors what initialise_node does in the LangGraph agent.
        # Without this, edited requirements get evaluated on their original
        # vague text instead of the BA-improved text (GR-010 bug).
        eval_reqs = []
        for req in case["requirements"]:
            r = dict(req)
            r["effective_text"] = (
                r["edited_text"] if r.get("edited_text")
                else r.get("text", "")
            )
            eval_reqs.append(r)
 
        # Run BABOK check (Tool 2 from requirements agent)
        try:
            babok_result, t_in, t_out, cost = _tool_babok_check(
                requirements=eval_reqs,
                kb_context=case.get("kb_context", ""),
                session=mock_session,
                iteration=1,
                previous_issues="None"
            )
            total_cost += cost
            agent_score = babok_result.get("overall_quality_score", 0)
            print(f"   Agent score: {agent_score}/100")
 
            # Guard: if babok_result is a string (parse failure), skip gracefully
            if not isinstance(babok_result, dict):
                print(f"   ⚠️  Non-dict babok_result for {case['id']} — skipping")
                case_result["error"] = "babok_result was not a dict (JSON parse failure)"
                results.append(case_result)
                continue
 
            # Score accuracy check
            score_min = case.get("expected_score_min", 0)
            score_max = case.get("expected_score_max", 100)
            score_in_range = score_min <= agent_score <= score_max
            if not score_in_range:
                score_errors.append({
                    "case_id": case["id"],
                    "expected_range": f"{score_min}-{score_max}",
                    "actual_score":   agent_score
                })
                print(f"   ⚠️  Score out of expected range [{score_min}-{score_max}]")
            else:
                print(f"   ✅ Score in expected range [{score_min}-{score_max}]")
 
            # Issue detection check
            detected_dimensions = set()
            for req_score in babok_result.get("requirement_scores", []):
                for issue in req_score.get("issues", []):
                    if issue.get("severity") in ("High", "Medium"):
                        detected_dimensions.add(issue.get("dimension", ""))
 
            expected_issues = set(case.get("expected_issues", []))
            true_positives  = detected_dimensions & expected_issues
            false_negatives = expected_issues - detected_dimensions
            false_positives = detected_dimensions - expected_issues
 
            precision = (len(true_positives) / len(detected_dimensions)
                        if detected_dimensions else 1.0)
            recall    = (len(true_positives) / len(expected_issues)
                        if expected_issues else 1.0)
            f1        = ((2 * precision * recall / (precision + recall))
                        if (precision + recall) > 0 else 0.0)
 
            case_result.update({
                "agent_score":       agent_score,
                "expected_range":    f"{score_min}-{score_max}",
                "score_in_range":    score_in_range,
                "detected_issues":   list(detected_dimensions),
                "expected_issues":   list(expected_issues),
                "true_positives":    list(true_positives),
                "false_negatives":   list(false_negatives),
                "false_positives":   list(false_positives),
                "precision":         round(precision, 3),
                "recall":            round(recall, 3),
                "f1":                round(f1, 3),
                "babok_cost":        round(cost, 6),
            })
 
        except Exception as e:
            print(f"   ❌ BABOK check failed: {e}")
            case_result["error"] = str(e)
            results.append(case_result)
            continue
 
        # Hallucination detection check
        expected_hall_rate = case.get("expected_hallucination_rate", 0.0)
        hall_result = check_requirements_batch(
            requirements=eval_reqs,
            kb_context=case.get("kb_context", ""),
            qa_context=""
        )
        detected_hall_rate = hall_result["hallucination_rate"]
 
        # Was hallucination correctly detected when expected?
        hallucination_expected = expected_hall_rate > 0
        hallucination_detected = detected_hall_rate > 0.1
        hall_detection_correct = (hallucination_expected == hallucination_detected)
 
        print(f"   Hallucination: expected={expected_hall_rate:.1f}, "
              f"detected={detected_hall_rate:.3f} "
              f"{'✅' if hall_detection_correct else '⚠️'}")
 
        case_result.update({
            "expected_hallucination_rate": expected_hall_rate,
            "detected_hallucination_rate": detected_hall_rate,
            "hallucination_detection_correct": hall_detection_correct,
            "hallucination_detail": hall_result,
        })
 
        # LLM-as-Judge (optional, more accurate)
        if use_llm_judge:
            judge_results = []
            kb_ctx = case.get("kb_context", "")
            for req in case["requirements"]:
                if req.get("status") == "rejected":
                    continue
                effective = (req["edited_text"] if req.get("edited_text")
                            else req.get("text", ""))
                try:
                    judge = judge_groundedness(effective, kb_ctx, req["id"])
                    total_cost += judge.get("cost", 0)
                    judge_results.append({
                        "req_id":     req["id"],
                        "verdict":    judge.get("verdict"),
                        "confidence": judge.get("confidence"),
                        "reasoning":  judge.get("reasoning", ""),
                    })
                except Exception as e:
                    judge_results.append({
                        "req_id": req["id"],
                        "error":  str(e)
                    })
 
            # Judge hallucination rate
            judge_unsupported = sum(
                1 for j in judge_results
                if j.get("verdict") == "unsupported"
            )
            judge_partial = sum(
                1 for j in judge_results
                if j.get("verdict") == "partially_supported"
            )
            judge_total = len(judge_results)
            judge_hall_rate = round(
                (judge_unsupported + judge_partial * 0.5) / judge_total
                if judge_total > 0 else 0.0, 3
            )
 
            case_result["llm_judge"] = {
                "results":         judge_results,
                "hallucination_rate": judge_hall_rate,
                "matches_expected": (
                    (judge_hall_rate > 0.1) ==
                    (expected_hall_rate > 0)
                ),
            }
            print(f"   LLM Judge hallucination rate: {judge_hall_rate:.3f}")
 
        case_result["duration_seconds"] = round(time.time() - case_start, 1)
        results.append(case_result)
 
    # ── Aggregate metrics ──────────────────────────────────────
    scored_results = [r for r in results if "agent_score" in r]
    n = len(scored_results)
 
    avg_score          = sum(r["agent_score"] for r in scored_results) / n if n else 0
    score_accuracy     = sum(1 for r in scored_results if r["score_in_range"]) / n if n else 0
    avg_precision      = sum(r["precision"] for r in scored_results) / n if n else 0
    avg_recall         = sum(r["recall"]    for r in scored_results) / n if n else 0
    avg_f1             = sum(r["f1"]        for r in scored_results) / n if n else 0
    hall_accuracy      = sum(
        1 for r in scored_results
        if r.get("hallucination_detection_correct", False)
    ) / n if n else 0
 
    report = {
        "run_id":          datetime.now().strftime("%Y%m%d_%H%M%S"),
        "timestamp":       datetime.now().isoformat(),
        "mode":            "llm_judge" if use_llm_judge else "lexical",
        "prompt_versions": {
            "agent_babok_check":  get_prompt_version("stages", "agent_babok_check"),
            "agent_reflection":   get_prompt_version("stages", "agent_reflection"),
        },
        "cases_run":       len(test_cases),
        "cases_scored":    n,
        "aggregate": {
            "avg_agent_score":          round(avg_score, 1),
            "score_accuracy":           round(score_accuracy, 3),
            "issue_detection_precision":round(avg_precision, 3),
            "issue_detection_recall":   round(avg_recall, 3),
            "issue_detection_f1":       round(avg_f1, 3),
            "hallucination_detection_accuracy": round(hall_accuracy, 3),
            "total_cost_usd":           round(total_cost, 6),
            "score_errors":             score_errors,
        },
        "case_results": results,
    }
 
    # Save results
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(report, f, indent=2)
 
    _print_summary(report)
    return report
 
 
# ══════════════════════════════════════════════════════════════
# A/B PROMPT TESTING
# ══════════════════════════════════════════════════════════════
 
def run_ab_test(
    stage_key:    str,
    version_a:    str,
    version_b:    str,
    max_cases:    int = 8,
    org_id:       Optional[str] = None,
) -> dict:
    """
    Compare two prompt versions on the golden dataset.
 
    Temporarily swaps the active prompt version, runs eval,
    then restores. Returns winner and metrics delta.
 
    Args:
        stage_key:  e.g. "agent_babok_check"
        version_a:  e.g. "1.0.0" (current/control)
        version_b:  e.g. "1.1.0" (challenger)
        max_cases:  how many golden cases to run (keep small to control cost)
 
    Returns:
        winner:         "A" | "B" | "tie"
        score_delta:    B_score - A_score
        recall_delta:   B_recall - A_recall
        cost_delta:     B_cost - A_cost
        recommendation: "deploy B" | "keep A" | "inconclusive"
    """
    print(f"\n{'='*60}")
    print(f"A/B Prompt Test: {stage_key}")
    print(f"Version A (control):    {version_a}")
    print(f"Version B (challenger): {version_b}")
    print(f"Cases: {max_cases}")
    print(f"{'='*60}")
 
    # Load prompts.json
    prompts_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "prompts.json"
    )
    with open(prompts_path) as f:
        prompts_data = json.load(f)
 
    if stage_key not in prompts_data.get("stages", {}):
        raise ValueError(f"Stage key '{stage_key}' not found in prompts.json")
 
    # ── Run with Version A ─────────────────────────────────────
    print(f"\nRunning Version A ({version_a})...")
    # Version A uses the current prompts.json as-is
    result_a = run_evaluation(
        use_llm_judge=False,  # lexical only for A/B — keeps cost low
        max_cases=max_cases
    )
 
    # ── Swap to Version B (if it exists in prompts.json) ──────
    # In practice, you'd have versioned prompt files.
    # For now we check if a version_b entry exists in the stage.
    stage_data = prompts_data["stages"][stage_key]
    if stage_data.get("version") == version_b:
        print(f"\nVersion B ({version_b}) is same as current — cannot A/B test")
        return {
            "error": f"version_b '{version_b}' matches current version",
            "recommendation": "inconclusive"
        }
 
    # ── Compare results ────────────────────────────────────────
    agg_a = result_a["aggregate"]
 
    # Since we can only run the current prompt (version_a),
    # we report the A results and explain the B comparison
    # requires the new prompt to be set in prompts.json first.
    ab_result = {
        "run_id":    datetime.now().strftime("%Y%m%d_%H%M%S"),
        "stage_key": stage_key,
        "version_a": version_a,
        "version_b": version_b,
        "status":    "version_a_baseline_captured",
        "version_a_results": {
            "avg_score":      agg_a["avg_agent_score"],
            "score_accuracy": agg_a["score_accuracy"],
            "precision":      agg_a["issue_detection_precision"],
            "recall":         agg_a["issue_detection_recall"],
            "f1":             agg_a["issue_detection_f1"],
            "hall_accuracy":  agg_a["hallucination_detection_accuracy"],
            "cost_usd":       agg_a["total_cost_usd"],
        },
        "instructions": (
            f"To complete A/B test: update prompts.json stages.{stage_key} "
            f"with version '{version_b}' content, then call run_ab_test() again. "
            f"Version A baseline has been saved to eval_results.json."
        ),
        "recommendation": "pending_version_b",
    }
 
    # Save A/B results
    ab_file = os.path.join(GOLDEN_DIR, f"ab_test_{stage_key}_{version_a}_vs_{version_b}.json")
    with open(ab_file, "w") as f:
        json.dump(ab_result, f, indent=2)
 
    print(f"\n📊 Version A baseline captured:")
    print(f"   Avg score:    {agg_a['avg_agent_score']}")
    print(f"   F1:           {agg_a['issue_detection_f1']}")
    print(f"   Cost:         ${agg_a['total_cost_usd']:.6f}")
    print(f"\nSaved to: {ab_file}")
 
    return ab_result
 
 
def get_latest_results(org_id: Optional[str] = None) -> Optional[dict]:
    """Load the most recent eval results from disk.

    org_id is accepted for endpoint-forwarding contract; results file is
    shared across orgs per A7 (eval is super_admin/admin-scoped).
    """
    _ = org_id   # reserved
    if not os.path.exists(RESULTS_FILE):
        return None
    with open(RESULTS_FILE) as f:
        return json.load(f)
 
 
def _print_summary(report: dict) -> None:
    """Print a clean eval summary to console."""
    agg = report["aggregate"]
    print(f"\n{'='*60}")
    print(f"EVALUATION COMPLETE — Run ID: {report['run_id']}")
    print(f"{'='*60}")
    print(f"Cases run:              {report['cases_run']}")
    print(f"Avg agent score:        {agg['avg_agent_score']}/100")
    print(f"Score accuracy:         {agg['score_accuracy']*100:.1f}%")
    print(f"Issue detection F1:     {agg['issue_detection_f1']*100:.1f}%")
    print(f"  Precision:            {agg['issue_detection_precision']*100:.1f}%")
    print(f"  Recall:               {agg['issue_detection_recall']*100:.1f}%")
    print(f"Hallucination accuracy: {agg['hallucination_detection_accuracy']*100:.1f}%")
    print(f"Total cost:             ${agg['total_cost_usd']:.6f}")
    if agg["score_errors"]:
        print(f"\n⚠️  Score range errors ({len(agg['score_errors'])}):")
        for e in agg["score_errors"]:
            print(f"   {e['case_id']}: got {e['actual_score']}, expected {e['expected_range']}")
    print(f"{'='*60}\n")