# embedder.py
# New version — uses Azure AI Search instead of ChromaDB
# Every chunk is stored with full metadata:
#   system_name, source_type, document_name, chunk_index
#
# This means every search result knows exactly
# where it came from — enabling citations in the BRD.

import os
import sys
import json
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SearchField,
    SearchFieldDataType,
    SimpleField,
    SearchableField,
    SearchableField,
    VectorSearch,
    HnswAlgorithmConfiguration,
    VectorSearchProfile,
    SearchFieldDataType,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

# ── Config ────────────────────────────────────────────────────
OPENAI_CLIENT  = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ENDPOINT       = os.getenv("AZURE_SEARCH_ENDPOINT")
KEY            = os.getenv("AZURE_SEARCH_KEY")
INDEX_NAME     = os.getenv("AZURE_SEARCH_INDEX", "ba-copilot-index")
CREDENTIAL     = AzureKeyCredential(KEY)
EMBEDDING_DIM  = 1536   # text-embedding-3-small output size


# ── Step 1: Create the Index ──────────────────────────────────
def create_index():
    """
    Create the Azure AI Search index with all fields.
    This only needs to run ONCE — it's safe to run again
    (create_or_update won't delete existing data).
    """
    index_client = SearchIndexClient(
        endpoint=ENDPOINT,
        credential=CREDENTIAL
    )

    fields = [
        # ── Identity ──────────────────────────────────────────
        SimpleField(
            name="id",
            type=SearchFieldDataType.String,
            key=True,
            filterable=True
        ),

        # ── Content ───────────────────────────────────────────
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
            analyzer_name="en.microsoft"   # English language analyser
        ),

        # ── Tenancy (Phase 2 Sprint 1) ────────────────────────
        # Filterable so every retriever query can enforce org isolation.
        # Legacy docs indexed before Sprint 1 have this field missing/null;
        # the search filter below handles that with an "eq null" disjunct
        # so Phase 1 "default" org behaviour is preserved.
        SimpleField(
            name="org_id",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True
        ),

        # ── Hierarchy Metadata ────────────────────────────────
        SimpleField(
            name="system_name",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True    # Allows grouping by system
        ),
        SimpleField(
            name="source_type",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True    # Allows grouping by source
        ),
        SimpleField(
            name="document_name",
            type=SearchFieldDataType.String,
            filterable=True
        ),

        # ── Chunk Metadata ────────────────────────────────────
        SimpleField(
            name="chunk_index",
            type=SearchFieldDataType.Int32,
            filterable=True
        ),
        SimpleField(
            name="total_chunks",
            type=SearchFieldDataType.Int32,
        ),
        SimpleField(
            name="upload_date",
            type=SearchFieldDataType.String,
            filterable=True
        ),

        # ── Vector Embedding ──────────────────────────────────
        SearchField(
            name="embedding",
            type=SearchFieldDataType.Collection(
                SearchFieldDataType.Single
            ),
            searchable=True,
            vector_search_dimensions=EMBEDDING_DIM,
            vector_search_profile_name="ba-copilot-profile"
        ),
    ]

    # Vector search configuration
    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="ba-copilot-hnsw")
        ],
        profiles=[
            VectorSearchProfile(
                name="ba-copilot-profile",
                algorithm_configuration_name="ba-copilot-hnsw"
            )
        ]
    )

    index = SearchIndex(
        name=INDEX_NAME,
        fields=fields,
        vector_search=vector_search
    )

    index_client.create_or_update_index(index)
    print(f"✅ Index '{INDEX_NAME}' ready in Azure AI Search")


# ── Step 2: Get Embedding ─────────────────────────────────────
def get_embedding(text):
    """Convert text to 1536 numbers using OpenAI."""
    response = OPENAI_CLIENT.embeddings.create(
        model="text-embedding-3-small",
        input=text
    )
    return response.data[0].embedding


