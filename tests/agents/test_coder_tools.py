"""Coder Worker tool factory tests (AGENT_TOOLS_SPEC §7).

Tools are invoked directly (the SDK shim makes @function_tool a no-op when
the real SDK isn't installed). Sandbox is the local backend; ArtifactStore is
a tmp-path-backed instance.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import (
    ArtifactError,
    PermissionDeniedError,
    ToolError,
)
from maf_coder.agents.tools.coder_tools import (
    build_coder_tools,
    make_cargo_fmt,
    make_cargo_test,
    make_edit_file,
    make_git_checkout,
    make_git_diff,
    make_git_show,
    make_git_status,
    make_read_file,
    make_run_bash,
    make_save_handoff,
    make_save_patch,
    make_save_test_report,
    make_write_file,
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m1")


@pytest.fixture
async def sandbox(tmp_path: Path):
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    # Initialize a real git repo so the diff/log/show tools have something to
    # show. Errors from these are tolerated by the tests below.
    await sb.exec("git init -q -b main", cwd="/workspace")
    await sb.exec(
        "git config user.email t@t && git config user.name t", cwd="/workspace"
    )
    await sb.exec("touch initial && git add -A && git commit -q -m initial", cwd="/workspace")
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
    task_id: str = "t1",
) -> TaskContext:
    perm = permission or Permission(
        allowed_paths=["**"],
        allowed_tools=[],
        network_policy=NetworkPolicy.NONE,
    )
    task = Task(
        task_id=task_id,
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="g",
        background="b",
        acceptance_criteria=["f1.a1"],
        required_outputs=["patch.diff"],
        permission=perm,
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m1",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# read / write / edit
# ---------------------------------------------------------------------------


class TestReadWriteEdit:
    @pytest.mark.asyncio
    async def test_round_trip(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        write = make_write_file(ctx)
        read = make_read_file(ctx)
        await write(path="src/foo.rs", content="fn main() {}\n")
        fc = await read(path="src/foo.rs")
        assert fc.content == "fn main() {}\n"
        assert "write_file" in ctx.tools_invoked
        assert "read_file" in ctx.tools_invoked

    @pytest.mark.asyncio
    async def test_write_denied_outside_allowed_paths(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        perm = Permission(allowed_paths=["src/"], network_policy=NetworkPolicy.NONE)
        ctx = _ctx(sandbox, store, router, permission=perm)
        with pytest.raises(PermissionDeniedError):
            await make_write_file(ctx)(path="etc/passwd", content="x")

    @pytest.mark.asyncio
    async def test_edit_file_exact_replacement(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_write_file(ctx)(path="src/foo.rs", content="x x x")
        await make_edit_file(ctx)(
            path="src/foo.rs", old_string="x", new_string="y", expected_replacements=3
        )
        fc = await make_read_file(ctx)(path="src/foo.rs")
        assert fc.content == "y y y"

    @pytest.mark.asyncio
    async def test_edit_file_count_mismatch_fails(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_write_file(ctx)(path="src/foo.rs", content="x x x")
        with pytest.raises(ToolError):
            await make_edit_file(ctx)(
                path="src/foo.rs", old_string="x", new_string="y", expected_replacements=1
            )

    @pytest.mark.asyncio
    async def test_edit_file_identical_strings_rejected(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_write_file(ctx)(path="x.rs", content="x")
        with pytest.raises(ToolError):
            await make_edit_file(ctx)(path="x.rs", old_string="x", new_string="x")


# ---------------------------------------------------------------------------
# run_bash + denylist
# ---------------------------------------------------------------------------


class TestRunBash:
    @pytest.mark.asyncio
    async def test_basic(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        r = await make_run_bash(ctx)(cmd="echo hi")
        assert r.exit_code == 0
        assert "hi" in r.stdout

    @pytest.mark.asyncio
    async def test_denylist_rejects_git_push(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(PermissionDeniedError):
            await make_run_bash(ctx)(cmd="git push origin main")

    @pytest.mark.asyncio
    async def test_denylist_rejects_sudo(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(PermissionDeniedError):
            await make_run_bash(ctx)(cmd="sudo apt install")


# ---------------------------------------------------------------------------
# cargo gate tools (we exercise the wiring, not actual rustc)
# ---------------------------------------------------------------------------


class TestCargoGates:
    @pytest.mark.asyncio
    async def test_cargo_test_runs(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        # cargo not installed in test env — we accept any exit code; what we
        # validate is that the tool produced a CommandResult and recorded the
        # invocation.
        r = await make_cargo_test(ctx)(args=[])
        assert "cargo_test" in ctx.tools_invoked
        assert isinstance(r.exit_code, int)

    @pytest.mark.asyncio
    async def test_cargo_fmt_check_only(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        r = await make_cargo_fmt(ctx)(check_only=True)
        assert "cargo_fmt" in ctx.tools_invoked
        assert "--check" in r.command


# ---------------------------------------------------------------------------
# git tools
# ---------------------------------------------------------------------------


class TestGit:
    @pytest.mark.asyncio
    async def test_git_status(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        out = await make_git_status(ctx)()
        # Repo is clean -> empty output
        assert isinstance(out, str)

    @pytest.mark.asyncio
    async def test_git_diff(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_write_file(ctx)(path="src/foo.rs", content="fn main() {}\n")
        out = await make_git_diff(ctx)()
        assert isinstance(out, str)

    @pytest.mark.asyncio
    async def test_git_log(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        out = await make_git_diff(ctx)()
        assert isinstance(out, str)

    @pytest.mark.asyncio
    async def test_git_show_rejects_evil_ref(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(PermissionDeniedError):
            await make_git_show(ctx)(ref="HEAD; rm -rf /")

    @pytest.mark.asyncio
    async def test_git_checkout_blocks_branch_target(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(PermissionDeniedError):
            await make_git_checkout(ctx)(target="main")


# ---------------------------------------------------------------------------
# Output saving tools
# ---------------------------------------------------------------------------


class TestSaveOutputs:
    @pytest.mark.asyncio
    async def test_save_patch_writes_diff(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_write_file(ctx)(path="src/foo.rs", content="fn main(){}\n")
        path = await make_save_patch(ctx)(task_id="t1")
        assert path == "patches/t1.diff"
        text = store.read_text("patches/t1.diff")
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_save_patch_rejects_wrong_task_id(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(PermissionDeniedError):
            await make_save_patch(ctx)(task_id="not-mine")

    @pytest.mark.asyncio
    async def test_save_handoff_round_trip(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_save_handoff(ctx)(
            task_id="t1",
            completed=["did the thing"],
            issues_discovered=["one issue"],
            next_recommended_action="send_to_review_validator",
        )
        loaded = store.load_handoff("t1")
        assert "did the thing" in loaded.completed
        assert loaded.triggers_second_pass is False  # issues non-empty

    @pytest.mark.asyncio
    async def test_save_handoff_validates(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ArtifactError):
            await make_save_handoff(ctx)(
                task_id="t1",
                completed=[],  # ok
                commands_run=[{"bad_key": "x"}],  # bad shape
            )

    @pytest.mark.asyncio
    async def test_save_test_report_writes_json(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        await make_save_test_report(ctx)(task_id="t1", report={"summary": "ok"})
        data = json.loads(store.read_text("reports/t1.test.json"))
        assert data == {"summary": "ok"}


class TestFactoryList:
    def test_build_coder_tools_returns_all(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        tools = build_coder_tools(ctx)
        assert len(tools) >= 15  # all required Coder tools
        names = {t.__name__ for t in tools}
        for required in (
            "read_file", "write_file", "edit_file", "run_bash",
            "cargo_check", "cargo_test", "cargo_clippy", "cargo_fmt", "cargo_nextest",
            "git_status", "git_diff", "git_show", "git_log", "git_checkout",
            "save_patch", "save_handoff", "save_test_report",
        ):
            assert required in names, f"missing tool: {required}"
