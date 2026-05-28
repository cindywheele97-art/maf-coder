"""Security Worker tool factory tests (AGENT_TOOLS_SPEC §10).

Sandbox `exec` is stubbed per-test so we can drive each tool's parsing
logic without depending on cargo-audit / cargo-deny / gitleaks /
trufflehog actually being installed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import ToolError
from maf_coder.agents.results import CommandResult
from maf_coder.agents.tools.security_tools import (
    build_security_tools,
    make_cargo_audit,
    make_cargo_deny_check,
    make_cargo_geiger,
    make_gitleaks_detect,
    make_save_security_notes,
    make_save_security_verdict,
    make_trufflehog_scan,
)
from maf_coder.blackboard import ArtifactStore
from maf_coder.blackboard.event_log import EventKind
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


def _ctx(
    sandbox: LocalShellSandbox,
    store: ArtifactStore,
    router: ModelRouter,
    *,
    permission: Permission | None = None,
    task_id: str = "sec-1",
) -> TaskContext:
    perm = permission or Permission(
        allowed_paths=["**"],
        allowed_tools=[],
        network_policy=NetworkPolicy.NONE,
    )
    task = Task(
        task_id=task_id,
        parent_milestone="m1",
        owner=Role.SECURITY_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="audit dependencies",
        background="b",
        acceptance_criteria=["f1.a1"],
        required_outputs=[f"verdicts/{task_id}.security.json"],
        permission=perm,
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-sec",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# Cargo-based scanners
# ---------------------------------------------------------------------------


class TestCargoAudit:
    @pytest.mark.asyncio
    async def test_parses_findings(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        payload = json.dumps({"vulnerabilities": [{"id": "RUSTSEC-2023-0001"}]})

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout=payload, stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_cargo_audit(ctx)()
        assert result["installed"] is True
        assert result["findings"] == [{"id": "RUSTSEC-2023-0001"}]

    @pytest.mark.asyncio
    async def test_tool_missing_degrades(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd,
                exit_code=127,
                stdout="",
                stderr="cargo: audit: command not found",
                duration_sec=0.01,
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_cargo_audit(ctx)()
        assert result["installed"] is False
        assert "not installed" in result["note"]

    @pytest.mark.asyncio
    async def test_non_zero_with_output(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """cargo-audit returns non-zero when findings exist — still parse."""
        payload = json.dumps([{"id": "v1"}, {"id": "v2"}])

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=1, stdout=payload, stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_cargo_audit(ctx)()
        assert result["installed"] is True
        assert len(result["findings"]) == 2


class TestCargoDeny:
    @pytest.mark.asyncio
    async def test_runs(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout="[]", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_cargo_deny_check(ctx)()
        assert result["installed"] is True
        assert result["findings"] == []


class TestCargoGeiger:
    @pytest.mark.asyncio
    async def test_runs(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd,
                exit_code=0,
                stdout=json.dumps({"results": [{"crate": "foo", "unsafe": 0}]}),
                stderr="",
                duration_sec=0.01,
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_cargo_geiger(ctx)()
        assert result["installed"] is True
        assert isinstance(result["findings"], list)


# ---------------------------------------------------------------------------
# Secret scanners
# ---------------------------------------------------------------------------


class TestSecretScanners:
    @pytest.mark.asyncio
    async def test_gitleaks_parses(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout="[]", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_gitleaks_detect(ctx)()
        assert result["installed"] is True

    @pytest.mark.asyncio
    async def test_trufflehog_missing(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd,
                exit_code=127,
                stdout="",
                stderr="bash: trufflehog: command not found",
                duration_sec=0.01,
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_trufflehog_scan(ctx)()
        assert result["installed"] is False


# ---------------------------------------------------------------------------
# save_security_verdict
# ---------------------------------------------------------------------------


class TestSaveSecurityVerdict:
    @pytest.mark.asyncio
    async def test_round_trip_with_findings(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_security_verdict(ctx)(
            task_id="sec-1",
            findings=[
                {
                    "severity": "high",
                    "category": "audit",
                    "description": "yanked crate foo 0.1.0",
                    "location": "Cargo.toml",
                    "suggestion": "bump to 0.2.0",
                },
            ],
        )
        assert "sec-1.security.json" in path
        loaded = store.load_security_verdict("sec-1")
        assert len(loaded.findings) == 1
        assert loaded.findings[0].description == "yanked crate foo 0.1.0"
        # security_finding events were emitted
        kinds = {e.kind for e in ctx.event_log.iter_events()}
        assert EventKind.SECURITY_FINDING.value in kinds

    @pytest.mark.asyncio
    async def test_critical_blocks_pr(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_save_security_verdict(ctx)(
            task_id="sec-2",
            findings=[
                {
                    "severity": "critical",
                    "category": "secret",
                    "description": "AWS key in source",
                }
            ],
        )
        loaded = store.load_security_verdict("sec-2")
        assert loaded.blocks_pr is True
        assert loaded.critical_count == 1

    @pytest.mark.asyncio
    async def test_empty_findings_ok_blocks_pr_false(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_save_security_verdict(ctx)(task_id="sec-3", findings=[])
        loaded = store.load_security_verdict("sec-3")
        assert loaded.blocks_pr is False
        assert loaded.findings == []

    @pytest.mark.asyncio
    async def test_rejects_bad_severity(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_save_security_verdict(ctx)(
                task_id="sec-4",
                findings=[{"severity": "EXTREME", "category": "audit", "description": "x"}],
            )

    @pytest.mark.asyncio
    async def test_rejects_bad_category(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_save_security_verdict(ctx)(
                task_id="sec-5",
                findings=[{"severity": "low", "category": "elsewhere", "description": "x"}],
            )

    @pytest.mark.asyncio
    async def test_rejects_empty_description(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_save_security_verdict(ctx)(
                task_id="sec-6",
                findings=[{"severity": "low", "category": "audit", "description": ""}],
            )


class TestSaveSecurityNotes:
    @pytest.mark.asyncio
    async def test_saves(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_security_notes(ctx)(
            task_id="sec-1", content_markdown="# Findings\n\nNothing critical."
        )
        assert path == "security_notes/sec-1.md"
        assert "Nothing critical" in store.read_text(path)


class TestBuilder:
    def test_build(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        tools = build_security_tools(ctx)
        assert len(tools) == 7
        for t in tools:
            assert callable(t)
