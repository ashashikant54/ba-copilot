# meeting_module.py
# Feature #4 — Meeting Recording Processor
# Phase 2 Sprint 2 (A8 step 3) — org_id tenancy for meetings.
#
# ARCHITECTURE (rewritten for large-file support):
#   The meetings pipeline is decomposed into three HTTP transactions:
#     1. POST /meetings/init   → creates record, returns write-SAS URL
#     2. Browser PUTs file directly to Azure Blob via SAS (never touches FastAPI)
#     3. POST /meetings/start  → spawns BackgroundTask for the pipeline
#   The pipeline runs off the HTTP path and updates the meeting record after
#   each step. The browser polls GET /meetings/{id}/status every 5s.
#
# STATUS LIFECYCLE (locked strings, used verbatim in backend + frontend):
#   pending → transcribing|extracting → analyzing → completed
#   Any step can transition to "failed" on exception.
#   "uploading" is browser-side only, never persisted.
#
# TEXT EXTRACTION:
#   .txt  → direct read
#   .vtt  → parse WebVTT format (strips timestamps, keeps speaker text)
#   .docx → python-docx paragraph extraction
#   .mp4  → Azure AI Speech Batch Transcription API (unchanged engine)
#
# STORAGE PATTERN (Sprint 2):
#   Container: "meetings"           — meeting JSON records
#   Container: "meetings-audio-temp" — uploaded files (browser → SAS → blob)
#   Blob name: "{org_id}/{meeting_id}.json"
#   Legacy Phase 1 blobs live at root and are still readable via fallback.
#
# FUTURE SWAP:
#   run_meeting_pipeline() is a standalone sync function. To move to an
#   Azure Queue + Function worker, import and call it from the Function —
#   no FastAPI dependency, no async gymnastics.
 
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
from prompt_manager import get_prompt, get_model_config, estimate_cost, get_prompt_version
 
load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from document_registry import register_document
from retriever import load_and_index_document
from session_manager import _resolve_org_id, DEFAULT_ORG_ID
 
