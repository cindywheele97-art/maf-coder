"""Coder Worker tool factories (AGENT_TOOLS_SPEC §7).

Tools are returned by `make_<name>(ctx)` factories. Each tool closes over
`ctx: TaskContext` and is decorated with the SDK's `@function_tool` (or its
no-op shim when the SDK isn't installed). Every tool runs the permission
checks for its operation before invoking the sandbox or artifact store.

The full Coder surface:
    file IO       : read_file, write_file, edit_file
    run           : run_bash
    cargo         : cargo_check, cargo_test, cargo_clippy, cargo_fmt, cargo_nextest
    git           : git_status, git_diff, git_show, git_log, git_checkout
    output        : save_patch, save_handoff, save_test_report
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ...schemas import Handoff
from .._sdk import function_tool
from ..base import TaskContext
from ..errors import (
    ArtifactError,
    PermissionDeniedError,
    SandboxError,
    ToolError,
)
from ..permissions import (
    check_command_pattern,
    check_path_access,
    check_tool_allowed,
)
from ..results import CommandResult, FileContent
from . import record_tool_call, time_block

# ---------------------------------------------------------------------------
# File IO
# ---------------------------------------------------------------------------


def make_read_file(ctx: TaskContext) -> Any:
    @function_tool
    async def read_file(path: str, max_bytes: int = 1_000_000) -> FileContent:
        """Read a file from the sandbox worktree.

        Path is relative to /workspace/<repo>/. Returns the file content
        truncated to `max_bytes` if larger.
        """
        check_tool_allowed(ctx.task.permission, "read_file")
        check_path_access(ctx.task.permission, path, mode="read")
        t0 = time_block()
        fc = await ctx.sandbox.read_file(path, max_bytes=max_bytes)
        record_tool_call(ctx, "read_file", f"path={path}", duration_sec=time_block() - t0)
        return fc

    return read_file


def make_write_file(ctx: TaskContext) -> Any:
    @function_tool
    async def write_file(path: str, content: str) -> str:
        """Write a file in the sandbox worktree.

        STATE-BASED write — overwrites existing content. Use this for creating
        new files OR rewriting an entire file. For partial edits, use
        edit_file instead. Returns the relative path written.
        """
        check_tool_allowed(ctx.task.permission, "write_file")
        check_path_access(ctx.task.permission, path, mode="write")
        t0 = time_block()
        await ctx.sandbox.write_file(path, content)
        record_tool_call(
            ctx,
            "write_file",
            f"path={path} bytes={len(content)}",
            duration_sec=time_block() - t0,
        )
        return path

    return write_file


def make_edit_file(ctx: TaskContext) -> Any:
    @function_tool
    async def edit_file(
        path: str,
        old_string: str,
        new_string: str,
        expected_replacements: int = 1,
    ) -> str:
        """Replace exactly N occurrence(s) of old_string with new_string in path.

        FAILS if old_string occurs != expected_replacements times. This forces
        the agent to be specific about which occurrence it means.
        """
        check_tool_allowed(ctx.task.permission, "edit_file")
        check_path_access(ctx.task.permission, path, mode="write")
        if expected_replacements < 1:
            raise ToolError("expected_replacements must be >= 1")
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical")

        t0 = time_block()
        fc = await ctx.sandbox.read_file(path)
        count = fc.content.count(old_string)
        if count != expected_replacements:
            raise ToolError(
                f"edit_file: expected {expected_replacements} occurrences of "
                f"old_string, found {count} in {path}"
            )
        new_content = fc.content.replace(old_string, new_string)
        await ctx.sandbox.write_file(path, new_content)
        record_tool_call(
            ctx,
            "edit_file",
            f"path={path} replacements={count}",
            duration_sec=time_block() - t0,
        )
        return path

    return edit_file


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------


def make_run_bash(ctx: TaskContext) -> Any:
    @function_tool
    async def run_bash(
        cmd: str,
        timeout_sec: int = 60,
        cwd: str | None = None,
    ) -> CommandResult:
        """Run an arbitrary bash command in the sandbox.

        cwd defaults to /workspace/<repo>/. Timeout applies to the full
        command including subshells.
        """
        check_tool_allowed(ctx.task.permission, "run_bash")
        check_command_pattern(ctx.task.permission, cmd)
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd=cwd or "/workspace", timeout_sec=timeout_sec)
        record_tool_call(
            ctx,
            "run_bash",
            f"cmd={cmd[:80]}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return res

    return run_bash


# ---------------------------------------------------------------------------
# Cargo gate tools — all share a thin shape
# ---------------------------------------------------------------------------


def _cargo_factory(tool_name: str, default_cmd: str) -> Callable[[TaskContext], Callable[..., Any]]:
    """Build a make_cargo_<x> factory function for a stock cargo gate."""

    def make(ctx: TaskContext) -> Any:
        @function_tool
        async def runner(args: list[str] | None = None) -> CommandResult:
            check_tool_allowed(ctx.task.permission, tool_name)
            extras = " ".join(args or [])
            cmd = f"{default_cmd} {extras}".strip()
            check_command_pattern(ctx.task.permission, cmd)
            t0 = time_block()
            res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=600)
            record_tool_call(
                ctx,
                tool_name,
                f"cmd={cmd[:120]}",
                exit_code=res.exit_code,
                duration_sec=time_block() - t0,
            )
            return res

        runner.__name__ = tool_name
        runner.__doc__ = f"Run `{default_cmd}` with optional extra args."
        return runner

    return make


make_cargo_check = _cargo_factory("cargo_check", "cargo check --workspace --all-targets")
make_cargo_test = _cargo_factory("cargo_test", "cargo test --workspace")
make_cargo_clippy = _cargo_factory(
    "cargo_clippy",
    "cargo clippy --workspace --all-targets --all-features -- -D warnings",
)
make_cargo_nextest = _cargo_factory("cargo_nextest", "cargo nextest run --workspace --all-features")


def make_cargo_fmt(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_fmt(check_only: bool = False) -> CommandResult:
        """Run `cargo fmt`. If check_only=True, runs with --check (no mutation)."""
        check_tool_allowed(ctx.task.permission, "cargo_fmt")
        cmd = "cargo fmt --all -- --check" if check_only else "cargo fmt --all"
        check_command_pattern(ctx.task.permission, cmd)
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=120)
        record_tool_call(
            ctx,
            "cargo_fmt",
            f"check_only={check_only}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return res

    return cargo_fmt


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------


_FORBIDDEN_CHECKOUT_TARGETS = {
    "main",
    "master",
    "develop",
    "trunk",
    "release",
}


def make_git_status(ctx: TaskContext) -> Any:
    @function_tool
    async def git_status() -> str:
        """Run `git status --short` in the worktree."""
        check_tool_allowed(ctx.task.permission, "git_status")
        t0 = time_block()
        res = await ctx.sandbox.exec("git status --short", cwd="/workspace")
        record_tool_call(
            ctx, "git_status", "", exit_code=res.exit_code, duration_sec=time_block() - t0
        )
        return res.stdout

    return git_status


def make_git_diff(ctx: TaskContext) -> Any:
    @function_tool
    async def git_diff(args: list[str] | None = None) -> str:
        """Run `git diff` with optional args. Default: HEAD."""
        check_tool_allowed(ctx.task.permission, "git_diff")
        rest = " ".join(args or ["HEAD"])
        cmd = f"git diff {rest}"
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=60)
        record_tool_call(
            ctx, "git_diff", f"args={rest}", exit_code=res.exit_code, duration_sec=time_block() - t0
        )
        return res.stdout

    return git_diff


def make_git_show(ctx: TaskContext) -> Any:
    @function_tool
    async def git_show(ref: str) -> str:
        """Run `git show <ref>`."""
        check_tool_allowed(ctx.task.permission, "git_show")
        if not re.match(r"^[A-Za-z0-9._/^~:-]+$", ref):
            raise PermissionDeniedError(ref, "git ref contains disallowed characters")
        t0 = time_block()
        res = await ctx.sandbox.exec(f"git show {ref}", cwd="/workspace", timeout_sec=60)
        record_tool_call(
            ctx, "git_show", f"ref={ref}", exit_code=res.exit_code, duration_sec=time_block() - t0
        )
        return res.stdout

    return git_show


def make_git_log(ctx: TaskContext) -> Any:
    @function_tool
    async def git_log(args: list[str] | None = None) -> str:
        """Run `git log` with optional args. Default: -10 --oneline."""
        check_tool_allowed(ctx.task.permission, "git_log")
        rest = " ".join(args or ["-10", "--oneline"])
        t0 = time_block()
        res = await ctx.sandbox.exec(f"git log {rest}", cwd="/workspace", timeout_sec=60)
        record_tool_call(
            ctx, "git_log", f"args={rest}", exit_code=res.exit_code, duration_sec=time_block() - t0
        )
        return res.stdout

    return git_log


def make_git_checkout(ctx: TaskContext) -> Any:
    @function_tool
    async def git_checkout(target: str = "--", paths: list[str] | None = None) -> CommandResult:
        """Reset worktree to HEAD (default) or to specific paths.

        Used at task start for the v3.1 idempotent-writes rule:
            git_checkout(target="--")  → discards all uncommitted changes.

        FORBIDDEN targets: branch refs (main, master, …). Use only for reset
        within the current branch.
        """
        check_tool_allowed(ctx.task.permission, "git_checkout")
        if target in _FORBIDDEN_CHECKOUT_TARGETS:
            raise PermissionDeniedError(
                target, f"git_checkout target {target!r} looks like a branch name"
            )
        path_args = " ".join(paths or [])
        cmd = f"git checkout {target} {path_args}".strip()
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=60)
        record_tool_call(
            ctx,
            "git_checkout",
            f"target={target}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return res

    return git_checkout


# ---------------------------------------------------------------------------
# Coder output saving (handoff / patch / test_report)
# ---------------------------------------------------------------------------


def _require_own_task_id(ctx: TaskContext, task_id: str, tool: str) -> None:
    """Coder Workers may only emit outputs scoped to their own task_id."""
    if task_id != ctx.task.task_id:
        raise PermissionDeniedError(
            task_id,
            f"{tool}: Coder may only save outputs for its own task "
            f"({ctx.task.task_id}); got {task_id}",
        )


def make_save_patch(ctx: TaskContext) -> Any:
    @function_tool
    async def save_patch(task_id: str) -> str:
        """Generate the worktree diff vs HEAD and save to patches/<task_id>.diff."""
        _require_own_task_id(ctx, task_id, "save_patch")
        check_tool_allowed(ctx.task.permission, "save_patch")
        t0 = time_block()
        diff = await ctx.sandbox.exec("git diff HEAD", cwd="/workspace", timeout_sec=60)
        if diff.exit_code != 0 and "fatal" in diff.stderr.lower():
            raise SandboxError(f"git diff failed: {diff.stderr}")
        relpath = f"patches/{task_id}.diff"
        try:
            ctx.store.write_text(relpath, diff.stdout)
        except Exception as e:
            raise ArtifactError(f"save_patch: {e}") from e
        record_tool_call(
            ctx,
            "save_patch",
            f"task_id={task_id} bytes={len(diff.stdout)}",
            duration_sec=time_block() - t0,
        )
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor=ctx.task.owner if isinstance(ctx.task.owner, str) else ctx.task.owner.value,  # type: ignore[union-attr]
            path=relpath,
            task_id=task_id,
        )
        return relpath

    return save_patch


def make_save_handoff(ctx: TaskContext) -> Any:
    @function_tool
    async def save_handoff(
        task_id: str,
        completed: list[str],
        incomplete: list[str] | None = None,
        commands_run: list[dict[str, Any]] | None = None,
        issues_discovered: list[str] | None = None,
        deviations_from_plan: list[str] | None = None,
        contract_coverage: list[dict[str, Any]] | None = None,
        dependency_changes: list[dict[str, Any]] | None = None,
        unsafe_usage: list[dict[str, Any]] | None = None,
        next_recommended_action: str = "send_to_review_validator",
    ) -> str:
        """Save the v3.1 handoff for this task. Validates against the Handoff schema.

        At least one of {incomplete, issues_discovered, deviations_from_plan}
        SHOULD be non-empty — the framework flags handoffs with all three empty
        as `triggers_second_pass=True` so ReviewValidator runs adversarial check.
        """
        _require_own_task_id(ctx, task_id, "save_handoff")
        check_tool_allowed(ctx.task.permission, "save_handoff")
        try:
            handoff = Handoff(
                task_id=task_id,
                completed=completed,
                incomplete=incomplete or [],
                commands_run=commands_run or [],  # type: ignore[arg-type]
                issues_discovered=issues_discovered or [],
                deviations_from_plan=deviations_from_plan or [],
                contract_coverage=contract_coverage or [],  # type: ignore[arg-type]
                dependency_changes=dependency_changes or [],  # type: ignore[arg-type]
                unsafe_usage=unsafe_usage or [],  # type: ignore[arg-type]
                next_recommended_action=next_recommended_action,
            )
        except Exception as e:  # pydantic ValidationError or similar
            raise ArtifactError(f"save_handoff: validation failed: {e}") from e
        try:
            path = ctx.store.save_handoff(task_id, handoff)
        except Exception as e:
            raise ArtifactError(f"save_handoff: store rejected: {e}") from e
        record_tool_call(
            ctx,
            "save_handoff",
            f"task_id={task_id} completed={len(completed)}",
        )
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor=ctx.task.owner if isinstance(ctx.task.owner, str) else ctx.task.owner.value,  # type: ignore[union-attr]
            path=str(path),
            task_id=task_id,
        )
        return str(path)

    return save_handoff


def make_save_test_report(ctx: TaskContext) -> Any:
    @function_tool
    async def save_test_report(task_id: str, report: dict[str, Any]) -> str:
        """Save reports/<task_id>.test.json from the Coder's self-test results."""
        _require_own_task_id(ctx, task_id, "save_test_report")
        check_tool_allowed(ctx.task.permission, "save_test_report")
        relpath = f"reports/{task_id}.test.json"
        try:
            ctx.store.write_json(relpath, report)
        except Exception as e:
            raise ArtifactError(f"save_test_report: {e}") from e
        record_tool_call(ctx, "save_test_report", f"task_id={task_id}")
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor=ctx.task.owner if isinstance(ctx.task.owner, str) else ctx.task.owner.value,  # type: ignore[union-attr]
            path=relpath,
            task_id=task_id,
        )
        return relpath

    return save_test_report


