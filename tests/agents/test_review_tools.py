"""ReviewValidator tool tests (AGENT_TOOLS_SPEC §8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import ArtifactError, PermissionDeniedError
from maf_coder.agents.tools import review_tools
from maf_coder.agents.tools.review_tools import (
    build_review_tools,
    make_apply_patch_in_fresh_worktree,
    make_save_review_notes,
    make_save_review_verdict,
    make_spawn_adversarial_subagent,
)
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
        "  coder_worker:\n"
        "    primary:\n"
        "      model: anthropic/x\n"
        "      temperature: 0.1\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
        "  review_validator:\n"
        "    primary:\n"
        "      model: openai/x\n"
        "      temperature: 0.0\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
        "  adversarial_subagent:\n"
        "    primary:\n"
        "      model: google/x\n"
        "      temperature: 0.0\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-review")


@pytest.fixture
async def sandbox(tmp_path: Path):
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    # Initialize a real git repo in /workspace. Split commands so any failure
    # surfaces immediately rather than being silently chained past.
    r = await sb.exec("git init -q", cwd="/workspace")
    assert r.exit_code == 0, f"git init failed: {r.stderr}"
    await sb.exec(
        "git config user.email t@t && git config user.name t "
        "&& git symbolic-ref HEAD refs/heads/main",
        cwd="/workspace",
    )
    r2 = await sb.exec(
        "echo init > initial.txt && git add -A && git commit -q -m initial",
        cwd="/workspace",
    )
    assert r2.exit_code == 0, f"initial commit failed: {r2.stderr}"
    try:
        yield sb
    finally:
        await sb.stop()


def _review_ctx(sandbox, store, router) -> TaskContext:
    perm = Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE)
    task = Task(
        task_id="rv-t1",
        parent_milestone="m1",
        owner=Role.REVIEW_VALIDATOR,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="review",
        background="review",
        acceptance_criteria=["f1.a1"],
        required_outputs=["verdicts/rv-t1.review.json"],
        permission=perm,
        budget=TaskBudget(max_tokens=2000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-review",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
        coder_provider_in_use="anthropic",
    )


def _coder_ctx(sandbox, store, router) -> TaskContext:
    """Same as review ctx but owner=CoderWorker — used for permission tests."""
    perm = Permission(allowed_paths=["**"], network_policy=NetworkPolicy.NONE)
    task = Task(
        task_id="cw-t1",
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="x",
        background="x",
        acceptance_criteria=["f1.a1"],
        required_outputs=["x"],
        permission=perm,
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m-review",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


class TestApplyPatchInFreshWorktree:
    @pytest.mark.asyncio
    async def test_clean_patch_applies(self, sandbox, store, router) -> None:
        # Generate a small patch from a real change.
        await sandbox.write_file("hello.txt", "before\n")
        await sandbox.exec("git add -A && git commit -q -m base", cwd="/workspace")
        await sandbox.write_file("hello.txt", "after\n")
        diff = await sandbox.exec("git diff HEAD", cwd="/workspace")
        store.write_text("patches/cw-t1.diff", diff.stdout)
        # Reset worktree so the patch can be applied cleanly to HEAD.
        await sandbox.exec("git checkout -- hello.txt", cwd="/workspace")

        ctx = _review_ctx(sandbox, store, router)
        result = await make_apply_patch_in_fresh_worktree(ctx)(
            patch_path="patches/cw-t1.diff", base_ref="HEAD"
        )
        assert result["applied"] is True
        assert "hello.txt" in result["files_changed"]

    @pytest.mark.asyncio
    async def test_missing_patch_raises_artifact_error(self, sandbox, store, router) -> None:
        ctx = _review_ctx(sandbox, store, router)
        with pytest.raises(ArtifactError):
            await make_apply_patch_in_fresh_worktree(ctx)(patch_path="patches/nope.diff")


class TestSpawnAdversarialSubagent:
    @pytest.mark.asyncio
    async def test_only_review_validator_may_call(self, sandbox, store, router) -> None:
        coder_ctx = _coder_ctx(sandbox, store, router)
        with pytest.raises(PermissionDeniedError):
            await make_spawn_adversarial_subagent(coder_ctx)(
                purpose="intent_test_detection",
                inputs={"x": 1},
            )

    @pytest.mark.asyncio
    async def test_uses_diff_provider_and_returns_parsed_findings(
        self, sandbox, store, router, monkeypatch
    ) -> None:
        captured: dict[str, str] = {}

        async def fake_sdk(*, prompt, message, model_id):
            captured["prompt"] = prompt
            captured["model_id"] = model_id
            return '```json\n{"findings": ["f1", "f2"], "adversarial_tests_generated": ["t1"]}\n```'

        monkeypatch.setattr(review_tools, "_run_subagent_sdk", fake_sdk)

        ctx = _review_ctx(sandbox, store, router)
        result = await make_spawn_adversarial_subagent(ctx)(
            purpose="completeness_second_pass",
            inputs={"patch": "..."},
        )
        # adversarial_subagent's primary model in fixture is google/x; coder used
        # anthropic — google must be the chosen provider here.
        assert "google/" in captured["model_id"]
        assert result["findings"] == ["f1", "f2"]
        assert result["adversarial_tests_generated"] == ["t1"]

    @pytest.mark.asyncio
    async def test_sdk_failure_returns_structured_error_not_raise(
        self, sandbox, store, router, monkeypatch
    ) -> None:
        async def fake_sdk(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(review_tools, "_run_subagent_sdk", fake_sdk)

        ctx = _review_ctx(sandbox, store, router)
        result = await make_spawn_adversarial_subagent(ctx)(
            purpose="intent_test_detection", inputs={}
        )
        assert any("execution failed" in f for f in result["findings"])

    @pytest.mark.asyncio
    async def test_records_second_pass_event(self, sandbox, store, router, monkeypatch) -> None:
        async def fake_sdk(**kw):
            return '{"findings": [], "adversarial_tests_generated": []}'

        monkeypatch.setattr(review_tools, "_run_subagent_sdk", fake_sdk)
        ctx = _review_ctx(sandbox, store, router)
        await make_spawn_adversarial_subagent(ctx)(purpose="intent_test_detection", inputs={})
        kinds = [e.kind for e in ctx.event_log.iter_events()]
        assert "second_pass_triggered" in kinds


class TestSaveReviewVerdict:
    @pytest.mark.asyncio
    async def test_round_trip(self, sandbox, store, router) -> None:
        ctx = _review_ctx(sandbox, store, router)
        path = await make_save_review_verdict(ctx)(
            task_id="rv-t1",
            result="pass",
            precise_reason="all gates green",
            next_action_recommendation="send_to_behavior_validator",
            cargo_gate_results={"build": True, "test": True, "clippy": True, "fmt": True},
        )
        assert path.endswith("verdicts/rv-t1.review.json")
        v = store.load_review_verdict("rv-t1")
        assert v.result == "pass"
        assert v.cargo_gate_results.build is True

    @pytest.mark.asyncio
    async def test_invalid_result_rejected(self, sandbox, store, router) -> None:
        ctx = _review_ctx(sandbox, store, router)
        with pytest.raises(ArtifactError):
            await make_save_review_verdict(ctx)(
                task_id="rv-t1",
                result="not-a-valid-result",
                precise_reason="x",
                next_action_recommendation="y",
                cargo_gate_results={"build": True, "test": True, "clippy": True, "fmt": True},
            )

    @pytest.mark.asyncio
    async def test_emits_validator_verdict_event(self, sandbox, store, router) -> None:
        ctx = _review_ctx(sandbox, store, router)
        await make_save_review_verdict(ctx)(
            task_id="rv-t1",
            result="fail",
            precise_reason="clippy at src/foo.rs:42",
            next_action_recommendation="fix",
            cargo_gate_results={"build": True, "test": True, "clippy": False, "fmt": True},
        )
        kinds = [e.kind for e in ctx.event_log.iter_events()]
        assert "validator_verdict" in kinds


class TestSaveReviewNotes:
    @pytest.mark.asyncio
    async def test_basic(self, sandbox, store, router) -> None:
        ctx = _review_ctx(sandbox, store, router)
        path = await make_save_review_notes(ctx)(task_id="rv-t1", notes_markdown="# notes\n")
        assert path == "review_notes/rv-t1.md"
        assert "notes" in store.read_text(path)


class TestFactoryList:
    def test_build_review_tools_returns_all(self, sandbox, store, router) -> None:
        ctx = _review_ctx(sandbox, store, router)
        tools = build_review_tools(ctx)
        names = {t.__name__ for t in tools}
        for n in (
            "read_file",
            "git_status",
            "git_diff",
            "git_show",
            "git_log",
            "cargo_check",
            "cargo_build",
            "cargo_test",
            "cargo_clippy",
            "cargo_fmt",
            "cargo_nextest",
            "cargo_test_doc",
            "apply_patch_in_fresh_worktree",
            "spawn_adversarial_subagent",
            "save_review_verdict",
            "save_review_notes",
        ):
            assert n in names, f"missing review tool: {n}"
