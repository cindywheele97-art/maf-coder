"""ResearchWorkerAgent (AGENT_TOOLS_SPEC §9 + §17 step 11).

A BaseAgent subclass wiring the Research Worker tool surface. Like the
Coder agent, the Research Worker's structured outputs are saved
in-flight via the `save_*` tools — `parse_output` only summarizes the
agent's closing narration and lists which artifacts now exist on disk.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory import retrieve_memory_block
from ..schemas import Role
from .base import BaseAgent, TaskContext
from .tools.research_tools import build_research_tools

_FetchFn = Callable[[str, int], tuple[str, str, int, str]]
_Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class ResearchRunSummary:
    """Parsed output from one Research Worker run.

    `saved_notes` enumerates the artifacts the Research Worker produced
    during this run (paths relative to `missions/<id>/`). Empty when the
    worker errored out before saving anything.
    """

    final_message: str
    saved_notes: list[str] = field(default_factory=list)
    tools_invoked: list[str] = field(default_factory=list)


class ResearchWorkerAgent(BaseAgent[ResearchRunSummary]):
    """Implements §9 — read-only tools + sanitized network fetch."""

    role = Role.RESEARCH_WORKER
    prompt_path = Path("prompts/research_worker.md")

    def __init__(
        self,
        *,
        store: Any,
        event_log: Any,
        router: Any,
        sandbox: Any,
        fetcher: _FetchFn | None = None,
        domain_whitelist: list[str] | None = None,
        resolver: _Resolver | None = None,
    ) -> None:
        super().__init__(store=store, event_log=event_log, router=router, sandbox=sandbox)
        self._fetcher = fetcher
        self._domain_whitelist = domain_whitelist
        self._resolver = resolver

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return build_research_tools(
            ctx,
            fetcher=self._fetcher,
            domain_whitelist=self._domain_whitelist,
            resolver=self._resolver,
        )

    def build_first_user_message(self, ctx: TaskContext) -> str:
        task = ctx.task
        lines = [
            f"# Task: {task.task_id}",
            "",
            "## Goal",
            task.goal,
            "",
            "## Background",
            task.background,
            "",
            "## Required outputs",
        ]
        for ro in task.required_outputs:
            lines.append(f"- {ro}")
        lines += [
            "",
            "## Network policy",
            f"network_policy = {task.permission.network_policy}",
            "",
            "## Discipline",
            "1. Prefer local sources (cargo_metadata, cargo_tree, grep, glob) before fetch_url.",
            "2. Every note must have at least one citation; rewrite external snippets.",
            "3. Hard cap 200 lines per note. Split if longer.",
            "4. Final message: list artifacts saved + the single most important fact for the Coder.",
        ]
        # Phase F — F-memory: prior-mission lessons on this topic as NON-binding
        # context. Cold-start safe: no db / any error ⇒ nothing appended.
        memory_block = retrieve_memory_block(ctx.store, task.goal)
        if memory_block:
            lines += ["", memory_block]
        return "\n".join(lines)

    def parse_output(self, raw_output: str, ctx: TaskContext) -> ResearchRunSummary:
        # Best-effort: scan the canonical write-out paths and list anything present.
        candidates = [
            "dependency_brief.md",
            "workspace_overview.md",
        ]
        existing = [p for p in candidates if ctx.store.exists(p)]
        for sub in ("research_notes", "code_map"):
            existing.extend(_list_dir(ctx, sub))
        return ResearchRunSummary(
            final_message=raw_output.strip(),
            saved_notes=existing,
            tools_invoked=list(ctx.tools_invoked),
        )

    def _null_output(self) -> ResearchRunSummary:
        return ResearchRunSummary(final_message="", saved_notes=[], tools_invoked=[])


def _list_dir(ctx: TaskContext, subdir: str) -> list[str]:
    """Return relative paths of all files under missions/<id>/<subdir>, if any."""
    base = ctx.store.mission_dir / subdir
    if not base.exists() or not base.is_dir():
        return []
    rels = []
    for p in sorted(base.iterdir()):
        if p.is_file():
            rels.append(f"{subdir}/{p.name}")
    return rels


__all__ = ["ResearchRunSummary", "ResearchWorkerAgent"]