# ── Step 3: Chunk the Document ────────────────────────────────
def chunk_text(text):
    """Chop document into overlapping chunks."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n\n", "\n", ". ", " "]
    )
    chunks = splitter.split_text(text)
    print(f"✂️  Split into {len(chunks)} chunks")
    return chunks


# ── Step 4: Embed and Store with Metadata ─────────────────────
def embed_and_store(
    text,
    system_name,
    source_type,
    document_name,
    org_id=None
):
    """
    Full pipeline for one document:
      1. Chunk the text
      2. Embed each chunk
      3. Store in Azure AI Search with full metadata (incl. org_id)

    Parameters:
      text          → raw text content of the document
      system_name   → e.g. "HR System"
      source_type   → e.g. "SharePoint"
      document_name → e.g. "HR_Policy_2024.pdf"
      org_id        → tenant scope; defaults to 'default' (Phase 2 Sprint 1)
    """
    # Late import to avoid a module-level circular dep with session_manager.
    from session_manager import _resolve_org_id
    org_id = _resolve_org_id(org_id)
    # Ensure index exists
    create_index()

    # Chunk
    chunks = chunk_text(text)
    total  = len(chunks)

    # Build search client
    search_client = SearchClient(
        endpoint=ENDPOINT,
        index_name=INDEX_NAME,
        credential=CREDENTIAL
    )

    # Build document ID prefix
    # (safe for Azure — no spaces or special chars)
    safe_org    = org_id.replace(" ", "-").lower()
    safe_system = system_name.replace(" ", "-").lower()
    safe_source = source_type.replace(" ", "-").lower()
    # Remove file extension and replace any dots with dashes
    # Azure AI Search keys only allow: letters, digits, _ - =
    doc_no_ext  = os.path.splitext(document_name)[0]  # removes .txt .pdf .docx
    safe_doc    = doc_no_ext.replace(" ", "-").replace(".", "-").lower()
    # Prefix org_id so doc ids are globally unique across tenants even when
    # two orgs index a file with the same name under the same system/source.
    id_prefix   = f"{safe_org}_{safe_system}_{safe_source}_{safe_doc}"

    # Embed and upload each chunk
    print(f"\n🔢 Embedding {total} chunks...")
    documents = []

    for i, chunk in enumerate(chunks):
        print(f"   Chunk {i+1}/{total}...")

        embedding = get_embedding(chunk)

        doc = {
            "id":            f"{id_prefix}_{i}",
            "content":       chunk,
            "org_id":        org_id,
            "system_name":   system_name,
            "source_type":   source_type,
            "document_name": document_name,
            "chunk_index":   i,
            "total_chunks":  total,
            "upload_date":   datetime.now().strftime("%Y-%m-%d"),
            "embedding":     embedding,
        }
        documents.append(doc)

    # Upload all chunks in one batch
    search_client.upload_documents(documents=documents)

    print(f"✅ Uploaded {total} chunks to Azure AI Search")
    print(f"   System  : {system_name}")
    print(f"   Source  : {source_type}")
    print(f"   Document: {document_name}")

    return total


# ── Step 5: Search ────────────────────────────────────────────
def search(
    query,
    top_k=3,
    system_name=None,
    source_type=None,
    org_id=None
):
    """
    Hybrid search — combines keyword + vector search.
    Optionally filter by system_name and/or source_type.
    Always filters by org_id for Phase 2 tenant isolation.

    Returns list of dicts with content + metadata.
    """
    # Late import to avoid a module-level circular dep with session_manager.
    from session_manager import _resolve_org_id, DEFAULT_ORG_ID
    org_id = _resolve_org_id(org_id)

    search_client = SearchClient(
        endpoint=ENDPOINT,
        index_name=INDEX_NAME,
        credential=CREDENTIAL
    )

    # Build filter string.
    # Tenancy filter: match docs explicitly tagged with this org_id.
    # For the "default" org we also match legacy docs where org_id is null
    # (indexed before Sprint 1). Remove the null disjunct in a future sprint
    # once all pilot docs have been re-indexed with org_id.
    filters = []
    if org_id == DEFAULT_ORG_ID:
        filters.append(f"(org_id eq '{org_id}' or org_id eq null)")
    else:
        filters.append(f"org_id eq '{org_id}'")
    if system_name:
        filters.append(f"system_name eq '{system_name}'")
    if source_type:
        filters.append(f"source_type eq '{source_type}'")
    filter_str = " and ".join(filters)

    # Get embedding for the query
    query_embedding = get_embedding(query)

    from azure.search.documents.models import VectorizedQuery
    vector_query = VectorizedQuery(
        vector=query_embedding,
        k_nearest_neighbors=top_k,
        fields="embedding"
    )

    # Run hybrid search
    results = search_client.search(
        search_text=query,          # keyword search
        vector_queries=[vector_query],  # vector search
        filter=filter_str,
        top=top_k,
        select=[
            "content",
            "org_id",
            "system_name",
            "source_type",
            "document_name",
            "chunk_index",
            "upload_date"
        ]
    )

    # Format results with metadata
    formatted = []
    for r in results:
        formatted.append({
            "content":       r["content"],
            "org_id":        r.get("org_id"),     # may be None for legacy docs
            "system_name":   r["system_name"],
            "source_type":   r["source_type"],
            "document_name": r["document_name"],
            "chunk_index":   r["chunk_index"],
            "upload_date":   r["upload_date"],
            "score":         r["@search.score"],
        })

    return formatted


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from document_loader import load_document

    print("=" * 55)
    print("TEST 1: Create index")
    print("=" * 55)
    create_index()

    print("\n" + "=" * 55)
    print("TEST 2: Load and embed document")
    print("=" * 55)
    text = load_document("documents/sample_requirements.txt")
    embed_and_store(
        text=text,
        system_name="HR System",
        source_type="SharePoint",
        document_name="sample_requirements.txt"
    )

    print("\n" + "=" * 55)
    print("TEST 3: Search with metadata returned")
    print("=" * 55)
    query   = "What are the main pain points?"
    results = search(query, top_k=2)

    print(f"🔍 Query: '{query}'")
    print(f"📋 Found {len(results)} results:\n")

    for i, r in enumerate(results):
        print(f"── Result {i+1} ──────────────────────────")
        print(f"📄 Content    : {r['content'][:120]}...")
        print(f"🏢 System     : {r['system_name']}")
        print(f"📁 Source     : {r['source_type']}")
        print(f"📎 Document   : {r['document_name']}")
        print(f"🔢 Chunk      : {r['chunk_index']}")
        print(f"📅 Uploaded   : {r['upload_date']}")
        print(f"⭐ Score      : {r['score']:.4f}")
        print()

    print("✅ Azure AI Search embedder working!")