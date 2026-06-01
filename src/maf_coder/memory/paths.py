"""Memory store path resolution (Phase F — F-memory).

The per-repo `ProjectMemory` and the configurable `GlobalLessons` must be
locatable from a running mission WITHOUT ever defaulting to the real home dir
(soul.md §1 hermeticity invariant — tests inject temp paths).

We derive both from the mission's `ArtifactStore.missions_root`:

- ProjectMemory lives at `<missions_root>/.maf-coder/memory.db` — co-located
  with the mission tree it belongs to. `missions_root` is the per-repo
  workspace root in production and a `tmp_path` subdir in tests, so this is
  hermetic by construction.
- GlobalLessons lives at `<missions_root>/.maf-coder/global_lessons.db` by
  default, but the path is overridable via the `MAF_GLOBAL_LESSONS_DB` env var
  for the cross-repo production case. The env override is still explicit — there
  is no silent `~` fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .store import GlobalLessons, ProjectMemory

if TYPE_CHECKING:
    from ..blackboard.artifact_store import ArtifactStore

_GLOBAL_DB_ENV = "MAF_GLOBAL_LESSONS_DB"


def project_memory_root(store: ArtifactStore) -> Path:
    """Repo root used for the per-repo ProjectMemory db (its `.maf-coder/`)."""
    return Path(store.missions_root)


def global_lessons_path(store: ArtifactStore) -> Path:
    """Path to the GlobalLessons db.

    Honors the `MAF_GLOBAL_LESSONS_DB` env override (explicit, never `~` by
    default); otherwise co-locates with the mission workspace.
    """
    override = os.environ.get(_GLOBAL_DB_ENV)
    if override:
        return Path(override)
    return Path(store.missions_root) / ".maf-coder" / "global_lessons.db"


def open_project_memory(store: ArtifactStore) -> ProjectMemory:
    return ProjectMemory(project_memory_root(store))


def open_global_lessons(store: ArtifactStore) -> GlobalLessons:
    return GlobalLessons(global_lessons_path(store))


__all__ = [
    "global_lessons_path",
    "open_global_lessons",
    "open_project_memory",
    "project_memory_root",
]
