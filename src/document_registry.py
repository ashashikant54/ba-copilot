# document_registry.py
# Tracks every document that has been uploaded and indexed.
# Stored in document_registry.json on disk.
#
# Structure:
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

REGISTRY_FILE = "document_registry.json"


def load_registry():
    """Load all document records from disk."""
    if not os.path.exists(REGISTRY_FILE):
        return []
    with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_registry(registry):
    """Save registry to disk."""
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)


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
    """Return all document records."""
    return load_registry()


def get_documents_by_system(system_name):
    """Return all documents for a specific system."""
    return [
        doc for doc in load_registry()
        if doc["system_name"] == system_name
    ]


def get_documents_by_source(system_name, source_type):
    """Return all documents for a specific system + source."""
    return [
        doc for doc in load_registry()
        if doc["system_name"] == system_name
        and doc["source_type"] == source_type
    ]


def get_registry_as_tree():
    """
    Return documents organised as a hierarchy tree.
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
    """Remove a document record by ID."""
    registry = load_registry()
    original = len(registry)
    registry = [d for d in registry if d["id"] != doc_id]

    if len(registry) == original:
        return {"success": False, "message": f"Document ID '{doc_id}' not found"}

    save_registry(registry)
    return {"success": True, "message": "Document record removed"}


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing document registry...\n")

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

    print("\n✅ Document registry working!")