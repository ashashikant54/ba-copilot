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
---

## Phase 2 Product Specification — Multi-tenancy, Connectors, Future Features

This section defines the complete product requirements for Phase 2 of CoAnalytica.
Claude Code must read this section fully before implementing any Phase 2 feature.
All features in this section are additive — existing Phase 1 functionality must be preserved.

---

## A. Multi-tenancy

### A1. Current State (Phase 1 — to be preserved)

- No authentication. Any user can open the app.
- All sessions, KB documents, and meetings are visible to everyone.
- Admin tab and Eval tab are open to all users.
- org_id: "default" exists in create_session but is not enforced anywhere.

### A2. Required Personas

Five personas with strict data isolation and tab visibility rules.

#### Persona 1 — Subscriber
- Signs up for CoAnalytica on behalf of an Organization using their work email.
- Provides the Organization name during signup (becomes the org namespace).
- Can provision other users into the org: Super Admin, Admin, Analyst roles.
- Does NOT use the product directly for BRD work — management only.
- Can configure org-level storage (see A5) and search settings (see A6).
- One Subscriber per Organization (the founding account).

#### Persona 2 — Super Admin
- Everything an Admin can do, with no Knowledge Box restriction.
- Sees ALL Knowledge Boxes within their Organization.
- Admin tab: full org-wide statistics (all KB, all sessions, all users).
- Eval tab: full org-wide evaluation metrics.
- Can promote/demote Analysts and Admins within the org.
- Cannot see data from other Organizations.

#### Persona 3 — Admin
- Creates and manages Knowledge Boxes within the org.
  - Knowledge Box (KB) = a named container for systems + sources + documents.
  - KB name must be unique within an org (e.g. Finance_KB1, Budget_KB1).
  - Admin can create, rename, and delete their own KBs.
- Adds systems to a KB (system = named grouping, e.g. "Finance System").
- Adds sources to a system (source = typed connection, e.g. SharePoint1, Database1, AzureDevOps_Backlog1).
- Links systems and sources to a KB.
- Grants Analyst access to specific KBs (not org-wide).
- Admin tab: visible, but scoped to KBs they own only.
- Eval tab: visible, but scoped to KBs they own only.
- Cannot see sessions or KBs owned by other Admins.

#### Persona 4 — Analyst
- Core product user — runs BRD sessions, generates requirements and stories.
- Can only access KBs explicitly granted by an Admin.
- Can upload documents under systems/sources within their accessible KBs.
- Sessions tab: sees only their own sessions.
- Meetings tab: sees only meetings uploaded within their accessible KBs.
- Admin tab: HIDDEN — Analysts cannot see this tab.
- Eval tab: HIDDEN — Analysts cannot see this tab.
- Knowledge Base tab: visible, but scoped to their accessible KBs only.

#### Persona 5 — App Support
- Cross-org visibility — can see all Organizations' usage.
- Does NOT see session content or KB documents (privacy boundary).
- Sees product analytics only:
  - Count of Organizations, users per org, roles breakdown.
  - Sessions created per org (count only, not content).
  - KB count, document count, meeting count per org.
  - AI quality scores aggregated per org (not individual sessions).
  - Billing-relevant metrics: total GPT cost per org, cache hit rates.
- Has a dedicated "Platform" tab visible only to this persona.
- Cannot perform any org-level actions.

### A3. Authentication Flow

- Email/password signup and login.
- JWT tokens with org_id + user_id + role claims.
- Every API request must carry the JWT in Authorization header.
- Backend validates JWT and injects org_id + role into every request context.
- Sessions, KB docs, meetings, and eval results are all partitioned by org_id.
- No cross-org data leakage under any endpoint.

Implementation note: Use Azure AD B2C for auth (already in the scaling plan).
For local dev, a simple JWT-based auth with a users.json store is acceptable
as a placeholder — keep it swappable.

### A4. Data Isolation Architecture

Every data entity gets org_id scoping:

Sessions (Azure Blob):
  BEFORE: sessions/{session_id}.json
  AFTER:  sessions/{org_id}/{session_id}.json

KB Documents (Azure AI Search):
  Add org_id as a filterable metadata field on every indexed document.
  Every retriever query must include filter: org_id == current_org_id.

