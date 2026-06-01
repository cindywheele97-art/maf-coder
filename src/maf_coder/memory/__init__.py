"""Cross-mission memory (Phase F — F-memory).

Persist each mission's experience so later missions benefit. Three layers:

- `store`     — SQLite-backed ProjectMemory (per-repo .maf-coder/memory.db) and
                GlobalLessons (configurable path). stdlib sqlite3 only.
- `retrieval` — keyword + optional injected-embedding hybrid scoring with time
                decay; anti-poisoning render of results as non-binding context.
- `retro`     — mission_retro.md template + assembler + ingest into memory.

Public API is re-exported here so callers do `from maf_coder.memory import ...`.
"""

from __future__ import annotations

from .retrieval import (
    Embedder,
    RetrievalResult,
    rank,
    render_results,
    retrieve,
)
from .retro import assemble_retro, ingest_retro, render_retro_markdown
from .store import GlobalLessons, ProjectMemory

__all__ = [
    "Embedder",
    "GlobalLessons",
    "ProjectMemory",
    "RetrievalResult",
    "assemble_retro",
    "ingest_retro",
    "rank",
    "render_results",
    "render_retro_markdown",
    "retrieve",
]
