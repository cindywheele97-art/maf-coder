"""Retrieval injection for agents (Phase F — F-memory).

A single cold-start-safe helper that turns a query into a rendered block of
prior-mission lessons, ready to append to an agent's first user message. Lives
in the memory package (not in any agent module) so the Orchestrator, Research,
and Coder workers share one implementation rather than each carrying a copy.

Decoupled from the agent layer: it takes the `ArtifactStore` + a query string,
so importing it never creates an agent ⇄ memory cycle.
"""

from __future__ import annotations

import logging

from ..blackboard import ArtifactStore
from .paths import open_project_memory
from .retrieval import render_results, retrieve

logger = logging.getLogger(__name__)


def retrieve_memory_block(
    store: ArtifactStore,
    query: str,
    *,
    top_k: int = 5,
    kind: str | None = None,
) -> str:
    """Retrieve prior-mission lessons for ``query`` and render them.

    Returns "" on any failure or empty result so retrieval can NEVER break an
    agent's first user message (no db yet, locked db, malformed rows, …). The
    rendered block carries the anti-poisoning non-binding framing from
    ``render_results``.

    ``kind`` optionally narrows to one MemoryKind (e.g. "handoff" for the Coder).
    """
    try:
        memory = open_project_memory(store)
        try:
            results = retrieve(query, memory, kind=kind, top_k=top_k)
        finally:
            memory.close()
        return render_results(results)
    except Exception:  # pragma: no cover - defensive cold-start guard
        logger.warning("memory retrieval injection failed; continuing without lessons")
        return ""


__all__ = ["retrieve_memory_block"]