Meetings (Azure Blob):
  BEFORE: meetings/{meeting_id}.json
  AFTER:  meetings/{org_id}/{meeting_id}.json

Knowledge Boxes:
  New entity: knowledge_boxes/{org_id}/{kb_id}.json
  Fields: kb_id, org_id, name, owner_admin_id, systems[], created_at

Users:
  New entity: users/{org_id}/{user_id}.json
  Fields: user_id, org_id, email, role, accessible_kb_ids[], created_at

Document Registry:
  Scoped: document_registry_{org_id}.json (or add org_id field to existing)

systems.json:
  Must become per-org: systems/{org_id}/systems.json

### A5. Org-level Storage Configuration

During Subscriber signup, allow configuration of storage backend:

```json
{
  "org_id": "acme_corp",
  "storage": {
    "provider": "azure_blob | aws_s3 | gcp_gcs | local",
    "connection_string": "...",
    "container_prefix": "coanalytica"
  }
}
```

This maps to the cloud-agnostic storage provider abstraction (see future architecture notes).
For Phase 2, support azure_blob (existing) and local as minimum.
The storage config is encrypted at rest in the platform's own config store.
Users never share their storage credentials with other orgs.

### A6. Org-level Search / Vector DB Configuration

During Subscriber signup, allow configuration of:

```json
{
  "search": {
    "vector_provider": "azure_ai_search | qdrant | pgvector | chroma",
    "endpoint": "...",
    "api_key": "...",
    "index_name": "coanalytica_{org_id}"
  },
  "embedding": {
    "provider": "openai | azure_openai | huggingface | ollama",
    "model": "text-embedding-3-small",
    "api_key": "..."
  }
}
```

For Phase 2, default remains Azure AI Search + OpenAI embeddings.
The abstraction layer should be designed so adding providers later is a config change.

### A7. UI Tab Visibility Rules

```
Tab          Subscriber  SuperAdmin  Admin       Analyst     AppSupport
─────────────────────────────────────────────────────────────────────────
Analyse      ✗           ✓           ✓           ✓           ✗
KnowledgeBase✗           ✓           ✓           ✓(scoped)   ✗
Sessions     ✗           ✓           ✓(scoped)   ✓(own only) ✗
Meetings     ✗           ✓           ✓(scoped)   ✓(scoped)   ✗
Admin        ✗           ✓(all KBs)  ✓(own KBs)  ✗           ✗
Eval         ✗           ✓(all KBs)  ✓(own KBs)  ✗           ✗
Platform     ✗           ✗           ✗           ✗           ✓
```

### A8. Implementation Order for Multi-tenancy

Build in this sequence — each step is independently deployable:

1. Add org_id to all session operations (session_manager.py) — no auth yet
2. Add org_id filter to all retriever queries (retriever.py, embedder.py)
3. Add org_id to meetings and document registry
4. Add Knowledge Box entity (new module: kb_manager.py)
5. Add user/role model (new module: user_manager.py)
6. Add JWT auth middleware (new module: auth_middleware.py)
7. Wire tab visibility to role claims in static/index.html
8. Add Subscriber signup + org storage config UI
9. Add App Support Platform tab

---

## B. Connectors — Automated Knowledge Base Ingestion

### B1. Current State (Phase 1 — to be preserved)

Individual document upload per session via the Knowledge Base tab.
Supported: .pdf, .docx, .txt
Meeting recordings: .mp4, .vtt, .docx uploaded individually.

### B2. Required Connectors

All connectors share a common interface defined in a new module: `src/connectors/base_connector.py`

```python
class BaseConnector:
    def __init__(self, config: dict, org_id: str, kb_id: str): ...
    def test_connection(self) -> bool: ...
    def list_items(self, since: datetime = None) -> list[ConnectorItem]: ...
    def fetch_item(self, item_id: str) -> bytes: ...
    def get_metadata(self, item_id: str) -> dict: ...
```

Each connector produces items that feed into the existing:
document_loader.py → embedder.py → Azure AI Search pipeline

#### Connector 1 — SharePoint
File: `src/connectors/sharepoint_connector.py`

Config fields:
- tenant_id, client_id, client_secret (Azure AD app registration)
- site_url: SharePoint site URL
- folder_path: relative folder path within the site
- sync_schedule: "daily" | "weekly" | "on_demand"

