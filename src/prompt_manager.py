# prompt_manager.py
# Central prompt registry loader for CoAnalytica
#
# WHY THIS EXISTS:
#   Previously, prompts were hardcoded strings scattered across 6+ Python modules.
#   Problems with that approach:
#     - Can't version prompts (no rollback when quality drops)
#     - Can't A/B test prompt variants
#     - Can't see which prompt version produced a given session output
#     - Changing a prompt requires touching module code → risky deploys
#
# HOW IT WORKS:
#   All prompts live in prompts.json (same directory as this file).
#   Modules call get_prompt() to retrieve prompt text + metadata.
#   The returned dict includes version info for logging/observability.
#
# USAGE IN YOUR MODULES:
#   from prompt_manager import get_prompt, get_model_config
#
#   # Get system + user prompt for a stage
#   prompt = get_prompt("stages", "clarification")
#   system_msg  = prompt["system"]
#   user_msg    = prompt["user_template"].format(
#       problem_statement=problem,
#       system_name=system_name,
#       context=kb_context
#   )
#   version = prompt["version"]   # log this with the session for traceability
#
#   # Get model config
#   cfg = get_model_config("stages", "clarification")
#   model       = cfg["model"]        # "gpt-4o-mini"
#   temperature = cfg["temperature"]  # 0.2
#   max_tokens  = cfg["max_tokens"]   # 1500
 
import os
import json
import logging
from functools import lru_cache
from typing import Optional
 
logger = logging.getLogger(__name__)
 
# ── Path resolution ─────────────────────────────────────────────
# prompts.json lives in the same src/ directory as this file
_PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts.json")
 
# Fallback path (for local dev where you run from project root)
if not os.path.exists(_PROMPTS_FILE):
    _PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts.json")
 
 
# ── Loader ──────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _load_prompts() -> dict:
    """
    Load prompts.json once and cache in memory.
    lru_cache means the file is read once per process lifetime.
    To hot-reload during development: call _load_prompts.cache_clear()
    """
    if not os.path.exists(_PROMPTS_FILE):
        raise FileNotFoundError(
            f"prompts.json not found at: {_PROMPTS_FILE}\n"
            "Make sure prompts.json is in your src/ directory."
        )
    with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.info(f"✅ Loaded prompts.json v{data.get('_meta', {}).get('version', 'unknown')} "
                f"({len(data.get('stages', {}))} stages, "
                f"{len(data.get('meetings', {}))} meeting prompts)")
    return data
 
 
def reload_prompts():
    """
    Force reload prompts from disk.
    Useful in dev when you edit prompts.json and want changes
    without restarting the server.
    Call: POST /admin/reload-prompts (see main.py endpoint)
    """
    _load_prompts.cache_clear()
    return _load_prompts()
 
 
# ── Public API ──────────────────────────────────────────────────
 
def get_prompt(category: str, name: str) -> dict:
    """
    Get a prompt config by category and name.
 
    Args:
        category: Top-level key in prompts.json ("stages" or "meetings")
        name:     Prompt name within the category
                  e.g. "clarification", "analysis", "brd", "user_stories"
                  e.g. "analysis" (for meetings.analysis)
 
    Returns:
        dict with keys: version, model, temperature, max_tokens,
                        system, user_template
 
    Raises:
        KeyError: if category or name not found in prompts.json
 
    Example:
        prompt = get_prompt("stages", "clarification")
        user_msg = prompt["user_template"].format(
            problem_statement="...",
            system_name="HR System"
        )
    """
    prompts = _load_prompts()
    if category not in prompts:
        raise KeyError(
            f"Prompt category '{category}' not found. "
            f"Available: {list(prompts.keys())}"
        )
    if name not in prompts[category]:
        raise KeyError(
            f"Prompt '{name}' not found in '{category}'. "
            f"Available: {list(prompts[category].keys())}"
        )
    return prompts[category][name]
 
 
def get_system_prompt(category: str, name: str) -> str:
    """Shortcut: get just the system prompt string."""
    return get_prompt(category, name)["system"]
 
 
def get_user_template(category: str, name: str) -> str:
    """Shortcut: get just the user_template string."""
    return get_prompt(category, name)["user_template"]
 
 
def get_model_config(category: str, name: str) -> dict:
    """
    Get model configuration for a prompt.
 
    Returns:
        dict with keys: model, temperature, max_tokens, version
    """
    p = get_prompt(category, name)
    return {
        "model":       p.get("model", "gpt-4o-mini"),
        "temperature": p.get("temperature", 0.1),
        "max_tokens":  p.get("max_tokens", 2000),
        "version":     p.get("version", "unknown"),
    }
 
 
def get_prompt_version(category: str, name: str) -> str:
    """
    Get the version string for a prompt.
    Log this alongside every LLM call for traceability.
 
    Example:
        version = get_prompt_version("stages", "brd")
        # logs: "brd prompt v1.0.0 used for session abc123"
    """
    return get_prompt(category, name).get("version", "unknown")
 
 
def get_registry_meta() -> dict:
    """
    Get metadata about the prompts registry.
    Used by the admin dashboard to show current prompt versions.
 
    Returns:
        dict with: version, updated, model_default, all prompt versions
    """
    prompts = _load_prompts()
    meta = prompts.get("_meta", {})
 
    # Collect all prompt versions
    all_versions = {}
    for category, items in prompts.items():
        if category.startswith("_"):
            continue
        for name, config in items.items():
            key = f"{category}.{name}"
            all_versions[key] = {
                "version":     config.get("version", "unknown"),
                "model":       config.get("model", meta.get("model_default", "gpt-4o-mini")),
                "max_tokens":  config.get("max_tokens", 2000),
                "temperature": config.get("temperature", 0.1),
            }
 
    return {
        "registry_version": meta.get("version", "unknown"),
        "updated":          meta.get("updated", "unknown"),
        "model_default":    meta.get("model_default", "gpt-4o-mini"),
        "cost_per_1k_input":  meta.get("cost_per_1k_input_tokens", 0.000150),
        "cost_per_1k_output": meta.get("cost_per_1k_output_tokens", 0.000600),
        "prompts":          all_versions,
    }
 
 
# ── Cost Estimation Helper ──────────────────────────────────────
 
def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """
    Estimate the cost of an LLM call in USD.
    Uses rates from prompts.json _meta section.
 
    Args:
        input_tokens:  Number of input/prompt tokens
        output_tokens: Number of output/completion tokens
 
    Returns:
        Estimated cost in USD (float)
 
    Example:
        cost = estimate_cost(1200, 800)
        # → $0.000660 for a typical clarification call
    """
    meta = _load_prompts().get("_meta", {})
    input_rate  = meta.get("cost_per_1k_input_tokens",  0.000150)
    output_rate = meta.get("cost_per_1k_output_tokens", 0.000600)
    return round(
        (input_tokens  / 1000 * input_rate) +
        (output_tokens / 1000 * output_rate),
        6
    )