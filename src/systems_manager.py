# systems_manager.py
# Manages the list of Systems and Source Types dynamically.
# Users add systems and sources from the browser UI.
# Everything is stored in systems.json on disk.
#
# Structure of systems.json:
# {
#   "HR System": ["SharePoint", "Azure DevOps", "Database"],
#   "Finance System": ["SharePoint", "Database"],
#   "IT System": ["Azure DevOps", "Meeting Recordings"]
# }
#
# PHASE 2 SPRINT 4 (A8.7):
#   Every public function accepts org_id as a trailing kwarg. Today this is
#   a PASS-THROUGH — the on-disk file stays global — so endpoint-level
#   signatures conform to the multi-tenancy contract without forcing a
#   disruptive storage migration. Moving to systems/{org_id}/systems.json
#   (or blob-backed per-org storage) is a separate Phase 2 follow-up and
#   is tracked against CLAUDE.md A4. Until then:
#     * Systems catalog is shared across all orgs in the pilot.
#     * Azure Search docs are already org-filtered (Sprint 1), so the
#       user-visible search results are still correctly tenant-isolated.
#   Remove this comment and swap load_systems/save_systems to per-org
#   files when the migration ships.

import json
import os

SYSTEMS_FILE = "systems.json"


def load_systems(org_id=None):
    """Load all systems and their source types from disk.

    org_id is accepted for future per-org partitioning but is currently
    ignored — see module header.
    """
    _ = org_id   # TODO(phase2): route to systems/{org_id}/systems.json
    if not os.path.exists(SYSTEMS_FILE):
        # First time — create with sensible defaults
        default = {
            "HR System":      ["SharePoint", "Azure DevOps", "Database"],
            "Finance System": ["SharePoint", "Database"],
            "IT System":      ["Azure DevOps", "Meeting Recordings"],
        }
        save_systems(default)
        return default

    with open(SYSTEMS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_systems(systems, org_id=None):
    """Save systems to disk."""
    _ = org_id   # TODO(phase2): route to systems/{org_id}/systems.json
    with open(SYSTEMS_FILE, "w", encoding="utf-8") as f:
        json.dump(systems, f, indent=2)


def add_system(system_name, org_id=None):
    """Add a new system. Returns error if already exists."""
    systems = load_systems(org_id=org_id)

    if system_name in systems:
        return {"success": False, "message": f"System '{system_name}' already exists"}

    systems[system_name] = []
    save_systems(systems, org_id=org_id)
    return {"success": True, "message": f"System '{system_name}' added"}


def add_source(system_name, source_type, org_id=None):
    """Add a source type to an existing system."""
    systems = load_systems(org_id=org_id)

    if system_name not in systems:
        return {"success": False, "message": f"System '{system_name}' not found"}

    if source_type in systems[system_name]:
        return {"success": False, "message": f"Source '{source_type}' already exists in '{system_name}'"}

    systems[system_name].append(source_type)
    save_systems(systems, org_id=org_id)
    return {"success": True, "message": f"Source '{source_type}' added to '{system_name}'"}


def remove_system(system_name, org_id=None):
    """Remove a system and all its sources."""
    systems = load_systems(org_id=org_id)

    if system_name not in systems:
        return {"success": False, "message": f"System '{system_name}' not found"}

    del systems[system_name]
    save_systems(systems, org_id=org_id)
    return {"success": True, "message": f"System '{system_name}' removed"}


def remove_source(system_name, source_type, org_id=None):
    """Remove a source type from a system."""
    systems = load_systems(org_id=org_id)

    if system_name not in systems:
        return {"success": False, "message": f"System '{system_name}' not found"}

    if source_type not in systems[system_name]:
        return {"success": False, "message": f"Source '{source_type}' not found"}

    systems[system_name].remove(source_type)
    save_systems(systems, org_id=org_id)
    return {"success": True, "message": f"Source '{source_type}' removed from '{system_name}'"}


def get_all_systems(org_id=None):
    """Return the full systems dictionary."""
    return load_systems(org_id=org_id)


# ── TEST ──────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing systems manager...\n")

    # Add a new system
    print(add_system("Operations System"))

    # Add sources to it
    print(add_source("Operations System", "SharePoint"))
    print(add_source("Operations System", "Email"))

    # Try adding duplicate
    print(add_source("Operations System", "SharePoint"))

    # Show everything
    systems = get_all_systems()
    print("\nAll systems:")
    for system, sources in systems.items():
        print(f"  📁 {system}")
        for source in sources:
            print(f"       └── {source}")

    print("\n✅ Systems manager working!")