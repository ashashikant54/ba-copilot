# document_registry.py
# UPDATED FOR AZURE CLOUD DEPLOYMENT + PHASE 2 MULTI-TENANCY (Sprint 2 A8.3)
#
# WHAT CHANGED:
#   Phase 1 — a single flat JSON blob:
#     documents/document_registry.json
#   Sprint 2 — one registry file per org (user-chosen design over per-record
#   org_id field, for simpler isolation and one-read list):
#     documents/document_registry_{org_id}.json
#
#   Record shape is UNCHANGED on purpose. The file location carries the
#   tenancy — no per-entry org_id field — so existing observability,
#   admin dashboard reads, and tree-building logic continue to work.
#
# READ-THROUGH FALLBACK (default org only):
#   On first read of document_registry_default.json after deploy, the file
#   does not exist. We fall through to the legacy document_registry.json,
#   return its contents, and the caller's next save_registry writes the
#   merged list (legacy entries + any new record) to the org-prefixed file.
#   From that point on, the legacy file is stale — untouched but orphaned.
#   Delete it manually from Blob when confident of the migration.
#
# Structure of each record (UNCHANGED):
# [
#   {
#     "id": "abc123",
#     "document_name": "HR_Policy_2024.pdf",
#     "system_name": "HR System",
#     "source_type": "SharePoint",
#     "chunks": 6,
#     "upload_date": "2025-01-15",
#     "upload_time": "14:32:01",
#     "file_size_kb": 245
#   },
#   ...
# ]

import json
import os
import sys
import uuid
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

from session_manager import _resolve_org_id, DEFAULT_ORG_ID

# ── CONFIGURATION ──────────────────────────────────────────────
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
DOCUMENTS_CONTAINER     = "documents"
LEGACY_REGISTRY_BLOB    = "document_registry.json"   # Phase 1 single-file registry


# ── HELPER: Get the Azure Blob Container ──────────────────────
def _get_container():
    """Connect to Azure Blob Storage and return the documents container."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError(
            "AZURE_STORAGE_CONNECTION_STRING is not set. "
            "Add it to .env (local dev) or App Service settings (prod)."
        )
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container = blob_service.get_container_client(DOCUMENTS_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass   # already exists
    return container


def _registry_blob_name(org_id):
    """Return the org-scoped registry filename."""
    return f"document_registry_{org_id}.json"


# ── Load / Save Registry ──────────────────────────────────────
def load_registry(org_id=None):
    """Load the registry for an org.

    Primary: document_registry_{org_id}.json
    LEGACY FALLBACK (default org only): document_registry.json — Phase 1 file.

    The fallback is intentionally read-only. save_registry always writes to
    the primary org-prefixed blob, so the first save after deploy persists
    the merged list (legacy + new) under the new path. After that the
    legacy file becomes stale but is left alone for human inspection.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()

    # Primary path
    try:
        blob = container.get_blob_client(_registry_blob_name(org_id))
        data = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except ResourceNotFoundError:
        pass

    # ── LEGACY FALLBACK (default org only) ──
    if org_id == DEFAULT_ORG_ID:
        try:
            blob = container.get_blob_client(LEGACY_REGISTRY_BLOB)
            data = blob.download_blob().readall()
            return json.loads(data.decode("utf-8"))
        except ResourceNotFoundError:
            pass

    # Neither file exists → empty registry (first-use behaviour preserved)
    return []


