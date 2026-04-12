# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project: CoAnalytica (ba-copilot)

FastAPI backend + single-file static frontend that takes a raw business problem and drives it through an 8-stage Business Analyst workflow, producing a BRD and user stories. All code lives in `src/`; `static/index.html` is the complete UI.

## Running the app

```bash
# Windows (PowerShell/bash via Git Bash)
venv/Scripts/activate                                   # or start_project.bat
python -m uvicorn src.main:app --reload --port 8000     # local dev
```

Production (Azure App Service) entrypoint is `startup.sh`:
```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 2
```

There is **no test suite and no linter configured**. `src/test_azure_search.py` and `src/test_openai.py` are smoke scripts (plain `python src/test_azure_search.py`), not pytest tests. Several modules have `if __name__ == "__main__":` blocks used as manual integration tests (e.g. `python src/session_manager.py`).

Deployment is GitHub Actions → Azure Web App `ba-copilot-app` on every push to `main` (`.github/workflows/main_ba-copilot-app.yml`). Oryx builds from `requirements.txt` during deployment.

## Required environment (`.env` at repo root, loaded by `python-dotenv`)

Every module uses these — the app will not boot without them:

- `OPENAI_API_KEY` — all LLM + embedding calls (`gpt-4o-mini`, `text-embedding-3-small`)
- `AZURE_SEARCH_ENDPOINT` / `AZURE_SEARCH_KEY` / `AZURE_SEARCH_INDEX` — vector KB (`embedder.py`, `retriever.py`)
- `AZURE_STORAGE_CONNECTION_STRING` — **sessions and meetings are stored in Azure Blob containers `sessions` and `meetings`, not on disk**. `session_manager.py` will raise on startup if missing.
- `APPLICATIONINSIGHTS_CONNECTION_STRING` — OTel exporter (falls back to console if unset)
- `REDIS_CONNECTION_STRING` — semantic cache (optional; `semantic_cache.py` degrades gracefully to direct GPT calls if Redis is down)
- `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` (default `0.93`), `SEMANTIC_CACHE_TTL_HOURS` (default `168`)

The `sessions/`, `uploads/`, `.chroma/` folders in the repo are legacy — **blob storage is the source of truth for sessions** now. ChromaDB was replaced by Azure AI Search.

## Architecture (the big picture)

### The 8-stage session pipeline

Every BA request is a `session` (8-char UUID) that walks through stages stored as integer `stage` in the session JSON. Stage constants live in `session_manager.py`:

1. Problem Definition → 2. Clarification → 3. System & Stakeholder Analysis → 4. Gap Filling → 5. Requirements Review → 6. BRD Preview → 7. User Stories → 8. Complete

Each stage has its own module under `src/`:
`clarification_module.py`, `analysis_module.py`, `gap_module.py`, `requirements_module.py`, `brd_module.py`, `stories_module.py`. Modules read `session` via `load_session`, mutate specific fields, call `update_session` / `_save_session`. Stage transitions are explicit (`advance_stage`, or stage-specific `approve_*`/`advance_*` functions).

`main.py` is a single-file FastAPI app that exposes one endpoint per stage action (`/sessions/{id}/clarify/questions`, `/analyse`, `/gaps/*`, `/requirements/*`, `/brd/*`, `/stories/*`, plus `/admin/*`, `/meetings/*`, `/upload`, `/eval/*`, `/cache/*`). When adding stage endpoints follow the existing thin-wrapper pattern: parse request model → call module function → catch `ValueError` as 400 and generic `Exception` as 500. **Do not add business logic to `main.py`.**

### Session as single source of truth

A session dict contains **every** piece of state the pipeline produces (see `create_session` in `session_manager.py`): raw/refined problem, clarifying Q&A, impacted systems/stakeholders, gap Q&A, requirements list, BRD draft, stories, plus all `agent_*` fields from Features 7/8 and cost tracking fields (`*_tokens_in/out`, `*_cost`). Observability, eval, and admin dashboards all reconstruct metrics by reading these session fields back — so **never rename existing session keys without updating `observability.py` and the admin dashboard in `static/index.html`**.

`revert_session` clears session fields downstream of a target stage via a hard-coded `clear_map` — if you add new stage-produced fields, add them there too.

### Prompt registry (`prompts.json` + `prompt_manager.py`)

All prompts live in `src/prompts.json` keyed by `category.name` (e.g. `stages.clarification`, `stages.agent_babok_check`, `meetings.analysis`, `stages.eval_judge`). Each entry has `version`, `model`, `temperature`, `max_tokens`, `system`, `user_template`. Modules fetch with `get_prompt("stages", "clarification")` and call `.format(...)` on `user_template`.