# ── Config ─────────────────────────────────────────────────────
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
MEETINGS_CONTAINER      = "meetings"
AZURE_SPEECH_KEY        = os.environ.get("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION     = os.environ.get("AZURE_SPEECH_REGION", "eastus")
AZURE_AUDIO_TEMP        = "meetings-audio-temp"   # Temp blob container for batch jobs

ALLOWED_EXTENSIONS = {".txt", ".vtt", ".docx", ".mp4"}

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
 
 
# ── Azure Blob Helpers ─────────────────────────────────────────

def _parse_account_parts():
    """Extract AccountName and AccountKey from the connection string."""
    conn_parts = {
        kv.split("=", 1)[0]: kv.split("=", 1)[1]
        for kv in AZURE_CONNECTION_STRING.split(";")
        if "=" in kv
    }
    return conn_parts.get("AccountName", ""), conn_parts.get("AccountKey", "")


def _get_temp_container():
    """Return the meetings-audio-temp container client, creating if needed."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError("AZURE_STORAGE_CONNECTION_STRING is not set.")
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container = blob_service.get_container_client(AZURE_AUDIO_TEMP)
    try:
        container.create_container()
    except Exception:
        pass
    return container


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
    """Save meeting record to Azure Blob Storage under its org_id prefix."""
    meeting["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    org_id           = _resolve_org_id(meeting.get("org_id"))
    meeting["org_id"] = org_id   # normalise in case it was missing (legacy load)
    blob_name = f"{org_id}/{meeting['meeting_id']}.json"
    data      = json.dumps(meeting, indent=2)
    container = _get_meetings_container()
    container.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,
        encoding="utf-8"
    )


def load_meeting(meeting_id: str, org_id: str = None) -> dict:
    """Load a meeting record from Azure Blob Storage.

    Primary path:  meetings/{org_id}/{meeting_id}.json
    LEGACY FALLBACK: meetings/{meeting_id}.json  (Phase 1 root-path blobs)

    Remove the fallback block in a future sprint once all Phase 1 meeting
    blobs are confirmed migrated (a save on a legacy-loaded meeting rewrites
    it under the org prefix automatically).
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_meetings_container()

    try:
        blob = container.get_blob_client(f"{org_id}/{meeting_id}.json")
        data = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except ResourceNotFoundError:
        pass   # fall through to legacy

    # ── LEGACY FALLBACK (remove once all Phase 1 meeting blobs migrated) ──
    try:
        blob = container.get_blob_client(f"{meeting_id}.json")
        data = blob.download_blob().readall()
        meeting = json.loads(data.decode("utf-8"))
        meeting.setdefault("org_id", DEFAULT_ORG_ID)
        return meeting
    except ResourceNotFoundError:
        raise FileNotFoundError(
            f"Meeting '{meeting_id}' not found in Azure Blob Storage "
            f"(checked {org_id}/{meeting_id}.json and legacy {meeting_id}.json)"
        )


def list_meetings(org_id: str = None) -> list:
    """Return meeting records for an org, newest first.

    Scoping via list_blobs(name_starts_with=...) so cross-org blobs are
    never downloaded. The default org also sweeps Phase 1 root-path blobs
    via LEGACY FALLBACK, deduped by meeting_id.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_meetings_container()
    meetings  = []
    seen_ids  = set()

    def _accumulate(blob_name):
        try:
            blob = container.get_blob_client(blob_name)
            data = blob.download_blob().readall()
            m    = json.loads(data.decode("utf-8"))
            if m["meeting_id"] in seen_ids:
                return   # org-prefixed copy already captured
            seen_ids.add(m["meeting_id"])
            meetings.append({
                "meeting_id":   m["meeting_id"],
                "title":        m["title"],
                "status":       m.get("status", "completed"),  # legacy meetings lack status
                "system_name":  m.get("system_name", ""),
                "file_type":    m.get("file_type", ""),
                "file_size_kb": m.get("file_size_kb", 0),
                "kb_stored":    m.get("kb_stored", False),
                "created_at":   m["created_at"],
                "updated_at":   m["updated_at"],
                "summary_preview": (
                    m.get("summary", "")[:120] + "..."
                    if len(m.get("summary", "")) > 120
                    else m.get("summary", "")
                )
            })
        except Exception:
            pass

    # Primary scope — meetings/{org_id}/*.json
    for blob_item in container.list_blobs(name_starts_with=f"{org_id}/"):
        if blob_item.name.endswith(".json"):
            _accumulate(blob_item.name)

    # ── LEGACY FALLBACK (default org only — Phase 1 root-path blobs) ──
    if org_id == DEFAULT_ORG_ID:
        for blob_item in container.list_blobs():
            if blob_item.name.endswith(".json") and "/" not in blob_item.name:
                _accumulate(blob_item.name)

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
 
 
def _extract_text_from_blob(blob_name: str) -> str:
    """Download a non-MP4 file from temp blob and extract text.

    For .txt/.vtt/.docx files the blob is small enough to download into a
    temp file and route through the existing extractors.
    """
    container = _get_temp_container()
    blob      = container.get_blob_client(blob_name)
    ext       = os.path.splitext(blob_name)[1].lower()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        data = blob.download_blob().readall()
        tmp.write(data)
        tmp.flush()
        tmp.close()

        if ext == ".txt":
            return _extract_txt(tmp.name)
        elif ext == ".vtt":
            return _extract_vtt(tmp.name)
        elif ext == ".docx":
            return _extract_docx(tmp.name)
        else:
            raise ValueError(f"Unsupported text file type: '{ext}'")
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)


def _transcribe_from_blob(blob_name: str) -> str:
    """Transcribe an MP4 already in meetings-audio-temp via Azure Speech.

    This is the refactored _extract_mp4. The browser uploaded the file
    directly to blob via SAS, so the local-file-upload step is gone.
    Steps:
      1. Generate a READ SAS for the existing blob
      2. Submit Azure Speech batch transcription job
      3. Poll until Succeeded / Failed
      4. Download + parse speaker-labelled transcript
      5. Best-effort cleanup of the batch job record
    Temp blob cleanup is handled by run_meeting_pipeline's finally block.
    """
    if not AZURE_SPEECH_KEY:
        raise EnvironmentError(
            "AZURE_SPEECH_KEY is not set. "
            "Get it from: Azure Portal → Speech resource → Keys and Endpoint."
        )

    account_name, account_key = _parse_account_parts()

    # Read SAS for the already-uploaded blob
    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=AZURE_AUDIO_TEMP,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=2),
    )
    audio_url = (
        f"https://{account_name}.blob.core.windows.net"
        f"/{AZURE_AUDIO_TEMP}/{blob_name}?{sas_token}"
    )

    base_url = f"https://{AZURE_SPEECH_REGION}.api.cognitive.microsoft.com/speechtotext/v3.2"
    headers  = {
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Content-Type":              "application/json",
    }

    # Submit batch transcription job
    print("   Submitting batch transcription job to Azure Speech...")
    job_payload = {
        "contentUrls": [audio_url],
        "locale":      "en-US",
        "displayName": f"coanalytica_{uuid.uuid4().hex[:8]}",
        "properties": {
            "wordLevelTimestampsEnabled": False,
            "diarizationEnabled":         True,
            "punctuationMode":            "DictatedAndAutomatic",
            "profanityFilterMode":        "None",
        },
    }

    create_res = requests.post(
        f"{base_url}/transcriptions",
        headers=headers,
        json=job_payload,
        timeout=30,
    )
    if create_res.status_code not in (200, 201):
        raise RuntimeError(
            f"Azure Speech API error {create_res.status_code}: "
            f"{create_res.text[:400]}"
        )

    transcription_url = create_res.json()["self"]
    print(f"   Job submitted — polling for completion...")

    # Poll until Succeeded or Failed
    max_wait = 7200
    interval = 10
    elapsed  = 0

    while elapsed < max_wait:
        time.sleep(interval)
        elapsed += interval

        status_res  = requests.get(transcription_url, headers=headers, timeout=30)
        status_res.raise_for_status()
        status_data = status_res.json()
        status      = status_data.get("status", "Unknown")

        print(f"   [{elapsed:4d}s] Status: {status}")

        if status == "Succeeded":
            break
        elif status == "Failed":
            err = status_data.get("properties", {}).get("error", {})
            raise RuntimeError(
                f"Azure Speech transcription failed: "
                f"{err.get('message', 'Unknown error')} "
                f"(code: {err.get('code', 'N/A')})"
            )
    else:
        raise TimeoutError(
            f"Azure Speech transcription timed out after {max_wait}s. "
            "Try a shorter recording or increase the timeout."
        )

    # Download and parse transcript
    files_res = requests.get(
        f"{transcription_url}/files", headers=headers, timeout=30,
    )
    files_res.raise_for_status()
    files = files_res.json().get("values", [])

    transcript_file = next(
        (f for f in files if f.get("kind") == "Transcription"), None,
    )
    if not transcript_file:
        raise ValueError(
            "No transcription output file found in Azure batch results."
        )

    content_url = transcript_file["links"]["contentUrl"]
    content_res = requests.get(content_url, timeout=60)
    content_res.raise_for_status()
    content     = content_res.json()

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
    print(f"   Transcription complete — {len(transcript)} chars, {len(phrases)} phrases")

    # Best-effort cleanup of the batch job record (not the blob)
    try:
        requests.delete(transcription_url, headers=headers, timeout=30)
    except Exception:
        pass

    return transcript


# ── AI Analysis (extracted from old process_meeting) ───────────

def _run_ai_analysis(title: str, system_name: str, transcript: str) -> tuple:
    """Run GPT-4o-mini analysis on a transcript.

    Returns (analysis_dict, prompt_ver, input_tokens, output_tokens, cost).
    """
    # Truncate to stay within GPT token limits (~12k chars ≈ 3k tokens)
    transcript_for_ai = transcript[:12000]
    if len(transcript) > 12000:
        transcript_for_ai += "\n\n[Transcript truncated for processing — full text stored]"

    prompt_cfg = get_prompt("meetings", "analysis")
    model_cfg  = get_model_config("meetings", "analysis")
    prompt_ver = get_prompt_version("meetings", "analysis")
    print(f"   AI analysis ({model_cfg['model']}, prompt v{prompt_ver})...")

    response = client.chat.completions.create(
        model=model_cfg["model"],
        messages=[
            {"role": "system", "content": prompt_cfg["system"]},
            {
                "role": "user",
                "content": prompt_cfg["user_template"].format(
                    title=title,
                    system_name=system_name or "Not specified",
                    transcript=transcript_for_ai,
                ),
            },
        ],
        temperature=model_cfg["temperature"],
        max_tokens=model_cfg["max_tokens"],
    )

    raw = response.choices[0].message.content.strip()
    usage         = response.usage
    input_tokens  = usage.prompt_tokens     if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    call_cost     = estimate_cost(input_tokens, output_tokens)
    print(f"   {input_tokens}in/{output_tokens}out tokens | ${call_cost:.6f}")

    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$",      "", raw)

    try:
        analysis = json.loads(raw.strip())
    except json.JSONDecodeError as e:
        raise ValueError(
            f"AI returned invalid JSON: {e}\nRaw response: {raw[:500]}"
        )

    return analysis, prompt_ver, input_tokens, output_tokens, call_cost


# ══════════════════════════════════════════════════════════════
# PUBLIC API — init, pipeline, status
# ══════════════════════════════════════════════════════════════

def init_meeting(
    title: str, filename: str, size_bytes: int,
    system_name: str = "", org_id: str = None,
) -> dict:
    """Create a pending meeting record and return a write-SAS URL.

    The browser PUTs the file directly to the SAS URL — file bytes never
    touch FastAPI. Returns {meeting_id, blob_name, sas_upload_url}.
    """
    org_id = _resolve_org_id(org_id)
    ext    = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"File type '{ext}' not supported. Use: {sorted(ALLOWED_EXTENSIONS)}"
        )

    meeting_id = uuid.uuid4().hex[:8]
    blob_name  = f"{meeting_id}_{uuid.uuid4().hex[:6]}{ext}"

    # Generate a WRITE-scoped SAS URL (2-hour expiry)
    account_name, account_key = _parse_account_parts()
    _get_temp_container()   # ensure container exists

    sas_token = generate_blob_sas(
        account_name=account_name,
        container_name=AZURE_AUDIO_TEMP,
        blob_name=blob_name,
        account_key=account_key,
        permission=BlobSasPermissions(create=True, write=True),
        expiry=datetime.utcnow() + timedelta(hours=2),
    )
    sas_upload_url = (
        f"https://{account_name}.blob.core.windows.net"
        f"/{AZURE_AUDIO_TEMP}/{blob_name}?{sas_token}"
    )

    # Create a minimal meeting record with status="pending"
    meeting = {
        "meeting_id":     meeting_id,
        "org_id":         org_id,
        "status":         "pending",
        "title":          title,
        "system_name":    system_name or "",
        "filename":       filename,
        "file_type":      ext,
        "file_size_kb":   round(size_bytes / 1024, 1),
        "blob_name":      blob_name,
        "created_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "progress_message": "Waiting for file upload...",
        # These will be populated by the pipeline:
        "transcript":     "",
        "summary":        "",
        "key_topics":     [],
        "decisions":      [],
        "action_items":   [],
        "open_questions": [],
        "participants":   [],
        "ba_insights":    "",
        "kb_stored":      False,
        "kb_system_name": None,
        "kb_source_type": None,
        "kb_document_id": None,
        "prompt_version": None,
        "input_tokens":   0,
        "output_tokens":  0,
        "estimated_cost_usd": 0.0,
        "error":          None,
    }
    _save_meeting(meeting)
    print(f"Meeting init: {meeting_id} ({filename}, {size_bytes} bytes)")

    return {
        "meeting_id":     meeting_id,
        "blob_name":      blob_name,
        "sas_upload_url": sas_upload_url,
    }


def check_blob_exists(blob_name: str) -> bool:
    """Return True if the blob exists in meetings-audio-temp."""
    try:
        container = _get_temp_container()
        container.get_blob_client(blob_name).get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False


def claim_meeting_for_processing(meeting_id: str, org_id: str = None) -> str:
    """Atomically transition a meeting from "pending" to the first processing status.

    Loads the record, verifies status == "pending", writes the appropriate
    next status ("transcribing" for MP4, "extracting" for text files),
    and saves. The persisted status change acts as a lock — a concurrent
    /meetings/start call will see the non-pending status and 409.

    Returns the claimed status string. Raises ValueError if the meeting
    is not in "pending" status.
    """
    org_id  = _resolve_org_id(org_id)
    meeting = load_meeting(meeting_id, org_id=org_id)

    if meeting.get("status") != "pending":
        raise ValueError(
            f"Meeting is not in 'pending' status (current: {meeting.get('status')})"
        )

    ext = meeting.get("file_type", "").lower()
    if ext == ".mp4":
        claimed_status  = "transcribing"
        progress_msg    = "Transcribing audio via Azure Speech..."
    else:
        claimed_status  = "extracting"
        progress_msg    = f"Extracting text from {ext} file..."

    meeting["status"]           = claimed_status
    meeting["progress_message"] = progress_msg
    _save_meeting(meeting)
    print(f"Claimed meeting {meeting_id} → {claimed_status}")
    return claimed_status


def run_meeting_pipeline(meeting_id: str, org_id: str = None) -> None:
    """Background pipeline: transcribe/extract → analyse → save.

    This is a standalone sync function so it can be moved to an Azure
    Function worker later with no FastAPI dependency. FastAPI's
    BackgroundTasks runs sync functions in a thread pool automatically.

    IMPORTANT: The caller (POST /meetings/start) has already transitioned
    the status from "pending" to "transcribing" or "extracting" via
    claim_meeting_for_processing. This function proceeds from that state —
    it does NOT re-check or re-set the initial processing status.

    Status transitions persisted to blob after each step:
      (already set) transcribing|extracting → analyzing → completed
    On any exception → failed (with error detail).
    """
    org_id  = _resolve_org_id(org_id)
    meeting = load_meeting(meeting_id, org_id=org_id)
    ext     = meeting.get("file_type", "").lower()
    blob_name = meeting.get("blob_name", "")

    try:
        # ── Step 1: Transcription / text extraction ──────────────
        # Status was already set to "transcribing" or "extracting" by
        # claim_meeting_for_processing. We only update progress_message.
        if ext == ".mp4":
            meeting["progress_message"] = "Transcribing audio via Azure Speech..."
            _save_meeting(meeting)
            transcript = _transcribe_from_blob(blob_name)
        else:
            meeting["progress_message"] = f"Extracting text from {ext} file..."
            _save_meeting(meeting)
            transcript = _extract_text_from_blob(blob_name)

        if not transcript.strip():
            raise ValueError(f"No text could be extracted from '{meeting['filename']}'.")

        # Clean up temp blob — transcript successfully captured.
        # After this point, retrying a failed pipeline requires re-upload
        # (accepted limitation for pilot — the file is no longer available).
        try:
            _get_temp_container().delete_blob(blob_name)
            print(f"   Temp blob deleted after successful extraction")
        except Exception:
            pass

        meeting["transcript"] = transcript

        # ── Step 2: AI analysis ──────────────────────────────────
        meeting["status"]           = "analyzing"
        meeting["progress_message"] = "Running AI analysis (GPT-4o-mini)..."
        _save_meeting(meeting)

        analysis, prompt_ver, t_in, t_out, cost = _run_ai_analysis(
            meeting["title"], meeting["system_name"], transcript,
        )

        meeting["summary"]           = analysis.get("summary", "")
        meeting["key_topics"]        = analysis.get("key_topics", [])
        meeting["decisions"]         = analysis.get("decisions", [])
        meeting["action_items"]      = analysis.get("action_items", [])
        meeting["open_questions"]    = analysis.get("open_questions", [])
        meeting["participants"]      = analysis.get("participants", [])
        meeting["ba_insights"]       = analysis.get("ba_insights", "")
        meeting["prompt_version"]    = prompt_ver
        meeting["input_tokens"]      = t_in
        meeting["output_tokens"]     = t_out
        meeting["estimated_cost_usd"] = cost

        # ── Step 3: Complete ─────────────────────────────────────
        meeting["status"]           = "completed"
        meeting["progress_message"] = "Processing complete."
        _save_meeting(meeting)

        print(f"Pipeline complete: {meeting_id} "
              f"({len(analysis.get('decisions',[]))} decisions, "
              f"{len(analysis.get('action_items',[]))} actions)")

    except Exception as e:
        meeting["status"]           = "failed"
        meeting["progress_message"] = f"Pipeline failed: {str(e)[:300]}"
        meeting["error"]            = str(e)[:1000]
        _save_meeting(meeting)
        print(f"Pipeline FAILED for {meeting_id}: {e}")

    finally:
        # Safety-net temp blob cleanup. If the try block already deleted it,
        # this is a harmless no-op. If the try block didn't reach that point
        # (e.g. transcription itself failed), this prevents orphaned blobs.
        # Note: retry-after-transcription-failure requires re-upload because
        # the blob is deleted here. Accepted limitation for pilot.
        try:
            _get_temp_container().delete_blob(blob_name)
        except Exception:
            pass  # already deleted or never uploaded


def get_meeting_status(meeting_id: str, org_id: str = None) -> dict:
    """Return the current pipeline status for a meeting.

    Used by the browser's 5-second poll loop. The result field is non-null
    only when status == "completed".
    """
    org_id  = _resolve_org_id(org_id)
    meeting = load_meeting(meeting_id, org_id=org_id)
    status  = meeting.get("status", "pending")
    ext     = meeting.get("file_type", "").lower()

    # Step numbers for the frontend pipeline visualisation
    step_map = {
        "pending":      1,
        "transcribing": 3,
        "extracting":   3,
        "analyzing":    4,
        "completed":    6,
        "failed":       0,
    }

    result = None
    if status == "completed":
        # Return the meeting minus the (potentially large) transcript
        result = {k: v for k, v in meeting.items() if k != "transcript"}

    return {
        "status":           status,
        "current_step":     step_map.get(status, 0),
        "total_steps":      6,
        "progress_message": meeting.get("progress_message", ""),
        "error":            meeting.get("error"),
        "result":           result,
    }
 
 
# ── KB Storage Function ─────────────────────────────────────────
 
def store_meeting_to_kb(
    meeting_id:  str,
    system_name: str,
    source_type: str,
    org_id:      str = None
) -> dict:
    """
    Human-approved step: Index meeting transcript into the Knowledge Base.

    What it does:
      1. Loads meeting record from Azure Blob (within org scope)
      2. Writes transcript + summary to a temp .txt file
      3. Calls load_and_index_document under the meeting's org_id
      4. Registers the document in the org's registry file
      5. Updates meeting record with kb_stored=True

    A meeting created under org X is stored in org X's Search index and
    registry — no cross-org leakage. If org_id is omitted here but the
    meeting dict carries one, we use the meeting's stamped org_id.
    """
    org_id  = _resolve_org_id(org_id)
    meeting = load_meeting(meeting_id, org_id=org_id)
    # Prefer the org_id already stamped on the meeting — guards against a
    # caller who looked up the meeting under the default fallback path.
    org_id  = _resolve_org_id(meeting.get("org_id") or org_id)
 
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
            source_type=source_type,
            org_id=org_id
        )

        # Register in the org's document registry
        kb_filename = f"meeting_{meeting_id}_{meeting['title'][:40].replace(' ', '_')}.txt"
        kb_filename = re.sub(r"[^\w\-_.]", "", kb_filename)  # sanitize

        record = register_document(
            document_name=kb_filename,
            system_name=system_name,
            source_type=source_type,
            chunks=chunks,
            file_size_kb=round(len(kb_content.encode("utf-8")) / 1024, 1),
            org_id=org_id
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


# ── Smoke test (storage layer only — no GPT / STT calls) ──────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Meeting Module storage layer (org_id round-trip)")
    print("=" * 55)

    test_org = "default"
    fake_id  = uuid.uuid4().hex[:8]

    fake_meeting = {
        "meeting_id":   fake_id,
        "org_id":       test_org,
        "title":        "Smoketest Meeting — Sprint 2 A8.3",
        "system_name":  "HR System",
        "filename":     "smoketest.txt",
        "file_type":    ".txt",
        "file_size_kb": 1.2,
        "created_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "transcript":   "Speaker 1: Hello. Speaker 2: Hi back.",
        "summary":      "Two speakers exchanged greetings.",
        "key_topics":   ["greetings"],
        "decisions":    [],
        "action_items": [],
        "open_questions": [],
        "participants": ["Speaker 1", "Speaker 2"],
        "ba_insights":  "N/A — smoke test fixture.",
        "kb_stored":    False,
        "kb_system_name": None,
        "kb_source_type": None,
        "kb_document_id": None,
    }

    print(f"\n── Saving meeting '{fake_id}' under org '{test_org}'")
    _save_meeting(fake_meeting)

    print("── Loading back via load_meeting (should hit org-prefixed path)")
    loaded = load_meeting(fake_id, org_id=test_org)
    assert loaded["meeting_id"] == fake_id
    assert loaded["org_id"]     == test_org
    print(f"   OK — loaded '{loaded['title']}'  org_id={loaded['org_id']}")

    print("── list_meetings scoped to 'default' (must include new meeting)")
    listed = list_meetings(org_id=test_org)
    assert any(m["meeting_id"] == fake_id for m in listed)
    print(f"   OK — {len(listed)} meetings total in org (org-prefixed + legacy)")

    print("── Cleaning up smoke test blob")
    try:
        _get_meetings_container().delete_blob(f"{test_org}/{fake_id}.json")
        print("   OK — test blob deleted")
    except Exception as e:
        print(f"   cleanup failed (non-fatal): {e}")

    print("\nMeeting module storage layer smoke test complete.")