# test_azure_search.py
# Confirms Azure AI Search is connected and working.
# Creates a test index → adds a document → searches → cleans up.

import os
import time
from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchFieldDataType,
)

load_dotenv()

ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT")
KEY      = os.getenv("AZURE_SEARCH_KEY")
INDEX    = "test-connection-index"


def test_connection():
    print("=" * 55)
    print("Testing Azure AI Search connection...")
    print("=" * 55)

    credential   = AzureKeyCredential(KEY)
    index_client = SearchIndexClient(endpoint=ENDPOINT, credential=credential)

    # ── Step 1: Create a test index ───────────────────────────
    print("\n📋 Step 1: Creating test index...")

    fields = [
        SimpleField(
            name="id",
            type=SearchFieldDataType.String,
            key=True
        ),
        SearchableField(
            name="content",
            type=SearchFieldDataType.String
        ),
    ]

    index = SearchIndex(name=INDEX, fields=fields)
    index_client.create_or_update_index(index)
    print(f"   ✅ Index created")

    # ── Step 2: Add a test document ───────────────────────────
    print("\n📄 Step 2: Adding a test document...")

    search_client = SearchClient(
        endpoint=ENDPOINT,
        index_name=INDEX,
        credential=credential
    )

    docs = [{"id": "1", "content": "BA Copilot Azure AI Search test"}]
    search_client.upload_documents(documents=docs)
    print("   ✅ Document uploaded")

    # ── Step 3: Search for it ─────────────────────────────────
    print("\n🔍 Step 3: Searching...")
    time.sleep(3)  # Give Azure a moment to index

    results = list(search_client.search("BA Copilot"))
    print(f"   ✅ Found {len(results)} result(s)")
    for r in results:
        print(f"   Content: {r['content']}")

    # ── Step 4: Clean up ──────────────────────────────────────
    print("\n🧹 Step 4: Cleaning up test index...")
    index_client.delete_index(INDEX)
    print("   ✅ Test index deleted")

    print("\n" + "=" * 55)
    print("✅ Azure AI Search is connected and working!")
    print(f"   Endpoint: {ENDPOINT}")
    print("=" * 55)


if __name__ == "__main__":
    test_connection()