def save_registry(registry, org_id=None):
    """Save the registry for an org to its per-org blob.

    Never writes to the legacy Phase 1 blob — that file stays immutable
    after Sprint 2 rollout.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    data      = json.dumps(registry, indent=2)
    container.upload_blob(
        name=_registry_blob_name(org_id),
        data=data,
        overwrite=True,
        encoding="utf-8"
    )


# ── Registry operations (now org-scoped) ──────────────────────
def register_document(
    document_name,
    system_name,
    source_type,
    chunks,
    file_size_kb=0,
    org_id=None
):
    """Add a document record after successful indexing.

    Called by main.py's /upload endpoint and by meeting_module.store_meeting_to_kb.
    Phase 1 callers that don't pass org_id fall through to the default org —
    behaviour unchanged pre-auth-middleware.
    """
    org_id   = _resolve_org_id(org_id)
    registry = load_registry(org_id=org_id)

    record = {
        "id":            str(uuid.uuid4())[:8],
        "document_name": document_name,
        "system_name":   system_name,
        "source_type":   source_type,
        "chunks":        chunks,
        "upload_date":   datetime.now().strftime("%Y-%m-%d"),
        "upload_time":   datetime.now().strftime("%H:%M:%S"),
        "file_size_kb":  file_size_kb,
    }

    registry.append(record)
    save_registry(registry, org_id=org_id)
    return record


def get_all_documents(org_id=None):
    """Return all document records for an org."""
    return load_registry(org_id=org_id)


def get_documents_by_system(system_name, org_id=None):
    """Return all documents for a system within an org."""
    return [
        doc for doc in load_registry(org_id=org_id)
        if doc["system_name"] == system_name
    ]


def get_documents_by_source(system_name, source_type, org_id=None):
    """Return all documents for a (system, source) pair within an org."""
    return [
        doc for doc in load_registry(org_id=org_id)
        if doc["system_name"] == system_name
        and doc["source_type"] == source_type
    ]


def get_registry_as_tree(org_id=None):
    """Return an org's documents organised as {system: {source: [docs]}}."""
    registry = load_registry(org_id=org_id)
    tree = {}

    for doc in registry:
        sys_name = doc["system_name"]
        src      = doc["source_type"]

        if sys_name not in tree:
            tree[sys_name] = {}
        if src not in tree[sys_name]:
            tree[sys_name][src] = []

        tree[sys_name][src].append(doc)

    return tree


def delete_document(doc_id, org_id=None):
    """Remove a document record by ID from an org's registry."""
    org_id   = _resolve_org_id(org_id)
    registry = load_registry(org_id=org_id)
    original = len(registry)
    registry = [d for d in registry if d["id"] != doc_id]

    if len(registry) == original:
        return {"success": False, "message": f"Document ID '{doc_id}' not found"}

    save_registry(registry, org_id=org_id)
    return {"success": True, "message": "Document record removed"}


# ── Smoke test (per-org isolation + legacy fallback) ──────────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Document Registry (per-org files + fallback)")
    print("=" * 55)

    # Two distinct throwaway orgs to prove isolation. Using UUID suffixes so
    # repeat smoke-test runs don't collide with each other or any real org.
    org_a = f"smoketest-a-{uuid.uuid4().hex[:6]}"
    org_b = f"smoketest-b-{uuid.uuid4().hex[:6]}"

    print(f"\n── Registering one doc under '{org_a}'")
    rec_a = register_document(
        document_name="alpha_policy.pdf",
        system_name="HR System",
        source_type="SharePoint",
        chunks=5,
        file_size_kb=120,
        org_id=org_a,
    )
    print(f"   OK — id={rec_a['id']}  name={rec_a['document_name']}")

    print(f"── Registering two docs under '{org_b}'")
    register_document("beta_spec.pdf",  "Finance System", "Database",    7, 200, org_id=org_b)
    register_document("beta_notes.txt", "Finance System", "SharePoint", 3,  55, org_id=org_b)

    print("\n── Isolation checks")
    docs_a = get_all_documents(org_id=org_a)
    docs_b = get_all_documents(org_id=org_b)
    print(f"   org_a sees {len(docs_a)} doc(s)  (expect 1)")
    print(f"   org_b sees {len(docs_b)} doc(s)  (expect 2)")
    assert len(docs_a) == 1 and len(docs_b) == 2, "Cross-org leak detected!"
    assert all(d["id"] != rec_a["id"] for d in docs_b), "org_b unexpectedly has org_a's record"

    print("\n── Tree view for org_b")
    tree_b = get_registry_as_tree(org_id=org_b)
    for sys_name, sources in tree_b.items():
        print(f"   {sys_name}")
        for src, docs in sources.items():
            print(f"     {src}  →  {len(docs)} doc(s)")

    print("\n── delete_document on org_a")
    result = delete_document(rec_a["id"], org_id=org_a)
    print(f"   {result}")
    assert get_all_documents(org_id=org_a) == []

    print("\n── Legacy fallback sanity check")
    legacy_docs = load_registry(org_id=DEFAULT_ORG_ID)
    print(f"   load_registry('default') → {len(legacy_docs)} record(s) "
          f"(reads org-prefixed primary if present, else legacy Phase 1 blob)")

    print("\n── Cleaning up smoke-test registry blobs")
    container = _get_container()
    for org in (org_a, org_b):
        try:
            container.delete_blob(_registry_blob_name(org))
            print(f"   deleted {_registry_blob_name(org)}")
        except ResourceNotFoundError:
            pass

    print("\nDocument registry smoke test complete.")