- Cache is `@lru_cache`-based; call `reload_prompts()` after editing
- Log `get_prompt_version(...)` alongside every LLM call for traceability
- `_meta.version` in prompts.json is the registry version; bump it when shipping a prompt change
- Cost estimation uses `estimate_cost(input_tokens, output_tokens)` pulling rates from `_meta`
- **Never hard-code prompts in Python modules** — always add them to `prompts.json` with a version string

### Multi-agent layer (Features 7 & 8)

Two agents run the observe-plan-act pattern with a reflection loop:

- **Feature 7 — Requirements Validation Agent** (`requirements_agent.py`): tools = KB contradiction search (Python), BABOK quality check (GPT), meeting decisions cross-reference (GPT). Reflects if `quality_score < 70` (`QUALITY_THRESHOLD`), max 3 iterations.
- **Feature 8 — BRD Review Agent** (`brd_review_agent.py`): tools = traceability check (Python), BRD 6-dimension quality check (GPT), stakeholder alignment vs Stage 3 (GPT). Reflects if `quality_score < 75`, max 3. **If F7 score was below 70, F8 raises its own threshold to 80** — this is the multi-agent coordination handoff.

Both are also exposed as LangGraph subgraphs:
- `lg_requirements_graph.py` / `lg_brd_review_graph.py` build the compiled subgraphs
- `lg_coordinator.py` composes both into a single coordinator graph with a `route_agent_node` that dispatches based on `agent_to_run ∈ {"validate_requirements","review_brd","both"}`
- `lg_state.py` has three `TypedDict`s: `RequirementsAgentState`, `BRDReviewAgentState`, `CoAnalyticaState`. Subgraphs have their own state; the coordinator **explicitly** extracts/writes fields between them (LangGraph does not auto-map across differing state types)
- Compiled graphs are built once at module load and reused — do not recompile per request
- LangGraph endpoints live at `/sessions/{id}/requirements/validate/lg`, `/brd/review/lg`, `/agents/run-all`. The non-`/lg` endpoints hit the plain Python implementations; both should stay behaviourally equivalent.

### Semantic cache (`semantic_cache.py`)

Intercepts BABOK GPT calls. Flow: hash requirements → embed with `text-embedding-3-small` → Redis lookup → cosine-similarity scan over cached vectors → return cached result if `≥ SIMILARITY_THRESHOLD` (default 0.93), else call GPT and store the result.

- Keys: `cache:babok:{hash}:result`, `:embedding`, `:meta`; running counters in `cache:stats`
- `get_redis()` returns `None` on failure — callers must fall back to direct GPT, not raise
- **Clear the cache (`DELETE /cache`) after any prompt version bump** that could make cached results stale
- Cost savings are computed from `AVG_BABOK_INPUT_TOKENS` × rates and written to `cache:stats` — admin dashboard reads from there

### Observability (`telemetry.py` + `observability.py`)

Two separate systems:

1. **OpenTelemetry live traces → Azure Application Insights.** `telemetry.setup_telemetry()` runs in FastAPI's `lifespan` context — it sets up `TracerProvider`, Azure Monitor exporter, and `FastAPIInstrumentor`. Spans are created with context managers: `agent_span()`, `tool_span()`, `llm_span()`. Attributes follow OTel GenAI semconv (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, etc.) plus `coanalytica.*` custom attrs. **Use `llm_span()` around every OpenAI call** so traces stay uniform.

2. **Admin dashboard aggregates (`observability.py`).** Pure read-only aggregation over sessions + meetings + doc registry — computes cost-by-stage, per-session cost tables, KB breakdown, and active prompt versions. Called from `/admin/*` endpoints. Relies on session fields named with stage prefixes (`{stage}_tokens_in`, `{stage}_tokens_out`, `{stage}_cost`) — if you add a new stage, extend the `STAGES` list at the top of the file.

### Evaluation framework (`eval_runner.py` + `src/eval/`)

Golden dataset in `src/eval/golden_requirements.json`; latest run results in `src/eval/eval_results.json`.

- `run_evaluation(use_llm_judge=False, max_cases=None)` runs all golden cases through the BABOK check. LLM-as-Judge groundedness (`stages.eval_judge`) is opt-in because it costs tokens.
- `run_ab_test(stage_key, version_a, version_b, max_cases=8)` captures baseline metrics for A/B prompt version comparison — run with A, edit `prompts.json`, run again with B, compare.
- API: `POST /eval/run`, `POST /eval/ab-test`, `GET /eval/results`.

### Knowledge base flow

