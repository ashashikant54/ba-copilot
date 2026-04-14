# kb_manager.py
# Knowledge Box entity — Phase 2 Sprint 2 (CLAUDE.md A8 step 4).
#
# WHAT IS A KNOWLEDGE BOX?
#   A Knowledge Box (KB) is a named, org-scoped container that groups together
#   the (system, source_type) pairs an Analyst is allowed to query. Admins
#   create and manage KBs; Analysts are granted access to specific KBs (not
#   org-wide). One org has many KBs; one KB subscribes to many (system, source)
#   pairs; the same (system, source) can appear in multiple KBs.
#
#   The KB entity is the access-control seam. In Sprint 3+ the auth middleware
#   will use a user's accessible_kb_ids[] to narrow retriever queries to the
#   systems/sources subscribed by those KBs. Today that enforcement lives
#   outside this module — this module is pure data.
#
# STORAGE:
#   knowledge-boxes/{org_id}/{kb_id}.json   (Azure Blob Storage)
#
# CONVENTIONS FOLLOWED (CLAUDE.md rules 1, 3, 5, 6):
#   - sys.path.append at top so flat imports work under uvicorn or direct run
#   - load_dotenv() at module top (Sprint 1 lesson — capturing env at import
#     time must happen after dotenv loads)
#   - Every operation takes org_id; _resolve_org_id preserves Phase 1 behaviour
#     and is the single seam the Sprint 3 auth middleware will flip
#   - Reuses session_manager's container pattern but a different container
#
# DESIGN DECISIONS (Sprint 2):
#   - Hard delete (Option A) — delete_kb removes the blob outright; no cascade
#     or tombstone. Revisit when KBs gain downstream references.
#   - Stable UUID kb_id decoupled from human name — rename-safe.
#   - add_system_to_kb is idempotent on the (system, source) pair — calling
#     twice with the same pair is a no-op, not an error.
#   - update_kb whitelists which fields can be edited. Rename collision check
#     is strict case-sensitive; new_name == current_name is a silent no-op.

import json
import os
import sys
import uuid
from datetime import datetime

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from azure.storage.blob import BlobServiceClient
from azure.core.exceptions import ResourceNotFoundError

from session_manager import _resolve_org_id, DEFAULT_ORG_ID

# ── CONFIGURATION ──────────────────────────────────────────────
AZURE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
KB_CONTAINER = "knowledge-boxes"   # Azure Blob container — lowercase+hyphens only

# Fields that callers are NOT allowed to mutate via update_kb. Edits to the
# systems[] field go through add_system_to_kb / remove_system_from_kb so the
# add/remove helpers can maintain dedup invariants and stamp added_at.
_UPDATE_BLOCKED_FIELDS = frozenset({
    "kb_id", "org_id", "created_at", "systems"
})


