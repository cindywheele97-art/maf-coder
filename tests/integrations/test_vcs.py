"""VCS / PR workflow tests (Build Plan §Phase F · F5).

Sandbox ``exec`` is stubbed per-test so we assert the *composed* gh/glab
command and parse a *canned* PR URL — no live network / gh is ever touched.
The gitleaks gate reuses the existing ``make_gitleaks_detect`` tool, so we
drive its clean / dirty behavior by stubbing the same ``sandbox.exec``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.results import CommandResult
from maf_coder.blackboard import ArtifactStore
from maf_coder.integrations.vcs import (
    build_artifact_links,
    compose_pr_command,
    create_pull_request,
    render_pr_body,
    run_gitleaks_gate,
    run_vcs_create,
)
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    PullRequestResult,
    PullRequestSpec,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
    VcsProvider,
)

PR_URL = "https://github.com/acme/widget/pull/42"
MR_URL = "https://gitlab.com/acme/widget/-/merge_requests/7"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  orchestrator:\n"
        "    primary: {model: openai/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-pr")


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
) -> TaskContext:
    task = Task(
        task_id="pr-1",
        parent_milestone="pr",
        owner=Role.ORCHESTRATOR,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="open pull request",
        background="b",
        acceptance_criteria=[],
        required_outputs=[],
        permission=Permission(
            allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.NONE
        ),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-pr",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


def _spec(provider: VcsProvider = VcsProvider.GH, *, draft: bool = False) -> PullRequestSpec:
    return PullRequestSpec(
        mission_id="m-pr",
        title="Add widget",
        body="body line one\nbody line two",
        head_branch="feature/widget",
        base_branch="main",
        provider=provider,
        draft=draft,
        repo_path="/workspace",
        artifact_links=["plan.md"],
    )


def _gitleaks_clean(cmd: str) -> CommandResult:
    return CommandResult(command=cmd, exit_code=0, stdout="[]", stderr="", duration_sec=0.01)


def _gitleaks_dirty(cmd: str) -> CommandResult:
    payload = json.dumps(
        [{"Description": "AWS key", "File": "src/main.rs", "Secret": "AKIA..."}]
    )
    # gitleaks exits non-zero when leaks are found; findings still parse.
    return CommandResult(command=cmd, exit_code=1, stdout=payload, stderr="", duration_sec=0.01)


# ---------------------------------------------------------------------------
# Command composition
# ---------------------------------------------------------------------------


class TestComposeCommand:
    def test_gh_command(self) -> None:
        cmd = compose_pr_command(_spec(VcsProvider.GH))
        assert cmd.startswith("gh pr create ")
        assert "--title 'Add widget'" in cmd
        assert "--base main" in cmd
        assert "--head feature/widget" in cmd
        assert "--draft" not in cmd

    def test_gh_draft(self) -> None:
        cmd = compose_pr_command(_spec(VcsProvider.GH, draft=True))
        assert cmd.endswith(" --draft")

    def test_glab_command(self) -> None:
        cmd = compose_pr_command(_spec(VcsProvider.GLAB))
        assert cmd.startswith("glab mr create ")
        assert "--description" in cmd
        assert "--target-branch main" in cmd
        assert "--source-branch feature/widget" in cmd

    def test_body_is_quoted(self) -> None:
        # Multiline body must be a single shell-safe token (shlex-quoted).
        cmd = compose_pr_command(_spec(VcsProvider.GH))
        assert "'body line one\nbody line two'" in cmd


# ---------------------------------------------------------------------------
# gh / glab wrapper — composes command + parses URL from canned response
# ---------------------------------------------------------------------------


class TestVcsWrapper:
    @pytest.mark.asyncio
    async def test_gh_parses_url(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: dict[str, str] = {}

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            seen["cmd"] = cmd
            seen["cwd"] = cwd
            return CommandResult(
                command=cmd, exit_code=0, stdout=PR_URL + "\n", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await run_vcs_create(ctx, _spec(VcsProvider.GH))
        assert result.created is True
        assert result.url == PR_URL
        assert seen["cmd"].startswith("gh pr create ")
        assert seen["cwd"] == "/workspace"

    @pytest.mark.asyncio
    async def test_glab_parses_url(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout=MR_URL + "\n", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await run_vcs_create(ctx, _spec(VcsProvider.GLAB))
        assert result.created is True
        assert result.url == MR_URL
        assert result.provider == VcsProvider.GLAB.value

    @pytest.mark.asyncio
    async def test_cli_failure_not_created(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=1, stdout="", stderr="not authenticated", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await run_vcs_create(ctx, _spec(VcsProvider.GH))
        assert result.created is False
        assert result.url is None
        assert result.exit_code == 1
        assert result.refused is False  # CLI failure is not a gate refusal


# ---------------------------------------------------------------------------
# PR-description generation
# ---------------------------------------------------------------------------


class TestRenderBody:
    def test_sections_and_artifact_link(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "# Build a widget\n\nDetails here.")
        store.write_json("verdicts/t5.review.json", {"result": "PASS"})
        store.write_json("verdicts/t5.behavior.json", {"result": "PASS"})
        body = render_pr_body(
            mission_id="m-pr", store=store, event_log=store.event_log()
        )
        assert "## Summary" in body
        assert "Build a widget" in body
        assert "## Changes" in body
        assert "## Validation" in body
        assert "t5.review.json" in body
        assert "**PASS**" in body
        assert "## Cost" in body
        assert "## Artifacts" in body
        assert "missions/m-pr/" in body

    def test_goal_override(self, store: ArtifactStore) -> None:
        body = render_pr_body(
            mission_id="m-pr",
            store=store,
            event_log=store.event_log(),
            goal="Explicit goal",
        )
        assert "Explicit goal" in body

    def test_no_verdicts_message(self, store: ArtifactStore) -> None:
        body = render_pr_body(mission_id="m-pr", store=store, event_log=store.event_log())
        assert "No validator verdicts recorded" in body

    def test_build_artifact_links_only_existing(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "x")
        store.write_json("verdicts/t1.review.json", {"result": "PASS"})
        links = build_artifact_links(store)
        assert "." in links
        assert "plan.md" in links
        assert "verdicts/t1.review.json" in links
        assert "validation_contract.yaml" not in links  # not written


# ---------------------------------------------------------------------------
# gitleaks pre-PR gate (reuses make_gitleaks_detect)
# ---------------------------------------------------------------------------


class TestGitleaksGate:
    @pytest.mark.asyncio
    async def test_clean_returns_empty(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            assert "gitleaks detect" in cmd  # reusing the existing wrapper's command
            return _gitleaks_clean(cmd)

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        findings = await run_gitleaks_gate(ctx)
        assert findings == []

    @pytest.mark.asyncio
    async def test_dirty_returns_findings(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return _gitleaks_dirty(cmd)

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        findings = await run_gitleaks_gate(ctx)
        assert len(findings) == 1
        assert findings[0]["File"] == "src/main.rs"


# ---------------------------------------------------------------------------
# create_pull_request — gate then wrapper (clean proceeds / dirty refuses)
# ---------------------------------------------------------------------------


class TestCreatePullRequest:
    @pytest.mark.asyncio
    async def test_clean_proceeds(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            calls.append(cmd)
            if "gitleaks" in cmd:
                return _gitleaks_clean(cmd)
            return CommandResult(
                command=cmd, exit_code=0, stdout=PR_URL, stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await create_pull_request(ctx, _spec(VcsProvider.GH))
        assert isinstance(result, PullRequestResult)
        assert result.created is True
        assert result.url == PR_URL
        # gitleaks ran BEFORE gh.
        assert any("gitleaks" in c for c in calls)
        assert any(c.startswith("gh pr create") for c in calls)

    @pytest.mark.asyncio
    async def test_dirty_refuses_without_running_gh(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            calls.append(cmd)
            if "gitleaks" in cmd:
                return _gitleaks_dirty(cmd)
            return CommandResult(
                command=cmd, exit_code=0, stdout=PR_URL, stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await create_pull_request(ctx, _spec(VcsProvider.GH))
        assert result.created is False
        assert result.refused is True
        assert result.refusal_reason is not None
        assert "secret" in result.refusal_reason.lower()
        assert len(result.gitleaks_findings) == 1
        # The gh CLI must NOT have been invoked when the gate refuses.
        assert not any(c.startswith("gh pr create") for c in calls)


# ---------------------------------------------------------------------------
# Schema invariant: extra="forbid"
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_spec_forbids_extra(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            PullRequestSpec(
                mission_id="m",
                title="t",
                body="b",
                head_branch="h",
                repo_path="/r",
                bogus="x",  # type: ignore[call-arg]
            )

    def test_result_forbids_extra(self) -> None:
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            PullRequestResult(
                mission_id="m",
                provider=VcsProvider.GH,
                created=True,
                bogus="x",  # type: ignore[call-arg]
            )