Supported file types: .docx, .xlsx, .pptx, .pdf, .txt, .mp4, .vtt
For .mp4: route through existing Azure Speech STT pipeline before embedding
For .xlsx: extract sheet data as structured text
For .pptx: extract slide text + speaker notes

Sync behavior:
- On first run: ingest all files in folder_path
- On subsequent runs: ingest only files modified since last_sync_at
- Track last_sync_at per connector config in Blob storage
- Deletions in SharePoint: mark document as inactive in registry (do not delete vectors)

#### Connector 2 — GitHub / Azure DevOps Codebase
File: `src/connectors/codebase_connector.py`

Config fields:
- provider: "github" | "azure_devops"
- repo_url
- branch: default "main"
- pat_token: Personal Access Token
- include_extensions: [".py", ".cs", ".java", ".sql", ".yaml", ".json", ".md"]
- exclude_paths: ["node_modules/", "venv/", "dist/", "*.min.js"]

Processing rules:
- Only index files matching include_extensions
- Skip binary files, auto-generated files, lock files
- For code files: extract function/class signatures + docstrings + README content
- For .sql: extract CREATE TABLE, stored procedure, function definitions
- Chunk by logical unit (function/class) not fixed size
- Store metadata: file_path, language, repo_name, branch, last_commit_sha

#### Connector 3 — Database Schema
File: `src/connectors/database_connector.py`

Config fields:
- db_type: "sqlserver" | "postgresql" | "mysql" | "oracle"
- connection_string (stored encrypted in Key Vault, never in plain text)
- schemas: list of schema names to include
- exclude_tables: list of table name patterns to skip

IMPORTANT: Only schema metadata is extracted — never row data.
Use INFORMATION_SCHEMA queries:
- Tables: table_name, column_name, data_type, is_nullable, column_default
- Stored procedures: procedure name + definition text
- Functions: function name + definition text
- Views: view name + definition text
- Foreign keys: relationship mapping

Output format per table:
"Table: {schema}.{table_name}
Columns: {col1} ({type}, {nullable}), {col2} ({type})...
Foreign keys: {col} → {ref_table}.{ref_col}
Description: [extracted from extended properties if available]"

Each database object becomes one chunk with metadata:
db_type, server, database_name, schema_name, object_type, object_name

#### Connector 4 — Jira / Azure DevOps Backlog
File: `src/connectors/backlog_connector.py`

Config fields:
- provider: "jira" | "azure_devops"
- base_url
- project_key (Jira) or organization/project (ADO)
- api_token
- item_types: ["Story", "Bug", "Epic", "Task"] (filter)
- status_filter: ["Active", "In Progress"] or empty for all
- date_range_days: only fetch items updated in last N days (default 180)

Per story, extract:
- Title, description, acceptance criteria
- Story points, priority, status
- Labels/tags, sprint, epic link
- Comments (last 5 only to avoid noise)
- Attachment names (not content)

Chunking: one chunk per story (title + description + ACs + comments)
Metadata: item_type, status, priority, sprint, epic, story_points, item_id, url

### B3. Connector Management UI

New tab or section within Knowledge Base tab for Admins only:
- "Connectors" sub-tab showing configured connectors per KB
- Add Connector button → wizard: select type → fill config → Test Connection → Save
- Each connector shows: type icon, name, last sync time, document count, status
- Manual "Sync Now" button per connector
- Sync schedule toggle: on/off + frequency selector
- Sync history: last 10 sync runs with count of items processed and errors

### B4. Ingestion Pipeline (Shared)

All connectors feed into a shared async ingestion pipeline:

```
Connector.fetch_item()
  → document_loader.py (existing, extend for new formats)
  → chunker (existing RecursiveCharacterTextSplitter, extend per type)
  → embedder.embed_and_store() (existing)
  → document_registry.py (register with connector metadata)
```

New module: `src/connectors/ingestion_pipeline.py`
- Accepts any BaseConnector instance
- Handles rate limiting, retries, error logging
- Reports progress via Server-Sent Events to the UI
- Stores ingestion run records in Blob: ingestion_logs/{org_id}/{connector_id}/{run_id}.json