# ── HELPER: Blob container ────────────────────────────────────
def _get_container():
    """Connect to Azure Blob and return the knowledge-boxes container."""
    if not AZURE_CONNECTION_STRING:
        raise EnvironmentError(
            "AZURE_STORAGE_CONNECTION_STRING is not set. "
            "Add it to .env (local dev) or App Service settings (prod)."
        )
    blob_service = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
    container = blob_service.get_container_client(KB_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass   # already exists
    return container


def _save_kb(kb):
    """Write a KB dict to its org-prefixed blob path, bumping updated_at."""
    kb["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    org_id    = _resolve_org_id(kb.get("org_id"))
    kb["org_id"] = org_id
    blob_name = f"{org_id}/{kb['kb_id']}.json"
    data      = json.dumps(kb, indent=2)
    container = _get_container()
    container.upload_blob(
        name=blob_name,
        data=data,
        overwrite=True,
        encoding="utf-8"
    )
    return kb


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Name uniqueness ───────────────────────────────────────────
def check_kb_name_unique(org_id, name):
    """True if no existing KB in this org has this name (case-sensitive)."""
    org_id = _resolve_org_id(org_id)
    for kb in list_kbs(org_id, admin_id=None):
        if kb["name"] == name:
            return False
    return True


# ── CRUD ──────────────────────────────────────────────────────
def create_kb(org_id, name, owner_admin_id):
    """Create a new KB. Returns the generated kb_id.

    Raises ValueError if the name is already taken within the org —
    main.py's thin-endpoint convention maps ValueError to HTTP 400.
    """
    org_id = _resolve_org_id(org_id)

    if not name or not name.strip():
        raise ValueError("KB name cannot be empty")
    if not check_kb_name_unique(org_id, name):
        raise ValueError(f"KB name '{name}' already exists in org '{org_id}'")

    kb_id = uuid.uuid4().hex[:8]
    kb = {
        "kb_id":          kb_id,
        "org_id":         org_id,
        "name":           name,
        "owner_admin_id": owner_admin_id,
        "systems":        [],
        "created_at":     _now(),
        "updated_at":     _now(),
    }
    _save_kb(kb)
    print(f"Created KB '{name}' ({kb_id}) in org '{org_id}'")
    return kb_id


def get_kb(org_id, kb_id):
    """Load a KB by id. Raises FileNotFoundError if missing."""
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    try:
        blob = container.get_blob_client(f"{org_id}/{kb_id}.json")
        data = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except ResourceNotFoundError:
        raise FileNotFoundError(
            f"KB '{kb_id}' not found in org '{org_id}' "
            f"(looked at {org_id}/{kb_id}.json)"
        )


def list_kbs(org_id, admin_id=None):
    """List KBs in an org, newest first.

    admin_id=None   → return ALL KBs in the org (SuperAdmin / App Support view)
    admin_id="..."  → return only KBs owned by that admin (Admin scope)

    Scoping is enforced at the blob-list level via name_starts_with so
    cross-org blobs are never downloaded. The admin_id filter runs in-process
    after download — per-admin indexing is not worth it at pilot scale.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    kbs       = []

    for blob_item in container.list_blobs(name_starts_with=f"{org_id}/"):
        if not blob_item.name.endswith(".json"):
            continue
        try:
            blob = container.get_blob_client(blob_item.name)
            data = blob.download_blob().readall()
            kb = json.loads(data.decode("utf-8"))
            if admin_id is not None and kb.get("owner_admin_id") != admin_id:
                continue
            kbs.append(kb)
        except Exception:
            pass   # skip malformed blobs rather than failing the whole list

    kbs.sort(key=lambda k: k.get("updated_at", ""), reverse=True)
    return kbs


def update_kb(org_id, kb_id, updates):
    """Apply whitelisted updates to a KB and return the updated dict.

    Blocked fields: kb_id, org_id, created_at, systems (use add/remove helpers).
    Rename semantics:
      - If 'name' is in updates and equals the current name → silent no-op
        on that field (other updates still apply).
      - Otherwise, enforce strict case-sensitive uniqueness in the org.
      - Collision → ValueError (maps to HTTP 400 in main.py).
    """
    org_id = _resolve_org_id(org_id)
    kb     = get_kb(org_id, kb_id)

    blocked = set(updates.keys()) & _UPDATE_BLOCKED_FIELDS
    if blocked:
        raise ValueError(
            f"Cannot update protected fields: {sorted(blocked)}. "
            f"Use add_system_to_kb / remove_system_from_kb for systems."
        )

    if "name" in updates:
        new_name = updates["name"]
        if new_name != kb["name"]:
            if not new_name or not new_name.strip():
                raise ValueError("KB name cannot be empty")
            # Strict case-sensitive collision check against every other KB in org.
            for other in list_kbs(org_id, admin_id=None):
                if other["kb_id"] == kb_id:
                    continue
                if other["name"] == new_name:
                    raise ValueError(
                        f"KB name '{new_name}' already exists in org '{org_id}'"
                    )

    kb.update(updates)
    _save_kb(kb)
    return kb


def delete_kb(org_id, kb_id):
    """Hard-delete a KB blob. Returns True on success, False if not found.

    Sprint 2 policy: no cascade. Downstream references (sessions, Search docs)
    are not affected because no such references exist yet. Revisit when
    connectors or granted-access lists start pointing at kb_id.
    """
    org_id    = _resolve_org_id(org_id)
    container = _get_container()
    try:
        container.delete_blob(f"{org_id}/{kb_id}.json")
        return True
    except ResourceNotFoundError:
        return False


# ── Systems membership ────────────────────────────────────────
def add_system_to_kb(org_id, kb_id, system_name, source_type):
    """Subscribe a KB to a (system_name, source_type) pair.

    Idempotent — if the exact pair is already on the KB, this is a no-op
    (returns the unchanged KB). Different source_types under the same
    system_name are independent entries.
    """
    org_id = _resolve_org_id(org_id)
    kb     = get_kb(org_id, kb_id)

    for entry in kb.get("systems", []):
        if entry["system_name"] == system_name and entry["source_type"] == source_type:
            return kb   # already subscribed — no write

    kb.setdefault("systems", []).append({
        "system_name": system_name,
        "source_type": source_type,
        "added_at":    _now(),
    })
    _save_kb(kb)
    return kb


def remove_system_from_kb(org_id, kb_id, system_name):
    """Unsubscribe a KB from ALL source_types under a system_name.

    The signature intentionally omits source_type — removal is coarse-grained
    by system. Admins who want to remove only one source under a system can
    remove all and re-add the ones they want to keep.
    """
    org_id = _resolve_org_id(org_id)
    kb     = get_kb(org_id, kb_id)

    before = len(kb.get("systems", []))
    kb["systems"] = [
        entry for entry in kb.get("systems", [])
        if entry.get("system_name") != system_name
    ]
    if len(kb["systems"]) != before:
        _save_kb(kb)
    return kb


# ── Smoke test ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("TEST: Knowledge Box Manager (Azure Blob Storage)")
    print("=" * 55)

    test_org   = "default"
    test_admin = "admin-smoketest"
    test_name  = f"Smoketest_KB_{uuid.uuid4().hex[:4]}"

    print(f"\n── Creating KB '{test_name}' for admin '{test_admin}'")
    kb_id = create_kb(test_org, test_name, test_admin)

    print("\n── Attempting duplicate create (should raise ValueError)")
    try:
        create_kb(test_org, test_name, test_admin)
        print("  FAIL — duplicate was accepted!")
    except ValueError as e:
        print(f"  OK — rejected: {e}")

    print("\n── Adding systems")
    add_system_to_kb(test_org, kb_id, "Finance System", "SharePoint")
    add_system_to_kb(test_org, kb_id, "Finance System", "Database")
    add_system_to_kb(test_org, kb_id, "HR System",      "SharePoint")
    add_system_to_kb(test_org, kb_id, "Finance System", "SharePoint")   # dedup — no-op
    kb = get_kb(test_org, kb_id)
    print(f"  KB now has {len(kb['systems'])} system entries "
          f"(expect 3 — the 4th was a dedup no-op)")
    for s in kb["systems"]:
        print(f"    - {s['system_name']} / {s['source_type']}")

    print("\n── Removing Finance System (both source_types)")
    remove_system_from_kb(test_org, kb_id, "Finance System")
    kb = get_kb(test_org, kb_id)
    print(f"  KB now has {len(kb['systems'])} system entries (expect 1 — HR System only)")

    print("\n── Rename via update_kb (valid)")
    new_name = test_name + "_renamed"
    update_kb(test_org, kb_id, {"name": new_name})
    print(f"  Renamed to '{get_kb(test_org, kb_id)['name']}'")

    print("\n── Attempting to update blocked field 'systems' (should raise)")
    try:
        update_kb(test_org, kb_id, {"systems": []})
        print("  FAIL — blocked write was accepted!")
    except ValueError as e:
        print(f"  OK — rejected: {e}")

    print("\n── list_kbs (full org view)")
    full = list_kbs(test_org, admin_id=None)
    print(f"  {len(full)} KBs total in org")

    print(f"\n── list_kbs scoped to admin '{test_admin}'")
    scoped = list_kbs(test_org, admin_id=test_admin)
    print(f"  {len(scoped)} KBs owned by this admin")
    for kb in scoped:
        print(f"    [{kb['kb_id']}] {kb['name']}  "
              f"(systems={len(kb['systems'])}, updated={kb['updated_at']})")

    print("\n── Deleting test KB")
    ok = delete_kb(test_org, kb_id)
    print(f"  delete_kb returned: {ok}")

    print("\n── Verifying deletion")
    try:
        get_kb(test_org, kb_id)
        print("  FAIL — KB still readable!")
    except FileNotFoundError:
        print("  OK — KB is gone")

    print("\nKnowledge Box Manager smoke test complete.")
