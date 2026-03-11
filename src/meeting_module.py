# meeting_module.py
# Feature #4 — Meeting Recording Processor
#
# WHAT IT DOES:
#   1. Accepts .txt, .vtt, .docx (transcript) OR .mp4 (audio/video)
#   2. Extracts text:
#      - .txt  → direct read
#      - .vtt  → parse WebVTT format (strips timestamps, keeps speaker text)
#      - .docx → python-docx paragraph extraction
#      - .mp4  → Azure AI Speech Batch Transcription API
#   3. AI analysis via GPT-4o-mini → summary, decisions, actions, gaps, topics
#   4. Saves meeting record to Azure Blob Storage (meetings container)
#   5. On human approval → indexes into KB via load_and_index_document
#
# STORAGE PATTERN:
#   Same as session_manager.py — Azure Blob Storage
#   Container: "meetings"
#   Blob name: "{meeting_id}.json"
#
# AZURE BLOB CONTAINERS:
#   sessions  → analysis sessions (existing)
#   meetings  → meeting records   (NEW)

import os
import sys
import re
import json
import uuid
import tempfile
from datetime import datetime

import requests
import time
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions
)
from azure.core.exceptions import ResourceNotFoundError
from datetime import timedelta
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from document_registry import register_document
from retriever import load_and_index_document