Azure Functions trigger for scheduled syncs (one Function per connector config).
On-demand sync triggered by API: POST /connectors/{connector_id}/sync

---

## C. Future Enhancements (Design for, build later)

These are not Phase 2 scope but must be architecturally accommodated.
Do not block Phase 2 implementation on these, but do not design against them.

### C1. Flexible Workflow Selection

Currently CoAnalytica always runs the full 8-stage BRD workflow.

Future: Let users select at session start:
- "Full BRD" — existing 8-stage workflow (default)
- "Quick Requirements" — stages 1, 4, 5, 7 only (problem → gaps → reqs → stories)
  Use case: small feature requests, bug analysis, change requests
- "Impact Analysis" — new workflow TBD
  Input: bug ID or change request
  Output: impacted systems, impacted code modules, impacted user groups, risk score
- "Document Summary" — single-stage
  Input: select a system/source/document from KB
  Output: structured summary + flowchart diagram (Mermaid)

Architecture implication: session `stage` becomes a `workflow_type`-aware
state machine. `revert_session` clear_map must be workflow-aware.

### C2. Impact Analysis

New analysis type (not a BRD workflow):
- Input: existing requirement ID, bug description, or change request text
- Output:
  - List of impacted systems (from KB systems graph)
  - Impacted code modules (from codebase connector metadata)
  - Impacted user groups / stakeholders (from Stage 3 analysis)
  - Risk score (Low / Medium / High based on breadth of impact)
  - Recommended test areas

Implementation: new LangGraph agent — ImpactAnalysisAgent
Tools: kb_search, code_dependency_graph (from codebase connector), stakeholder_lookup

### C3. Document/Source/System Summary with Flowchart

New single-stage workflow:
- User selects a document, source, or entire system from KB tree
- Agent reads all chunks for that selection
- Generates: executive summary + key concepts + Mermaid flowchart
- Flowchart rendered in browser via mermaid.js (already available on cdnjs)
- Output exportable as Word doc or PDF

### C4. Code/SQL to Business Rules Conversion

New single-stage workflow:
- Input: codebase connector OR database connector selection
- Agent reads code/SQL objects
- Generates:
  - Business rules in plain English ("When X, then Y, unless Z")
  - Process flow diagram (Mermaid)
  - Data dictionary (for SQL objects)
  - Gap identification (rules with no corresponding requirement in KB)

---

## Phase 2 Build Sequence (across A, B, C)

Priority order — each item is independently shippable:

```
Sprint 1 (Weeks 1-2):  A8.1 + A8.2 — org_id in sessions + retriever
Sprint 2 (Weeks 3-4):  A8.3 + A8.4 — meetings + KB entity model
Sprint 3 (Weeks 5-6):  A8.5 + A8.6 — user/role model + JWT auth
Sprint 4 (Weeks 7-8):  A8.7 — tab visibility wired to roles
Sprint 5 (Weeks 9-10): B — SharePoint connector (highest value)
Sprint 6 (Weeks 11-12): B — Database schema connector
Sprint 7 (Weeks 13-14): B — Jira/ADO backlog connector
Sprint 8 (Weeks 15-16): B — GitHub/ADO codebase connector
Post-pilot:             A8.8 + A8.9 — Subscriber signup + App Support tab
Future:                 C1-C4 as separate sprints
```

---

## Key Architectural Rules for Phase 2

1. Every new module in src/ must start with sys.path.append (existing convention).
2. Every new LLM call must follow the 5-step convention (prompt in prompts.json,
   get_prompt, llm_span, record tokens/cost in session, consider caching).
3. org_id must be present on every Blob operation, every Search query,
   every session read/write. No operation is allowed without org_id context.
4. Connector credentials (API keys, connection strings, PAT tokens) must
   never be stored in plain text. Use Azure Key Vault references.
5. The existing Phase 1 default org (org_id: "default") must continue to work
   without auth during local development — add an env var DEV_MODE=true that
   bypasses auth and uses org_id: "default" automatically.
6. Do not modify existing session field names — observability.py and the admin
   dashboard depend on them. Add new fields; never rename existing ones.
7. All new UI components follow the existing card/tile pattern in index.html.
   Do not introduce new CSS frameworks.

