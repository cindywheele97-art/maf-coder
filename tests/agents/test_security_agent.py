"""SecurityWorkerAgent integration test.

Drives the agent by overriding `_execute_sdk` to invoke a few tools
directly, then asserts that `parse_output` reports the right artifact
paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maf_coder.agents.base import _RawResult
from maf_coder.agents.security import SecurityRunSummary, SecurityWorkerAgent
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
        "  security_worker:\n"
        "    primary:\n"
        "      model: google/x\n"
        "      temperature: 0.0\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-sec")


@pytest.fixture
async def sandbox(tmp_path: Path) -> AsyncIterator[LocalShellSandbox]:
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    try:
        yield sb
    finally:
        await sb.stop()


@pytest.fixture
def prompt(tmp_path: Path) -> Path:
    p = tmp_path / "sec_prompt.md"
    p.write_text("You are the Security Worker.")
    return p


def _task() -> Task:
    return Task(
        task_id="sec-1",
        parent_milestone="m1",
        owner=Role.SECURITY_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="audit deps",
        background="b",
        acceptance_criteria=["f1.a1"],
        required_outputs=["verdicts/sec-1.security.json"],
        permission=Permission(
            allowed_paths=["**"],
            allowed_tools=[],
            network_policy=NetworkPolicy.NONE,
        ),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=30),
    )


class _ScriptedSecurity(SecurityWorkerAgent):
    def __init__(self, *, prompt_path: Path, **kw):  # type: ignore[no-untyped-def]
        self.prompt_path = prompt_path
        super().__init__(**kw)

    async def _execute_sdk(self, **kw):  # type: ignore[override]
        ctx = kw["ctx"]
        tools_by_name = {t.__name__: t for t in self.build_tools(ctx)}
        # Save one HIGH finding + a note.
        await tools_by_name["save_security_verdict"](
            task_id=ctx.task.task_id,
            findings=[
                {
                    "severity": "high",
                    "category": "audit",
                    "description": "RUSTSEC-2099-0001 affects foo 0.1.0",
                    "location": "Cargo.lock",
                    "suggestion": "bump foo to 0.2.0",
                },
            ],
        )
        await tools_by_name["save_security_notes"](
            task_id=ctx.task.task_id,
            content_markdown="# Findings\n\n- 1 high (audit)\n",
        )
        return _RawResult(
            final_output="1 high; blocks_pr=False; no missing scanners.",
            tokens_in=20,
            tokens_out=15,
            cost_usd=0.0,
            model_used=kw["model_id"],
        )


class TestSecurityWorkerAgent:
    def test_role(self, prompt: Path, store, router, sandbox) -> None:
        agent = _ScriptedSecurity(
            prompt_path=prompt,
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        assert agent.role == Role.SECURITY_WORKER

    @pytest.mark.asyncio
    async def test_end_to_end(self, prompt: Path, store, router, sandbox) -> None:
        agent = _ScriptedSecurity(
            prompt_path=prompt,
            store=store,
            event_log=store.event_log(),
            router=router,
            sandbox=sandbox,
        )
        result = await agent.run(_task(), mission_id="m-sec")
        assert result.errored is False
        assert isinstance(result.parsed_output, SecurityRunSummary)
        assert result.parsed_output.verdict_path == "verdicts/sec-1.security.json"
        assert result.parsed_output.notes_path == "security_notes/sec-1.md"
        verdict = store.load_security_verdict("sec-1")
        assert verdict.blocks_pr is False
        assert verdict.high_count == 1
