# retriever.py
# Updated to use Azure AI Search instead of ChromaDB.
# Now returns full metadata with every result
# so the generator can build proper citations.

import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from embedder import search, embed_and_store, chunk_text
from document_loader import load_document

load_dotenv()


def get_relevant_context(
    question,
    top_k=3,
    system_name=None,
    source_type=None
):
    """
    Find the most relevant chunks for a question.
    Returns list of result dicts with content + metadata.
    """
    print(f"\n🔍 Searching: '{question}'")
    if system_name:
        print(f"   Filter — System: {system_name}")
    if source_type:
        print(f"   Filter — Source: {source_type}")

    results = search(
        query=question,
        top_k=top_k,
        system_name=system_name,
        source_type=source_type
    )

    print(f"📋 Found {len(results)} relevant chunks")
    return results


def format_context_with_citations(results):
    """
    Format retrieved chunks into two things:
      1. context_text  → clean text for GPT-4o-mini prompt
      2. citations     → source list for BRD footer
    """
    context_text = ""
    citations    = []

    for i, r in enumerate(results):
        # Context block for the prompt
        context_text += f"[Source {i+1}]\n"
        context_text += f"{r['content']}\n\n"

        # Citation entry for the BRD
        citations.append({
            "number":        i + 1,
            "document_name": r["document_name"],
            "system_name":   r["system_name"],
            "source_type":   r["source_type"],
            "chunk_index":   r["chunk_index"],
            "upload_date":   r["upload_date"],
            "score":         r["score"],
        })

    return context_text.strip(), citations


def format_citations_block(citations):
    """
    Build the citation block that appears at the
    bottom of every generated BRD.
    """
    if not citations:
        return ""

    block  = "\n\n---\n"
    block += "## 📎 Sources & Citations\n\n"

    for c in citations:
        confidence = min(100, int(c["score"] * 100))
        block += f"**[{c['number']}] {c['document_name']}**\n"
        block += f"- System     : {c['system_name']}\n"
        block += f"- Source     : {c['source_type']}\n"
        block += f"- Chunk      : {c['chunk_index']}\n"
        block += f"- Uploaded   : {c['upload_date']}\n"
        block += f"- Confidence : {confidence}%\n\n"

    return block


def load_and_index_document(
    file_path,
    system_name,
    source_type
):
    """
    Full pipeline:
    Load document → chunk → embed → store in Azure AI Search
    with full hierarchy metadata.
    """
    document_name = os.path.basename(file_path)
    print(f"\n📂 Indexing: {document_name}")
    print(f"   System : {system_name}")
    print(f"   Source : {source_type}")

    text  = load_document(file_path)
    total = embed_and_store(
        text=text,
        system_name=system_name,
        source_type=source_type,
        document_name=document_name
    )

    print(f"✅ Indexed {total} chunks from {document_name}")
    return total


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":

    print("=" * 55)
    print("TEST 1: Index a document with hierarchy")
    print("=" * 55)
    load_and_index_document(
        file_path="documents/sample_requirements.txt",
        system_name="HR System",
        source_type="SharePoint"
    )

    print("\n" + "=" * 55)
    print("TEST 2: Search and get citations")
    print("=" * 55)

    questions = [
        "What are the main pain points?",
        "Who are the stakeholders?",
        "What are the business goals?"
    ]

    for q in questions:
        results = get_relevant_context(q, top_k=2)
        context, citations = format_context_with_citations(results)
        citation_block     = format_citations_block(citations)

        print(f"\n{'─' * 55}")
        print(f"Question: {q}")
        print(f"\nContext snippet:\n{context[:200]}...")
        print(f"\nCitations:{citation_block}")

    print("✅ Retriever with citations working!")