`document_loader.py` (read `.pdf`/`.docx`/`.txt`) → `embedder.py` chunks with `RecursiveCharacterTextSplitter`, embeds with `text-embedding-3-small`, upserts into Azure AI Search index `ba-copilot-index` with `system_name`/`source_type`/`document_name`/`chunk_index` metadata → `retriever.get_relevant_context()` runs hybrid search with optional system/source filter → `format_context_with_citations()` builds the prompt context block and the citation list that every BRD footer renders.

`document_registry.py` keeps a flat JSON index at `document_registry.json` of what was uploaded (used by the `/documents` tree view and the admin KB breakdown). Systems and sources hierarchy lives in `systems.json` — edit via `systems_manager.py`, not directly.

### Meetings pipeline (`meeting_module.py`, Feature #4)

Upload → optional speech-to-text (if `.mp4`) → GPT summary + decision extraction → stored in Blob `meetings/<id>.json`. Meetings can be **re-indexed into the KB** via `POST /meetings/{id}/store` so downstream stages (and the requirements agent Tool 3) can cross-reference them.

## Import & path conventions

**Every module in `src/` starts with this pattern** and depends on it:

```python
import os, sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
```

This lets modules import each other as flat top-level names (`from session_manager import ...`) regardless of whether the app is launched via `uvicorn src.main:app` or from inside `src/`. **Preserve this sys.path append in any new module** — several imports (`telemetry`, `semantic_cache`, LangGraph modules) break without it.

Do not introduce relative imports (`from .foo import bar`) — they won't resolve under the current layout.

## Conventions to respect

- **Thin endpoints, fat modules.** `main.py` wrappers should not touch OpenAI, Blob, or Search directly.
- **Session fields are the contract.** Adding a feature usually means adding a session field + populating it in a module + surfacing it in `get_session_summary` and/or `observability.py` and/or the admin dashboard.
- **New LLM calls**: (1) add prompt to `prompts.json` with a `version`, (2) call via `get_prompt`/`get_model_config`, (3) wrap in `llm_span()` from `telemetry.py`, (4) record tokens/cost into the session with a stage-prefixed key, (5) consider cache-ability via `semantic_cache`.
- **Comments in this codebase are deliberately instructional** — many files begin with long docstring headers explaining the architecture concept they implement. When editing, keep those headers accurate; they're the primary documentation.
- The file `=5.0.0` in the repo root is an accidental shell redirect artifact from `pip install 'redis>=5.0.0'` — ignore it, don't delete unless intentionally cleaning up.

## Pilot Phase — Next Development Priorities

Current pilot scope: **single org, single BA user**. Local dev first, then promoted to Azure. The features below are queued in priority order.

1. **Multi-tenancy** — Partition Azure Blob containers and the Azure AI Search index by `org_id`. Today `create_session` writes a hard-coded `"org_id": "default"` in `session_manager.py` — that field is the seam to build on. Add Azure AD SSO on the FastAPI layer and enforce RBAC with three roles: **Org Admin**, **BA**, **Read-only Reviewer**. All `/admin/*`, `/eval/*`, `/cache/*`, `/upload`, `/systems/*` endpoints must become role-gated.

2. **KB Setup Portal — highest priority.** Build the Org Admin ingestion experience so a new tenant can self-serve their knowledge base. Scope:
   - SharePoint connector (document libraries → `embedder.embed_and_store` with system/source metadata)
   - Database schema connector (table/column metadata → KB chunks)
   - Jira / Azure DevOps connector (tickets, epics, comments)
   - Ingestion status dashboard for the Org Admin showing per-source sync state, last run, chunk counts, and failures
   Reuse the existing `document_registry.py` + Azure AI Search pipeline — connectors should emit the same chunk shape `embed_and_store` already accepts.

3. **MCP Tool Protocol** — `mcp_server.py` exists in the `outputs/` area but has **not yet been moved into `src/`**. Target: expose 6 tools over **streamable HTTP** mounted at `/mcp` on the same FastAPI app. When promoting it, follow the `sys.path.append` convention and reuse the existing stage-module functions as the tool implementations rather than re-implementing logic.

### Current environment snapshot

- **Prompts registry version:** `2.1.0` (`prompts.json` `_meta.version`)
- **`stages.agent_babok_check` version:** `1.6.0` — `temperature=0`, anti-anchor instruction forcing actual weighted average (fixes the 72-score anchoring bug on the 7 remaining eval cases)
- **Redis semantic cache:** live at `redis-15188.c56.east-us.azure.cloud.redislabs.com:15188` (Redis Cloud). Threshold `0.93`, TTL 168h. Graceful fallback to direct GPT if the connection fails.
