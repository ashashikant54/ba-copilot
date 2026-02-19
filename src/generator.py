# generator.py
# The generator is the final step in the pipeline:
#
#   User's business problem
#          ↓
#   Retriever finds relevant chunks
#          ↓
#   Generator builds a prompt  ← (this file)
#          ↓
#   GPT-4o-mini writes the BRD
#          ↓
#   Structured BRD + User Stories returned

import os
import sys
from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from retriever import get_relevant_context, format_context

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── The Prompt Template ───────────────────────────────────────
# This is the most important part — it tells GPT-4o-mini
# exactly what to do and how to format the output.

BRD_PROMPT = """
You are a senior Business Analyst with 15 years of experience.
Your job is to write a professional Business Requirements Document (BRD).

You have been given:
1. A business problem statement from the user
2. Relevant context retrieved from existing project documents

Use BOTH the problem statement AND the context to write the BRD.
Only include information that is supported by the context provided.
If something is not in the context, say "To be confirmed with stakeholders".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUSINESS PROBLEM:
{problem}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RELEVANT CONTEXT FROM DOCUMENTS:
{context}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Write a BRD using EXACTLY this structure:

## 1. EXECUTIVE SUMMARY
(2-3 sentences summarising what this project is and why it matters)

## 2. BUSINESS PROBLEM
(Clearly describe the current pain points and their business impact)

## 3. BUSINESS GOALS
(List 3-5 measurable goals this project must achieve)

## 4. SCOPE
### In Scope:
(What this project WILL deliver)
### Out of Scope:
(What this project will NOT deliver — important for managing expectations)

## 5. STAKEHOLDERS
| Role | Name/Team | Interest |
|------|-----------|----------|
(Fill in from context or mark as TBC)

## 6. FUNCTIONAL REQUIREMENTS
(List the specific things the system must DO — number each one)
FR-001:
FR-002:
FR-003:
(continue as needed)

## 7. NON-FUNCTIONAL REQUIREMENTS
(Performance, security, usability requirements)
NFR-001:
NFR-002:

## 8. USER STORIES
(Write 3-5 user stories in this exact format)
**Story 1:**
- As a [type of user]
- I want to [do something]
- So that [I get this benefit]
- Acceptance Criteria: [how we know it's done]

## 9. SUCCESS METRICS
(How will we measure if this project succeeded? List 3-5 metrics)

## 10. ASSUMPTIONS & RISKS
### Assumptions:
### Risks:
"""


# ── Main Generator Function ───────────────────────────────────
def generate_brd(problem_statement):
    """
    Full pipeline:
    problem statement → retrieve context → build prompt → call GPT → return BRD
    """
    print("\n" + "=" * 55)
    print("🤖 BA COPILOT — GENERATING YOUR BRD")
    print("=" * 55)

    # Step 1: Retrieve relevant context
    print("\n📚 Step 1: Searching knowledge base...")
    chunks  = get_relevant_context(problem_statement, top_k=3)
    context = format_context(chunks)
    print(f"   Found {len(chunks)} relevant sections")

    # Step 2: Build the prompt
    print("\n📝 Step 2: Building prompt...")
    prompt = BRD_PROMPT.format(
        problem=problem_statement,
        context=context
    )
    print(f"   Prompt length: {len(prompt)} characters")

    # Step 3: Call GPT-4o-mini
    print("\n🧠 Step 3: Sending to GPT-4o-mini...")
    print("   (This may take 15-30 seconds...)\n")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a senior Business Analyst. "
                           "Always write structured, professional BRDs. "
                           "Never invent information not present in the context."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0.2,   # Low = more focused, less creative
        max_tokens=2000    # Enough for a full BRD
    )

    # Step 4: Extract the BRD text
    brd = response.choices[0].message.content

    # Step 5: Show token usage (so you can track costs)
    usage = response.usage
    print(f"📊 Tokens used:")
    print(f"   Input  : {usage.prompt_tokens}")
    print(f"   Output : {usage.completion_tokens}")
    print(f"   Total  : {usage.total_tokens}")
    print(f"   Cost   : ~${usage.total_tokens * 0.00000015:.6f} USD")

    return brd


def save_brd(brd, filename="output_brd.md"):
    """Save the BRD to a markdown file."""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(brd)
    print(f"\n💾 BRD saved to: {filename}")


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":

    # This is the business problem we want the BRD for
    problem = """
    Our HR department is struggling with a slow, manual employee onboarding 
    process that relies on emails and paper forms. New employees often start 
    work without proper system access, and HR spends too much time on 
    repetitive administrative tasks. We need a digital solution that 
    automates the onboarding workflow end to end.
    """

    # Generate the BRD
    brd = generate_brd(problem)

    # Print it to screen
    print("\n" + "=" * 55)
    print("📄 YOUR GENERATED BRD")
    print("=" * 55)
    print(brd)

    # Save it to a file
    save_brd(brd)

    print("\n" + "=" * 55)
    print("✅ Generator is working!")
    print("=" * 55)