# ── Config ─────────────────────────────────────────────────────
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
MEETINGS_CONTAINER      = "meetings"
AZURE_SPEECH_KEY        = os.environ.get("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION     = os.environ.get("AZURE_SPEECH_REGION", "eastus")
AZURE_AUDIO_TEMP        = "meetings-audio-temp"   # Temp blob container for batch jobs

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# ── Azure Blob Helpers (same pattern as session_manager.py) ────
def _get_meetings_container():
    """Connect to Azure Blob Storage and return the meetings container."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError(
            "❌ AZURE_STORAGE_CONNECTION_STRING is not set!\n"
            "  On your laptop: add it to your .env file\n"
            "  On Azure: add it in App Service → Configuration → App Settings"
        )
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container    = blob_service.get_container_client(MEETINGS_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass  # Container already exists — fine
    return container


def _save_meeting(meeting: dict):
    """Save meeting record to Azure Blob Storage."""
    meeting["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    blob_name = f"{meeting['meeting_id']}.json"
    data      = json.dumps(meeting, indent=2)
    container = _get_meetings_container()
    container.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,
        encoding="utf-8"
    )


def load_meeting(meeting_id: str) -> dict:
    """Load a meeting record from Azure Blob Storage."""
    try:
        container = _get_meetings_container()
        blob      = container.get_blob_client(f"{meeting_id}.json")
        data      = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except ResourceNotFoundError:
        raise FileNotFoundError(f"Meeting '{meeting_id}' not found in Azure Blob Storage")


def list_meetings() -> list:
    """Return all meeting records, newest first."""
    container = _get_meetings_container()
    meetings  = []
    for blob_item in container.list_blobs():
        if blob_item.name.endswith(".json"):
            try:
                blob = container.get_blob_client(blob_item.name)
                data = blob.download_blob().readall()
                m    = json.loads(data.decode("utf-8"))
                meetings.append({
                    "meeting_id":   m["meeting_id"],
                    "title":        m["title"],
                    "system_name":  m.get("system_name", ""),
                    "file_type":    m.get("file_type", ""),
                    "file_size_kb": m.get("file_size_kb", 0),
                    "kb_stored":    m.get("kb_stored", False),
                    "created_at":   m["created_at"],
                    "updated_at":   m["updated_at"],
                    # One-liner preview of summary
                    "summary_preview": (
                        m.get("summary", "")[:120] + "..."
                        if len(m.get("summary", "")) > 120
                        else m.get("summary", "")
                    )
                })
            except Exception:
                pass
    meetings.sort(key=lambda x: x["updated_at"], reverse=True)
    return meetings


# ── Text Extraction ─────────────────────────────────────────────

def _extract_txt(file_path: str) -> str:
    """Read plain text file."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def _extract_vtt(file_path: str) -> str:
    """
    Parse WebVTT transcript format.
    Strips timestamps and cue headers, keeps speaker text lines.

    VTT format looks like:
        WEBVTT
        00:00:01.000 --> 00:00:04.000
        John: Hello everyone, let's get started.
        00:00:05.000 --> 00:00:08.000
        Jane: Thanks for joining.
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()

    lines       = raw.splitlines()
    text_lines  = []
    # Regex: skip WEBVTT header, timestamp lines (00:00:00.000 --> ...), blank lines, cue numbers
    ts_pattern  = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->")
    num_pattern = re.compile(r"^\d+$")

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if ts_pattern.match(line):
            continue
        if num_pattern.match(line):
            continue
        # Strip VTT cue tags like <c>, </c>, <00:00:00.000>
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line:
            text_lines.append(line)

    return "\n".join(text_lines)


def _extract_docx(file_path: str) -> str:
    """Extract text from Word document using python-docx."""
    try:
        from docx import Document
    except ImportError:
        raise ImportError(
            "python-docx is required for .docx files. "
            "Run: pip install python-docx"
        )
    doc   = Document(file_path)
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(lines)


def _extract_mp4(file_path: str) -> str:
    """
    Transcribe MP4 audio using Azure AI Speech Batch Transcription API.

    WHY BATCH TRANSCRIPTION (not SDK streaming or OpenAI Whisper):
    ─────────────────────────────────────────────────────────────────
    - Any file length   → enterprise meetings are 30–90 min (Whisper caps at ~25MB)
    - Speaker labels    → "Speaker 1: ..." lines, critical for BA meeting notes
    - Data residency    → audio never leaves your Azure tenant (FERPA/compliance ready)
    - Cost              → $0.017/min vs $0.006/min for Whisper, but covered by Azure credits
    - 5 hrs/month FREE  → more than enough for typical BA team usage

    FLOW:
    ─────
    1. Upload MP4  → Azure Blob (meetings-audio-temp container)
    2. Get SAS URL → time-limited read URL for the batch job
    3. Submit job  → POST to Azure Speech batch transcription REST API
    4. Poll status → typically ~1 min per 10 min of audio
    5. Get results → download JSON, parse speaker-labelled phrases
    6. Cleanup     → delete temp blob + batch job

    ENV VARS REQUIRED:
    ──────────────────
    AZURE_SPEECH_KEY    — from Azure Portal → Speech resource → Keys and Endpoint
    AZURE_SPEECH_REGION — e.g. "eastus" (must match your Speech resource region)
    """
    if not AZURE_SPEECH_KEY:
        raise EnvironmentError(
            "❌ AZURE_SPEECH_KEY is not set!\n"
            "   Get it from: Azure Portal → your Speech resource → Keys and Endpoint\n"
            "   On your laptop: add to .env\n"
            "   On Azure App Service: add to Configuration → App Settings"
        )
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError("AZURE_STORAGE_CONNECTION_STRING is not set!")

    base_url = f"https://{AZURE_SPEECH_REGION}.api.cognitive.microsoft.com/speechtotext/v3.2"
    headers  = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type":              "application/json"
    }

    # ── Step 1: Upload MP4 to Azure Blob (temp container) ───────
    print("   ☁️  Uploading audio to Azure Blob (temp)...")
    blob_service   = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    temp_container = blob_service.get_container_client(AZURE_AUDIO_TEMP)
    try:
        temp_container.create_container()
    except Exception:
        pass  # Already exists — fine

    blob_name = f"temp_{uuid.uuid4().hex}.mp4"
    with open(file_path, "rb") as f:
        temp_container.upload_blob(name=blob_name, data=f, overwrite=True)
    print(f"   ✅ Uploaded (blob: {blob_name})")

    # ── Step 2: Generate SAS URL (2-hour window) ─────────────────
    # Parse account name + key from connection string
    # Format: "DefaultEndpointsProtocol=https;AccountName=xxx;AccountKey=yyy;..."
    conn_parts   = {
        kv.split("=", 1)[0]: kv.split("=", 1)[1]
        for kv in AZURE_CONNECTION_STRING.split(";")
        if "=" in kv
    }
    account_name = conn_parts.get("AccountName", "")
    account_key  = conn_parts.get("AccountKey", "")

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=AZURE_AUDIO_TEMP,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=2)
    )
    audio_url = (
        f"https://{account_name}.blob.core.windows.net"
        f"/{AZURE_AUDIO_TEMP}/{blob_name}?{sas_token}"
    )

    transcript = ""

    try:
        # ── Step 3: Submit batch transcription job ───────────────
        print("   🎙️  Submitting batch transcription job to Azure Speech...")

        job_payload = {
            "contentUrls": [audio_url],
            "locale":      "en-US",
            "displayName": f"coanalytica_{uuid.uuid4().hex[:8]}",
            "properties": {
                "wordLevelTimestampsEnabled": False,
                "diarizationEnabled":         True,   # Speaker 1 / Speaker 2 labels
                "punctuationMode":            "DictatedAndAutomatic",
                "profanityFilterMode":        "None",
            }
        }

        create_res = requests.post(
            f"{base_url}/transcriptions",
            headers=headers,
            json=job_payload,
            timeout=30
        )

        if create_res.status_code not in (200, 201):
            raise RuntimeError(
                f"Azure Speech API error {create_res.status_code}: "
                f"{create_res.text[:400]}"
            )

        transcription_url = create_res.json()["self"]
        print(f"   ✅ Job submitted — polling for completion...")

        # ── Step 4: Poll until Succeeded or Failed ───────────────
        # Typical speed: ~1 min processing per 10 min of audio
        max_wait  = 7200   # 2 hour absolute timeout
        interval  = 10     # poll every 10 seconds
        elapsed   = 0

        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval

            status_res  = requests.get(transcription_url, headers=headers, timeout=30)
            status_res.raise_for_status()
            status_data = status_res.json()
            status      = status_data.get("status", "Unknown")

            print(f"   ⏳ [{elapsed:4d}s] Status: {status}")

            if status == "Succeeded":
                break
            elif status == "Failed":
                err = status_data.get("properties", {}).get("error", {})
                raise RuntimeError(
                    f"Azure Speech transcription failed: "
                    f"{err.get('message', 'Unknown error')} "
                    f"(code: {err.get('code', 'N/A')})"
                )
            # "Running" / "NotStarted" → keep polling

        else:
            raise TimeoutError(
                f"Azure Speech transcription timed out after {max_wait}s. "
                "Try a shorter recording or increase the timeout."
            )

        # ── Step 5: Download and parse transcript ────────────────
        files_res = requests.get(
            f"{transcription_url}/files",
            headers=headers,
            timeout=30
        )
        files_res.raise_for_status()
        files = files_res.json().get("values", [])

        # Find the transcription output file (not the report)
        transcript_file = next(
            (f for f in files if f.get("kind") == "Transcription"),
            None
        )

        if not transcript_file:
            raise ValueError(
                "No transcription output file found in Azure batch results. "
                "The job may have succeeded with no recognized speech."
            )

        content_url = transcript_file["links"]["contentUrl"]
        content_res = requests.get(content_url, timeout=60)
        content_res.raise_for_status()
        content     = content_res.json()

        # Parse phrases — include speaker label if diarization worked
        # Each phrase has: speaker (int), nBest[0].display (string)
        phrases = []
        for phrase in content.get("recognizedPhrases", []):
            best = phrase.get("nBest", [{}])[0]
            text = best.get("display", "").strip()
            if not text:
                continue
            speaker = phrase.get("speaker")
            if speaker is not None:
                phrases.append(f"Speaker {speaker}: {text}")
            else:
                phrases.append(text)

        if not phrases:
            raise ValueError(
                "Azure Speech returned zero recognized phrases. "
                "Check that the audio file contains clear speech."
            )

        transcript = "\n".join(phrases)
        print(
            f"   ✅ Transcription complete — "
            f"{len(transcript)} chars, {len(phrases)} phrases"
        )

        # Best-effort cleanup of the batch job record
        try:
            requests.delete(transcription_url, headers=headers, timeout=30)
        except Exception:
            pass

    finally:
        # ── Step 6: Always delete the temp audio blob ────────────
        # This runs whether transcription succeeded or failed
        try:
            temp_container.delete_blob(blob_name)
            print("   🗑️  Temp audio blob deleted")
        except Exception:
            pass  # Best effort — blob will expire anyway

    return transcript


def extract_text_from_file(file_path: str, ext: str) -> str:
    """
    Route to the correct extractor based on file extension.
    Returns the full plain-text transcript/content.
    """
    ext = ext.lower()
    if ext == ".txt":
        return _extract_txt(file_path)
    elif ext == ".vtt":
        return _extract_vtt(file_path)
    elif ext == ".docx":
        return _extract_docx(file_path)
    elif ext == ".mp4":
        return _extract_mp4(file_path)   # → Azure Batch Transcription
    else:
        raise ValueError(f"Unsupported file type: '{ext}'. Use .txt, .vtt, .docx, or .mp4")


# ── AI Analysis Prompt ──────────────────────────────────────────

MEETING_ANALYSIS_PROMPT = """
You are an expert Business Analyst reviewing a meeting transcript.

Extract structured information from the transcript below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MEETING TITLE: {title}
SYSTEM CONTEXT: {system_name}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TRANSCRIPT:
{transcript}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Respond ONLY with valid JSON. No markdown, no preamble, no explanation.

{{
  "summary": "2-4 sentence executive summary of the meeting purpose and key outcomes",
  "key_topics": ["topic 1", "topic 2", "topic 3"],
  "decisions": [
    {{
      "decision": "The exact decision made",
      "owner": "Person or team responsible (or 'TBC')",
      "context": "Brief context for why this decision was made"
    }}
  ],
  "action_items": [
    {{
      "action": "Specific task to be done",
      "owner": "Person or team assigned (or 'TBC')",
      "due_date": "Due date if mentioned, otherwise 'TBC'",
      "priority": "High / Medium / Low"
    }}
  ],
  "open_questions": [
    {{
      "question": "Unresolved question or open item",
      "directed_to": "Who should answer (or 'TBC')",
      "impact": "Why this matters"
    }}
  ],
  "participants": ["Name 1", "Name 2"],
  "ba_insights": "1-2 sentences on what requirements or gaps this meeting reveals for the BA"
}}
"""


# ── Core Processing Function ────────────────────────────────────

def process_meeting(
    title:        str,
    system_name:  str,
    file_path:    str,
    filename:     str,
    file_size_kb: float
) -> dict:
    """
    Full pipeline:
      1. Extract text from file (txt/vtt/docx/mp4)
      2. AI analysis via GPT-4o-mini
      3. Save meeting record to Azure Blob Storage
      4. Return meeting record

    Does NOT store to KB — that's a separate human-approval step.
    """
    meeting_id = str(uuid.uuid4())[:8]
    ext        = os.path.splitext(filename)[1].lower()

    print(f"\n📋 Processing meeting: '{title}' (ID: {meeting_id})")
    print(f"   File: {filename} ({file_size_kb} KB, type: {ext})")

    # ── Step 1: Extract text ──────────────────────────────────
    print(f"📄 Step 1/2: Extracting text from {ext} file...")
    transcript = extract_text_from_file(file_path, ext)

    if not transcript.strip():
        raise ValueError(
            f"No text could be extracted from '{filename}'. "
            "Please check the file has content."
        )

    print(f"   Extracted {len(transcript)} characters of text")

    # Truncate very long transcripts to stay within GPT token limits
    # ~12,000 chars ≈ 3,000 tokens — safe for gpt-4o-mini context
    transcript_for_ai = transcript[:12000]
    if len(transcript) > 12000:
        transcript_for_ai += "\n\n[Transcript truncated for processing — full text stored]"
        print(f"   ⚠️ Truncated to 12,000 chars for AI (full text saved)")

    # ── Step 2: AI Analysis ───────────────────────────────────
    print(f"🧠 Step 2/2: Running AI analysis with GPT-4o-mini...")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert Business Analyst. "
                    "Extract structured meeting information and respond ONLY with valid JSON. "
                    "Never include markdown code fences or any text outside the JSON object."
                )
            },
            {
                "role": "user",
                "content": MEETING_ANALYSIS_PROMPT.format(
                    title=title,
                    system_name=system_name or "Not specified",
                    transcript=transcript_for_ai
                )
            }
        ],
        temperature=0.1,
        max_tokens=2000
    )

    raw_response = response.choices[0].message.content.strip()

    # Strip any accidental markdown fences
    raw_response = re.sub(r"^```json\s*", "", raw_response)
    raw_response = re.sub(r"\s*```$",      "", raw_response)

    try:
        analysis = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"AI returned invalid JSON: {e}\n"
            f"Raw response: {raw_response[:500]}"
        )

    print(f"   ✅ AI analysis complete")
    print(f"   Summary: {analysis.get('summary', '')[:80]}...")
    print(f"   Decisions:    {len(analysis.get('decisions', []))}")
    print(f"   Action items: {len(analysis.get('action_items', []))}")
    print(f"   Open questions: {len(analysis.get('open_questions', []))}")

    # ── Step 3: Save meeting record ───────────────────────────
    meeting = {
        "meeting_id":    meeting_id,
        "title":         title,
        "system_name":   system_name or "",
        "filename":      filename,
        "file_type":     ext,
        "file_size_kb":  file_size_kb,
        "created_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

        # Full transcript (stored for KB indexing later)
        "transcript":    transcript,

        # AI analysis results
        "summary":           analysis.get("summary", ""),
        "key_topics":        analysis.get("key_topics", []),
        "decisions":         analysis.get("decisions", []),
        "action_items":      analysis.get("action_items", []),
        "open_questions":    analysis.get("open_questions", []),
        "participants":      analysis.get("participants", []),
        "ba_insights":       analysis.get("ba_insights", ""),

        # KB storage state
        "kb_stored":         False,
        "kb_system_name":    None,
        "kb_source_type":    None,
        "kb_document_id":    None,
    }

    _save_meeting(meeting)
    print(f"✅ Meeting record saved (ID: {meeting_id})")
    return meeting


# ── KB Storage Function ─────────────────────────────────────────

def store_meeting_to_kb(
    meeting_id:  str,
    system_name: str,
    source_type: str
) -> dict:
    """
    Human-approved step: Index meeting transcript into the Knowledge Base.

    What it does:
      1. Loads meeting record from Azure Blob
      2. Writes transcript + summary to a temp .txt file
      3. Calls load_and_index_document (same as KB upload)
      4. Calls register_document (same as KB upload)
      5. Updates meeting record with kb_stored=True

    This means the meeting transcript becomes searchable in RAG queries —
    future analysis sessions can pull context from past meeting discussions.
    """
    meeting = load_meeting(meeting_id)

    if meeting.get("kb_stored"):
        raise ValueError(
            f"Meeting '{meeting_id}' has already been stored in the Knowledge Base."
        )

    print(f"\n💾 Storing meeting '{meeting['title']}' to Knowledge Base...")
    print(f"   System: {system_name} → {source_type}")

    # Build a rich text document that includes summary + full transcript
    # This format gives the RAG retriever the best context to work with
    kb_content = f"""MEETING TRANSCRIPT
==================
Title: {meeting['title']}
Date: {meeting['created_at']}
System: {meeting.get('system_name', 'N/A')}
Participants: {', '.join(meeting.get('participants', ['Unknown']))}

EXECUTIVE SUMMARY
-----------------
{meeting.get('summary', '')}

BA INSIGHTS
-----------
{meeting.get('ba_insights', '')}

KEY DECISIONS
-------------
{_format_decisions_for_kb(meeting.get('decisions', []))}

ACTION ITEMS
------------
{_format_actions_for_kb(meeting.get('action_items', []))}

OPEN QUESTIONS
--------------
{_format_questions_for_kb(meeting.get('open_questions', []))}

FULL TRANSCRIPT
---------------
{meeting.get('transcript', '')}
"""

    # Write to temp file (same pattern as main.py /upload endpoint)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    try:
        tmp.write(kb_content.encode("utf-8"))
        tmp.flush()
        tmp.close()

        # Index into Azure AI Search (same function as regular KB upload)
        chunks = load_and_index_document(
            file_path=tmp.name,
            system_name=system_name,
            source_type=source_type
        )

        # Register in document registry (same function as regular KB upload)
        kb_filename = f"meeting_{meeting_id}_{meeting['title'][:40].replace(' ', '_')}.txt"
        kb_filename = re.sub(r"[^\w\-_.]", "", kb_filename)  # sanitize

        record = register_document(
            document_name=kb_filename,
            system_name=system_name,
            source_type=source_type,
            chunks=chunks,
            file_size_kb=round(len(kb_content.encode("utf-8")) / 1024, 1)
        )

        print(f"   ✅ Indexed {chunks} chunks into '{system_name} → {source_type}'")

    except Exception as e:
        raise RuntimeError(f"Failed to store meeting to KB: {e}")

    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)

    # Update meeting record
    meeting["kb_stored"]      = True
    meeting["kb_system_name"] = system_name
    meeting["kb_source_type"] = source_type
    meeting["kb_document_id"] = record.get("id", "")
    _save_meeting(meeting)

    print(f"✅ Meeting stored in Knowledge Base (document ID: {record.get('id', '')})")
    return meeting


# ── KB Formatting Helpers ───────────────────────────────────────

def _format_decisions_for_kb(decisions: list) -> str:
    if not decisions:
        return "No decisions recorded."
    lines = []
    for i, d in enumerate(decisions, 1):
        lines.append(
            f"{i}. {d.get('decision', '')}\n"
            f"   Owner: {d.get('owner', 'TBC')}\n"
            f"   Context: {d.get('context', '')}"
        )
    return "\n".join(lines)


def _format_actions_for_kb(actions: list) -> str:
    if not actions:
        return "No action items recorded."
    lines = []
    for i, a in enumerate(actions, 1):
        lines.append(
            f"{i}. [{a.get('priority', 'Medium')}] {a.get('action', '')}\n"
            f"   Owner: {a.get('owner', 'TBC')} | Due: {a.get('due_date', 'TBC')}"
        )
    return "\n".join(lines)


def _format_questions_for_kb(questions: list) -> str:
    if not questions:
        return "No open questions recorded."
    lines = []
    for i, q in enumerate(questions, 1):
        lines.append(
            f"{i}. {q.get('question', '')}\n"
            f"   Directed to: {q.get('directed_to', 'TBC')} | Impact: {q.get('impact', '')}"
        )
    return "\n".join(lines)
