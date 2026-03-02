# generator.py
# Updated to include citations in every generated BRD.
# Every section references which source it came from.

import os
import sys
from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import (
    get_relevant_context,
    format_context_with_citations,
    format_citations_block
)

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


BRD_PROMPT = """
You are a senior Business Analyst with 15 years of experience.
Write a professional Business Requirements Document (BRD).

IMPORTANT INSTRUCTIONS:
- Only use information from the provided sources
- After each major section add: (Source: [1], [2]) etc.
- If information is not in the sources write: "TBC with stakeholders"
- Never invent or assume information

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUSINESS PROBLEM STATEMENT:
{problem}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RETRIEVED SOURCES:
{context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Write the BRD using this EXACT structure:

## 1. EXECUTIVE SUMMARY
(2-3 sentences — what and why)
(Source: [X])

## 2. BUSINESS PROBLEM
(Current pain points and business impact)
(Source: [X], [Y])

## 3. BUSINESS GOALS
(3-5 measurable goals)
(Source: [X])

## 4. SCOPE
### In Scope:
### Out of Scope:

## 5. STAKEHOLDERS
| Role | Team | Interest |
|------|------|----------|
(Source: [X])

## 6. FUNCTIONAL REQUIREMENTS
FR-001:
FR-002:
FR-003:

## 7. NON-FUNCTIONAL REQUIREMENTS
NFR-001:
NFR-002:

## 8. USER STORIES
**Story 1:**
- As a [user type]
- I want to [action]
- So that [benefit]
- Acceptance Criteria: [criteria]

## 9. SUCCESS METRICS
(3-5 measurable metrics)

## 10. ASSUMPTIONS & RISKS
### Assumptions:
### Risks:
"""


def generate_brd(
    problem_statement,
    system_name=None,
    source_type=None
):
    """
    Full pipeline:
    problem → retrieve → prompt → GPT-4o-mini → BRD with citations
    """
    print("\n" + "=" * 55)
    print("🤖 BA COPILOT — GENERATING BRD")
    print("=" * 55)

    # Step 1: Retrieve relevant chunks with metadata
    print("\n📚 Step 1: Searching knowledge base...")
    results = get_relevant_context(
        problem_statement,
        top_k=3,
        system_name=system_name,
        source_type=source_type
    )

    if not results:
        return (
            "⚠️ No relevant documents found in the knowledge base.\n"
            "Please upload documents first using the Upload tab."
        )

    # Step 2: Format context + citations
    print("\n📝 Step 2: Building context and citations...")
    context, citations = format_context_with_citations(results)
    citation_block     = format_citations_block(citations)

    # Step 3: Build prompt
    prompt = BRD_PROMPT.format(
        problem=problem_statement,
        context=context
    )
    print(f"   Prompt length: {len(prompt)} characters")

    # Step 4: Call GPT-4o-mini
    print("\n🧠 Step 3: Calling GPT-4o-mini...")
    print("   (15-30 seconds...)\n")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior Business Analyst. "
                    "Write structured professional BRDs. "
                    "Always cite sources using [1], [2] notation. "
                    "Never invent information not in the sources."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.2,
        max_tokens=2000
    )

    # Step 5: Build final BRD with citations appended
    brd_content    = response.choices[0].message.content
    full_brd       = brd_content + citation_block

    # Step 6: Show usage
    usage = response.usage
    print(f"📊 Tokens used  : {usage.total_tokens}")
    print(f"💰 Approx cost  : ~${usage.total_tokens * 0.00000015:.5f}")

    return full_brd


def save_brd(brd, filename="output_brd.md"):
    """Save BRD to a markdown file."""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(brd)
    print(f"\n💾 BRD saved to: {filename}")


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    problem = """
    Our HR department is struggling with a slow manual employee
    onboarding process using emails and paper forms. New employees
    often start without proper system access and HR spends too
    much time on repetitive administrative tasks. We need a
    digital solution to automate onboarding end to end.
    """

    brd = generate_brd(
        problem_statement=problem,
        system_name="HR System",
        source_type="SharePoint"
    )

    print("\n" + "=" * 55)
    print("📄 GENERATED BRD WITH CITATIONS")
    print("=" * 55)
    print(brd)
    save_brd(brd)

    print("\n✅ Generator with citations working!")