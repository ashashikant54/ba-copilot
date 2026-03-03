# document_registry.py
# UPDATED FOR AZURE CLOUD DEPLOYMENT
#
# WHAT CHANGED FROM THE ORIGINAL:
#   - Registry was saved as "document_registry.json" on your laptop's hard drive
#   - Now saved to Azure Blob Storage as "document_registry.json" in the "documents" container
#   - ALL function names and logic stay EXACTLY the same
#
# WHY WE CHANGED THIS:
#   - Azure App Service wipes local files on every restart
#   - Your document registry would be lost, making all uploaded docs disappear from the UI
#
# Structure of registry (UNCHANGED):
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
import uuid
from datetime import datetime

# ── NEW IMPORTS FOR AZURE BLOB STORAGE ────────────────────────
# Same imports as session_manager.py — these load the Azure library
from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

# ── CONFIGURATION ──────────────────────────────────────────────
# OLD CODE saved to a local file:
#   REGISTRY_FILE = "document_registry.json"
#
# NEW CODE reads the connection string from your environment settings.
# Same connection string you already set up for session_manager.py!
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
DOCUMENTS_CONTAINER = "documents"       # The Azure Blob container name
REGISTRY_BLOB_NAME  = "document_registry.json"  # The file name inside that container


# ── HELPER: Get the Azure Blob Container ──────────────────────
# Same pattern as session_manager.py — connects to Azure Blob Storage
def _get_container():
    """Connect to Azure Blob Storage and return the documents container."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError(
            "❌ AZURE_STORAGE_CONNECTION_STRING is not set!\n"
            "  On your laptop: add it to your .env file\n"
            "  On Azure: add it in App Service → Configuration → App Settings"
        )
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container = blob_service.get_container_client(DOCUMENTS_CONTAINER)
    # Create the container if it doesn't exist yet
    try:
        container.create_container()
    except Exception:
        pass  # Container already exists — that's fine
    return container


# ── Load Registry from Azure ───────────────────────────────────
def load_registry():
    """Load all document records from Azure Blob Storage."""
    # OLD CODE:
    #   if not os.path.exists(REGISTRY_FILE):
    #       return []
    #   with open(REGISTRY_FILE, "r") as f:
    #       return json.load(f)
    #
    # NEW CODE: download "document_registry.json" from Azure Blob Storage
    try:
        container = _get_container()
        blob = container.get_blob_client(REGISTRY_BLOB_NAME)
        data = blob.download_blob().readall()       # Download the file bytes
        return json.loads(data.decode("utf-8"))     # Convert bytes → list of records
    except ResourceNotFoundError:
        # File doesn't exist yet — return empty list (same as original behaviour)
        return []


# ── Save Registry to Azure ─────────────────────────────────────
def save_registry(registry):
    """Save registry to Azure Blob Storage."""
    # OLD CODE:
    #   with open(REGISTRY_FILE, "w") as f:
    #       json.dump(registry, f, indent=2)
    #
    # NEW CODE: upload "document_registry.json" to Azure Blob Storage
    container = _get_container()
    data = json.dumps(registry, indent=2)           # Convert list → JSON text
    container.upload_blob(
        name=REGISTRY_BLOB_NAME,
        data=data,
        overwrite=True,         # overwrite=True means "update if it already exists"
        encoding="utf-8"
    )


# ── All functions below are COMPLETELY UNCHANGED ───────────────
# Only load_registry() and save_registry() changed above.
# Everything else works exactly as before because they all
# call load_registry() and save_registry() internally.

def register_document(
    document_name,
    system_name,
    source_type,
    chunks,
    file_size_kb=0
):
    """
    Add a document record after successful indexing.
    Called by the upload endpoint after embed_and_store completes.
    (UNCHANGED)
    """
    registry = load_registry()

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
    save_registry(registry)
    return record


def get_all_documents():
    """Return all document records. (UNCHANGED)"""
    return load_registry()


def get_documents_by_system(system_name):
    """Return all documents for a specific system. (UNCHANGED)"""
    return [
        doc for doc in load_registry()
        if doc["system_name"] == system_name
    ]


def get_documents_by_source(system_name, source_type):
    """Return all documents for a specific system + source. (UNCHANGED)"""
    return [
        doc for doc in load_registry()
        if doc["system_name"] == system_name
        and doc["source_type"] == source_type
    ]


def get_registry_as_tree():
    """
    Return documents organised as a hierarchy tree. (UNCHANGED)
    Structure:
    {
      "HR System": {
        "SharePoint": [doc1, doc2],
        "Azure DevOps": [doc3]
      },
      "Finance System": {
        "SharePoint": [doc4]
      }
    }
    """
    registry = load_registry()
    tree = {}

    for doc in registry:
        sys  = doc["system_name"]
        src  = doc["source_type"]

        if sys not in tree:
            tree[sys] = {}
        if src not in tree[sys]:
            tree[sys][src] = []

        tree[sys][src].append(doc)

    return tree


def delete_document(doc_id):
    """Remove a document record by ID. (UNCHANGED)"""
    registry = load_registry()
    original = len(registry)
    registry = [d for d in registry if d["id"] != doc_id]

    if len(registry) == original:
        return {"success": False, "message": f"Document ID '{doc_id}' not found"}

    save_registry(registry)
    return {"success": True, "message": "Document record removed"}


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing document registry with Azure Blob Storage...\n")

    # Register some test documents
    register_document(
        document_name="HR_Policy_2024.pdf",
        system_name="HR System",
        source_type="SharePoint",
        chunks=6,
        file_size_kb=245
    )
    register_document(
        document_name="Onboarding_SOP.docx",
        system_name="HR System",
        source_type="SharePoint",
        chunks=4,
        file_size_kb=128
    )
    register_document(
        document_name="Sprint_Backlog_Q3.xlsx",
        system_name="IT System",
        source_type="Azure DevOps",
        chunks=8,
        file_size_kb=89
    )

    # Show tree
    print("Document tree:")
    tree = get_registry_as_tree()
    for system, sources in tree.items():
        print(f"\n  🏢 {system}")
        for source, docs in sources.items():
            print(f"     📁 {source}")
            for doc in docs:
                print(f"          📄 {doc['document_name']} "
                      f"({doc['chunks']} chunks, "
                      f"{doc['file_size_kb']}KB, "
                      f"{doc['upload_date']})")

    print("\n✅ Document registry working with Azure Blob Storage!")