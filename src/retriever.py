# retriever.py
# The retriever does one job:
# Take a user's question → find the most relevant chunks → return them
#
# It sits between the user and the generator:
#   User question
#       ↓
#   Retriever  ← (this file)
#       ↓
#   Relevant chunks
#       ↓
#   Generator (Step 7) → Final answer

import os
import sys
from dotenv import load_dotenv

# So Python can find our other files in src/
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from embedder import load_existing_store, embed_and_store, chunk_text
from document_loader import load_document

load_dotenv()


def get_relevant_context(question, top_k=3):
    """
    Main function — takes a question, returns relevant text chunks.

    top_k = how many chunks to return
    More chunks = more context for the AI but costs more tokens.
    3 is a good balance for MVP.
    """
    print(f"\n🔍 Searching knowledge base for: '{question}'")

    # Search ChromaDB for the most relevant chunks
    vectorstore = load_existing_store()
    results = vectorstore.similarity_search(question, k=top_k)

    # Pull out just the text from the results
    chunks = [result.page_content for result in results]

    print(f"📋 Found {len(chunks)} relevant chunks")
    return chunks


def format_context(chunks):
    """
    Join the chunks into one clean block of text
    that we can paste into the prompt for GPT-4o-mini.
    """
    context = ""
    for i, chunk in enumerate(chunks):
        context += f"--- Source {i+1} ---\n"
        context += chunk
        context += "\n\n"
    return context.strip()


def load_and_index_document(file_path):
    """
    Helper function that does the full pipeline:
    Load a document → chunk it → embed it → store in ChromaDB.

    Call this whenever you add a new document.
    """
    print(f"\n📂 Loading and indexing: {file_path}")
    text   = load_document(file_path)
    chunks = chunk_text(text)
    embed_and_store(chunks)
    print(f"✅ Document indexed and ready to search!")


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":

    # ── Test 1: Re-index the document ─────────────────────────
    print("=" * 55)
    print("TEST 1: Index the document into ChromaDB")
    print("=" * 55)
    load_and_index_document("documents/sample_requirements.txt")

    # ── Test 2: Retrieve for different questions ───────────────
    print("\n" + "=" * 55)
    print("TEST 2: Retrieve relevant chunks for 3 questions")
    print("=" * 55)

    questions = [
        "What are the main pain points with the current process?",
        "Who are the stakeholders for this project?",
        "What are the business goals?"
    ]

    for question in questions:
        print(f"\n{'─' * 55}")
        chunks  = get_relevant_context(question, top_k=2)
        context = format_context(chunks)

        print(f"\n📄 Context that will be sent to GPT-4o-mini:\n")
        print(context)

    print("\n" + "=" * 55)
    print("✅ Retriever is working!")
    print("=" * 55)