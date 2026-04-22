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
