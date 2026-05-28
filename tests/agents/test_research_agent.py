"""ResearchWorkerAgent integration test.

Drives the agent by overriding `_execute_sdk` to invoke a few save_* tools
directly, then asserts that `parse_output` reports the right artifact list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maf_coder.agents.base import _RawResult
from maf_coder.agents.research import ResearchRunSummary, ResearchWorkerAgent
from maf_coder.blackboard import ArtifactStore
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
)


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  research_worker:\n"
        "    primary:\n"
        "      model: openai/x\n"
        "      temperature: 0.0\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-research")


@pytest.fixture
async def sandbox(tmp_path: Path) -> AsyncIterator[LocalShellSandbox]:
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    await sb.exec("git init -q -b main", cwd="/workspace")
    try:
        yield sb
    finally:
        await sb.stop()


@pytest.fixture
def prompt(tmp_path: Path) -> Path:
    p = tmp_path / "research_prompt.md"
    p.write_text("You are the Research Worker. Save notes.")
    return p


def _task() -> Task:
    return Task(
        task_id="r1",
        parent_milestone="m1",
        owner=Role.RESEARCH_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="map axum routing patterns",
        background="we are adding /health",
        acceptance_criteria=["f1.a1"],
        required_outputs=["research_notes/axum-routing.md"],
        permission=Permission(
            allowed_paths=["**"],
            allowed_tools=[],
            network_policy=NetworkPolicy.CRATES_ONLY,
        ),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
    )


def _stub_fetch(url: str, timeout_sec: int) -> tuple[str, str, int, str]:
    return (url, "text/html", 200, "<p>axum routing docs</p>")


class _ScriptedResearch(ResearchWorkerAgent):
    def __init__(self, *, prompt_path: Path, **kw):  # type: ignore[no-untyped-def]
        self.prompt_path = prompt_path
        super().__init__(fetcher=_stub_fetch, **kw)

    async def _execute_sdk(self, **kw):  # type: ignore[override]
        ctx = kw["ctx"]
        tools_by_name = {t.__name__: t for t in self.build_tools(ctx)}
        await tools_by_name["fetch_url"](url="https://docs.rs/axum")
        await tools_by_name["save_research_note"](
            topic="axum-routing",
            content_markdown="# Axum routing\n\n> Research Worker synthesis: handlers are async fns.\n",
        )
        await tools_by_name["save_workspace_overview"](
            content_markdown="# Workspace\n\n- crate-a\n"
        )
        return _RawResult(
            final_output="Saved 1 note + workspace overview.",
            tokens_in=30,
            tokens_out=20,
            cost_usd=0.0,
            model_used=kw["model_id"],
        )


class TestResearchWorkerAgent:
    def test_role(self, prompt: Path, store, router, sandbox) -> None:
        agent = _ScriptedResearch(
            prompt_path=prompt,
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        assert agent.role == Role.RESEARCH_WORKER

    @pytest.mark.asyncio
    async def test_end_to_end(self, prompt: Path, store, router, sandbox) -> None:
        agent = _ScriptedResearch(
            prompt_path=prompt,
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        result = await agent.run(_task(), mission_id="m-research")
        assert result.errored is False
        assert isinstance(result.parsed_output, ResearchRunSummary)
        saved = result.parsed_output.saved_notes
        assert "research_notes/axum-routing.md" in saved
        assert "workspace_overview.md" in saved
        assert "fetch_url" in result.tools_invoked
        assert "save_research_note" in result.tools_invoked
