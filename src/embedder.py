# embedder.py
# Chops a document into chunks, embeds them with OpenAI,
# and stores everything in ChromaDB — a proper vector database.

import os
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import Chroma

load_dotenv()


# ── STEP 1: Chop ─────────────────────────────────────────────
def chunk_text(text):
    """Chop a long text into smaller overlapping chunks."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        separators=["\n\n", "\n", ". ", " "]
    )
    chunks = splitter.split_text(text)
    print(f"✂️  Chopped document into {len(chunks)} chunks")
    for i, chunk in enumerate(chunks):
        print(f"   Chunk {i+1}: {len(chunk)} chars — '{chunk[:55]}...'")
    return chunks


# ── STEP 2 & 3: Embed + Store in ChromaDB ────────────────────
def embed_and_store(chunks, collection_name="ba_copilot"):
    """Turn chunks into embeddings and store in ChromaDB."""
    print(f"\n🔢 Embedding {len(chunks)} chunks with OpenAI...")

    embeddings = OpenAIEmbeddings(
        model="text-embedding-3-small",
        api_key=os.getenv("OPENAI_API_KEY")
    )

    vectorstore = Chroma.from_texts(
        texts=chunks,
        embedding=embeddings,
        collection_name=collection_name,
        persist_directory=".chroma"
    )

    print(f"✅ Stored {len(chunks)} chunks in ChromaDB")
    print(f"💾 Database saved to: .chroma/ folder")
    return vectorstore


# ── STEP 4: Load existing store ───────────────────────────────
def load_existing_store(collection_name="ba_copilot"):
    """Load a ChromaDB that was already created — avoids re-embedding."""
    embeddings = OpenAIEmbeddings(
        model="text-embedding-3-small",
        api_key=os.getenv("OPENAI_API_KEY")
    )
    vectorstore = Chroma(
        collection_name=collection_name,
        embedding_function=embeddings,
        persist_directory=".chroma"
    )
    return vectorstore


# ── STEP 5: Search ────────────────────────────────────────────
def search(query, top_k=3, collection_name="ba_copilot"):
    """Find the most relevant chunks for a question."""
    vectorstore = load_existing_store(collection_name)
    results = vectorstore.similarity_search(query, k=top_k)
    return [r.page_content for r in results]


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from document_loader import load_document

    print("=" * 50)
    print("STEP 1: Load the document")
    print("=" * 50)
    text = load_document("documents/sample_requirements.txt")

    print("\n" + "=" * 50)
    print("STEP 2: Chop into chunks")
    print("=" * 50)
    chunks = chunk_text(text)

    print("\n" + "=" * 50)
    print("STEP 3: Embed and store in ChromaDB")
    print("=" * 50)
    embed_and_store(chunks)

    print("\n" + "=" * 50)
    print("STEP 4: Test a search query")
    print("=" * 50)
    query = "What are the main pain points?"
    print(f"🔍 Searching for: '{query}'\n")

    results = search(query, top_k=2)
    for i, result in enumerate(results):
        print(f"── Result {i+1} ──")
        print(result)
        print()

    print("✅ Embedder is working!")