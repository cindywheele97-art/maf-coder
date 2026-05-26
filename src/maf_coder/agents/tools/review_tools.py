"""ReviewValidator tool factories (AGENT_TOOLS_SPEC §8).

Read-only on code, write-only to its own verdict + notes. The two
distinguishing capabilities (vs Coder tools):

1. `apply_patch_in_fresh_worktree(patch_path, base_ref="HEAD")` —
   reconstructs a clean worktree from `base_ref` and applies the saved patch
   to verify it lands without conflicts.

2. `spawn_adversarial_subagent(purpose, inputs, instructions_override=None)`
   — spins up a one-shot SDK Agent with NO tools, on a DIFFERENT provider
   from both the Coder AND the ReviewValidator that called it. The sub-agent
   only sees the inputs the parent supplies — never the Coder's narration or
   the parent's reasoning trace. This is the v3.1 hardcoded-test detector.

Plus the standard read-only suite (read_file/grep/git_diff/git_show/git_log)
and cargo gate reruns (cargo_check / cargo_test / cargo_clippy / cargo_fmt /
cargo_nextest / cargo_build / cargo_test_doc).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ...schemas import CargoGateResults, ReviewVerdict, Role
from .._sdk import SDK_AVAILABLE, LitellmModel, ModelSettings, Runner, function_tool
from ..base import TaskContext
from ..errors import (
    ArtifactError,
    PermissionDeniedError,
    SandboxError,
)
from ..permissions import check_tool_allowed
from ..results import CommandResult
from . import record_tool_call, time_block
from .coder_tools import (
    make_cargo_check,
    make_cargo_clippy,
    make_cargo_fmt,
    make_cargo_nextest,
    make_cargo_test,
    make_git_diff,
    make_git_log,
    make_git_show,
    make_git_status,
    make_read_file,
    make_run_bash,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# cargo_build, cargo_test_doc — Review-specific gate
# ---------------------------------------------------------------------------


def make_cargo_build(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_build(args: list[str] | None = None) -> CommandResult:
        """Run `cargo build` with optional args. Default: `--workspace --all-targets`."""
        check_tool_allowed(ctx.task.permission, "cargo_build")
        extras = " ".join(args or [])
        cmd = f"cargo build --workspace --all-targets {extras}".strip()
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=600)
        record_tool_call(
            ctx,
            "cargo_build",
            f"cmd={cmd[:120]}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return res

    return cargo_build


def make_cargo_test_doc(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_test_doc() -> CommandResult:
        """Run doctests: `cargo test --workspace --doc`."""
        check_tool_allowed(ctx.task.permission, "cargo_test_doc")
        cmd = "cargo test --workspace --doc"
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=600)
        record_tool_call(
            ctx, "cargo_test_doc", "", exit_code=res.exit_code, duration_sec=time_block() - t0
        )
        return res

    return cargo_test_doc


# ---------------------------------------------------------------------------
# Patch verification
# ---------------------------------------------------------------------------


def make_apply_patch_in_fresh_worktree(ctx: TaskContext) -> Any:
    @function_tool
    async def apply_patch_in_fresh_worktree(
        patch_path: str, base_ref: str = "HEAD"
    ) -> dict[str, Any]:
        """Apply patches/<task_id>.diff to a fresh worktree at base_ref.

        Verifies the patch applies cleanly. Returns:
          {applied: bool, conflicts: list[str], files_changed: list[str]}
        """
        check_tool_allowed(ctx.task.permission, "apply_patch_in_fresh_worktree")
        # Read the patch from the blackboard (mission scope).
        try:
            patch_text = ctx.store.read_text(patch_path)
        except Exception as e:
            raise ArtifactError(
                f"apply_patch_in_fresh_worktree: cannot read {patch_path}: {e}"
            ) from e

        # Use paths RELATIVE to /workspace so the sandbox layer maps them
        # consistently and the underlying shell resolves them against cwd.
        # Passing absolute "/workspace/..." to `git` would leak the container
        # convention onto the host (and `git` would try to create paths under
        # the literal host root).
        stamp = int(time.time() * 1000)
        worktree_rel = f".maf_wt_{stamp}"
        patch_rel = f"{worktree_rel}.patch"  # sibling of the worktree dir
        t0 = time_block()
        try:
            await ctx.sandbox.write_file(patch_rel, patch_text)
            add = await ctx.sandbox.exec(
                f"git worktree add --detach {worktree_rel} {base_ref}",
                cwd="/workspace",
                timeout_sec=60,
            )
            if add.exit_code != 0:
                raise SandboxError(f"git worktree add failed: {add.stderr}")

            # From inside the worktree, the patch sits one directory up.
            patch_from_wt = f"../{patch_rel}"
            check = await ctx.sandbox.exec(
                f"git apply --check {patch_from_wt}",
                cwd=f"/workspace/{worktree_rel}",
                timeout_sec=60,
            )
            applied = check.exit_code == 0
            conflicts: list[str] = []
            files_changed: list[str] = []
            if applied:
                _ = await ctx.sandbox.exec(
                    f"git apply {patch_from_wt}",
                    cwd=f"/workspace/{worktree_rel}",
                    timeout_sec=60,
                )
                diff = await ctx.sandbox.exec(
                    "git diff --name-only",
                    cwd=f"/workspace/{worktree_rel}",
                    timeout_sec=30,
                )
                files_changed = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
            else:
                conflicts = [ln.strip() for ln in check.stderr.splitlines() if ln.strip()]
        finally:
            # Best-effort cleanup. Use relative paths under /workspace.
            await ctx.sandbox.exec(
                f"git worktree remove --force {worktree_rel} 2>/dev/null || "
                f"rm -rf {worktree_rel}; rm -f {patch_rel}",
                cwd="/workspace",
                timeout_sec=30,
            )

        result = {"applied": applied, "conflicts": conflicts, "files_changed": files_changed}
        record_tool_call(
            ctx,
            "apply_patch_in_fresh_worktree",
            f"patch={patch_path} base={base_ref} applied={applied}",
            duration_sec=time_block() - t0,
        )
        return result

    return apply_patch_in_fresh_worktree


# ---------------------------------------------------------------------------
# spawn_adversarial_subagent — the v3.1 cornerstone
# ---------------------------------------------------------------------------


_DEFAULT_SUBAGENT_PROMPTS: dict[str, str] = {
    "intent_test_detection": (
        "You are an adversarial test auditor. You are reviewing a patch and "
        "its accompanying tests for the v3.1 'hardcoded-test' anti-pattern: "
        "tests that pass without actually verifying the user-facing intent.\n\n"
        "Given the inputs (patch content, test content, contract assertions), "
        "produce a JSON object with two fields:\n"
        "  findings: list of strings — each is a concrete suspicion with file:line\n"
        "  adversarial_tests_generated: list of strings — short test names you "
        "would add to verify the intent that the existing tests appear to skip.\n\n"
        "Output JSON ONLY. No commentary."
    ),
    "completeness_second_pass": (
        "You are a skeptical reviewer. The Coder Worker handed off a 'too clean' "
        "handoff (no incomplete items, no issues, no plan deviations). Adversarial "
        "experience says this is suspicious. Independently inspect the patch, the "
        "tests, and the validation contract. Report any deviation the Coder did "
        "not flag.\n\n"
        "Output JSON with:\n"
        "  findings: list of strings\n"
        "  adversarial_tests_generated: list of strings\n"
        "Output JSON ONLY."
    ),
}


def _build_subagent_message(purpose: str, inputs: dict[str, Any]) -> str:
    return (
        "## Purpose: "
        + purpose
        + "\n\n## Inputs (JSON)\n```json\n"
        + json.dumps(inputs, indent=2, ensure_ascii=False)
        + "\n```\n"
    )


def _parse_subagent_output(raw: str) -> dict[str, Any]:
    """Tolerant parse: prefer JSON, fall back to best-effort extraction."""
    text = raw.strip()
    # Common case: model wraps in ```json ... ```
    if "```" in text:
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[len("json") :]
    try:
        obj = json.loads(text)
    except Exception:
        return {"findings": [raw.strip()], "adversarial_tests_generated": []}
    findings = obj.get("findings", [])
    tests = obj.get("adversarial_tests_generated", [])
    if not isinstance(findings, list):
        findings = [str(findings)]
    if not isinstance(tests, list):
        tests = [str(tests)]
    return {
        "findings": [str(f) for f in findings],
        "adversarial_tests_generated": [str(t) for t in tests],
    }


def make_spawn_adversarial_subagent(ctx: TaskContext) -> Any:
    @function_tool
    async def spawn_adversarial_subagent(
        purpose: str,
        inputs: dict[str, Any],
        instructions_override: str | None = None,
    ) -> dict[str, Any]:
        """Spawn a one-shot sub-agent with scoped context (v3.1 cornerstone).

        purpose: 'intent_test_detection' | 'completeness_second_pass' | other

        inputs: dict the sub-agent sees. MUST NOT contain the Coder's handoff
        text or the parent ReviewValidator's reasoning.

        Returns: {findings: list[str], adversarial_tests_generated: list[str]}

        The sub-agent runs on a DIFFERENT provider from BOTH the Coder (per
        mission's coder_provider_in_use) AND this parent agent. Constraint
        enforcement happens in ModelRouter.
        """
        if ctx.task.owner not in (
            Role.REVIEW_VALIDATOR.value,
            Role.REVIEW_VALIDATOR,
        ):
            raise PermissionDeniedError(
                "spawn_adversarial_subagent",
                f"only review_validator may call (current owner: {ctx.task.owner})",
            )
        check_tool_allowed(ctx.task.permission, "spawn_adversarial_subagent")

        # Resolve a sub-agent model that differs from Coder's provider.
        sub_model = ctx.router.get_primary_model(
            Role.ADVERSARIAL_SUBAGENT.value,
            coder_provider_in_use=ctx.coder_provider_in_use,
        )
        prompt = instructions_override or _DEFAULT_SUBAGENT_PROMPTS.get(
            purpose,
            "You are an adversarial reviewer. Inspect the inputs and report concrete findings.",
        )
        msg = _build_subagent_message(purpose, inputs)

        ctx.event_log.log_second_pass_triggered(
            mission_id=ctx.mission_id,
            task_id=ctx.task.task_id,
            reason=f"adversarial_subagent purpose={purpose}",
        )

        t0 = time_block()
        try:
            raw = await _run_subagent_sdk(prompt=prompt, message=msg, model_id=sub_model.model)
        except Exception as e:
            logger.warning("Sub-agent SDK execution failed: %r", e)
            # Don't block the parent — return a structured "couldn't run" result.
            result = {
                "findings": [f"sub-agent execution failed: {type(e).__name__}: {e}"],
                "adversarial_tests_generated": [],
            }
        else:
            result = _parse_subagent_output(raw)

        record_tool_call(
            ctx,
            "spawn_adversarial_subagent",
            f"purpose={purpose} findings={len(result['findings'])}",
            duration_sec=time_block() - t0,
        )
        return result

    return spawn_adversarial_subagent


async def _run_subagent_sdk(*, prompt: str, message: str, model_id: str) -> str:
    """Direct SDK call for sub-agent. Isolated so tests can monkeypatch it."""
    if not SDK_AVAILABLE:
        raise RuntimeError(
            "spawn_adversarial_subagent requires the OpenAI Agents SDK or a "
            "monkeypatched override of _run_subagent_sdk."
        )
    from .._sdk import Agent  # local import to support monkeypatch in tests

    agent_kwargs: dict[str, Any] = {
        "name": "adversarial_subagent",
        "instructions": prompt,
        "tools": [],  # adversarial sub-agent has no tools (v3.1 cornerstone)
    }
    if LitellmModel is not None:
        agent_kwargs["model"] = LitellmModel(model_id)
    if ModelSettings is not None:
        agent_kwargs["model_settings"] = ModelSettings(temperature=0.0)
    sub = Agent(**agent_kwargs)
    sdk_res = await Runner.run(sub, message)
    return str(getattr(sdk_res, "final_output", "") or "")


# ---------------------------------------------------------------------------
# Verdict / notes saving
# ---------------------------------------------------------------------------


def make_save_review_verdict(ctx: TaskContext) -> Any:
    @function_tool
    async def save_review_verdict(
        task_id: str,
        result: str,
        precise_reason: str,
        next_action_recommendation: str,
        cargo_gate_results: dict[str, Any],
        assertion_results: list[dict[str, Any]] | None = None,
        triggered_second_pass: bool = False,
        adversarial_findings: list[str] | None = None,
        hardcoded_test_warnings: list[str] | None = None,
    ) -> str:
        """Save verdicts/<task_id>.review.json. Validates against ReviewVerdict schema."""
        check_tool_allowed(ctx.task.permission, "save_review_verdict")
        try:
            gate = CargoGateResults(**cargo_gate_results)
        except Exception as e:
            raise ArtifactError(f"save_review_verdict: cargo_gate_results invalid: {e}") from e
        try:
            verdict = ReviewVerdict(
                task_id=task_id,
                result=result,  # type: ignore[arg-type]
                precise_reason=precise_reason,
                next_action_recommendation=next_action_recommendation,
                cargo_gate_results=gate,
                assertion_results=assertion_results or [],  # type: ignore[arg-type]
                triggered_second_pass=triggered_second_pass,
                adversarial_findings=adversarial_findings or [],
                hardcoded_test_warnings=hardcoded_test_warnings or [],
            )
        except Exception as e:
            raise ArtifactError(f"save_review_verdict: validation failed: {e}") from e
        path = ctx.store.save_review_verdict(task_id, verdict)
        record_tool_call(ctx, "save_review_verdict", f"task_id={task_id} result={result}")
        ctx.event_log.log_validator_verdict(
            mission_id=ctx.mission_id,
            task_id=task_id,
            validator="review_validator",
            result=result,
            triggered_second_pass=triggered_second_pass,
        )
        return str(path)

    return save_review_verdict


def make_save_review_notes(ctx: TaskContext) -> Any:
    @function_tool
    async def save_review_notes(task_id: str, notes_markdown: str) -> str:
        """Save review_notes/<task_id>.md."""
        check_tool_allowed(ctx.task.permission, "save_review_notes")
        relpath = f"review_notes/{task_id}.md"
        try:
            ctx.store.write_text(relpath, notes_markdown)
        except Exception as e:
            raise ArtifactError(f"save_review_notes: {e}") from e
        record_tool_call(ctx, "save_review_notes", f"task_id={task_id}")
        return relpath

    return save_review_notes


# ---------------------------------------------------------------------------
# Read-only Coder tools rebadged for the Validator
# ---------------------------------------------------------------------------


def build_review_tools(ctx: TaskContext) -> list[Any]:
    return [
        make_read_file(ctx),
        make_run_bash(ctx),  # Validator may run things; permission layer constrains
        make_git_status(ctx),
        make_git_diff(ctx),
        make_git_show(ctx),
        make_git_log(ctx),
        make_cargo_check(ctx),
        make_cargo_build(ctx),
        make_cargo_test(ctx),
        make_cargo_clippy(ctx),
        make_cargo_fmt(ctx),
        make_cargo_nextest(ctx),
        make_cargo_test_doc(ctx),
        make_apply_patch_in_fresh_worktree(ctx),
        make_spawn_adversarial_subagent(ctx),
        make_save_review_verdict(ctx),
        make_save_review_notes(ctx),
    ]


__all__ = [
    "_run_subagent_sdk",  # exposed for monkeypatching in tests
    "build_review_tools",
    "make_apply_patch_in_fresh_worktree",
    "make_cargo_build",
    "make_cargo_test_doc",
    "make_save_review_notes",
    "make_save_review_verdict",
    "make_spawn_adversarial_subagent",
]