# ---------------------------------------------------------------------------
# Public list — used by CoderWorkerAgent.build_tools
# ---------------------------------------------------------------------------


def build_coder_tools(ctx: TaskContext) -> list[Any]:
    """Build the full set of Coder Worker tools bound to `ctx`."""
    return [
        make_read_file(ctx),
        make_write_file(ctx),
        make_edit_file(ctx),
        make_run_bash(ctx),
        make_cargo_check(ctx),
        make_cargo_test(ctx),
        make_cargo_clippy(ctx),
        make_cargo_fmt(ctx),
        make_cargo_nextest(ctx),
        make_git_status(ctx),
        make_git_diff(ctx),
        make_git_show(ctx),
        make_git_log(ctx),
        make_git_checkout(ctx),
        make_save_patch(ctx),
        make_save_handoff(ctx),
        make_save_test_report(ctx),
    ]


__all__ = [
    "build_coder_tools",
    "make_cargo_check",
    "make_cargo_clippy",
    "make_cargo_fmt",
    "make_cargo_nextest",
    "make_cargo_test",
    "make_edit_file",
    "make_git_checkout",
    "make_git_diff",
    "make_git_log",
    "make_git_show",
    "make_git_status",
    "make_read_file",
    "make_run_bash",
    "make_save_handoff",
    "make_save_patch",
    "make_save_test_report",
    "make_write_file",
]
