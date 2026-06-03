"""Orchestrator tool factories (AGENT_TOOLS_SPEC §6).

Orchestrator does NOT run code in the sandbox; all of its tools are in-process
blackboard / scheduler operations.

The scheduler is a *late-bound* dependency: tool factories accept it via the
optional `scheduler` parameter so unit tests can substitute a stub. Production
wiring passes the real `Scheduler` instance constructed by `MissionDriver`.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from ...blackboard.artifact_store import ContractAlreadyLockedError
from ...schemas import (
    Checkpoint,
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
)
from .._sdk import function_tool
from ..base import TaskContext
from ..errors import (
    ArtifactError,
    AssertionUnknownError,
    PermissionDeniedError,
    TaskAlreadyDispatchedError,
    ValidatorChainError,
)
from ..permissions import check_tool_allowed
from ..results import TaskHandle
from . import record_tool_call

logger = logging.getLogger(__name__)

# Paths Orchestrator is allowed to save via save_artifact (top-level only).
_ORCH_ALLOWED_SAVE_PREFIXES = (
    "plan.md",
    "tasks.yaml",
    "risk_register.md",
    "budget.yaml",
    "validation_contract.yaml",
    "final_answer.md",
    "mission_retro.md",
    "user_messages/",
    "research_notes/",
)

# mission_state.json keys agents may patch directly via update_mission_state.
_MS_AGENT_PATCHABLE = {
    "current_milestone",
    "last_status_report_at",
    "coder_provider_in_use",
}


class _SchedulerLike(Protocol):
    """Minimal interface the Orchestrator tools need from the Scheduler."""

    async def add_task(self, task: Task) -> TaskHandle: ...
    def has_task(self, task_id: str) -> bool: ...


def _scheduler_task_owner(scheduler: _SchedulerLike | None, task_id: str) -> str | None:
    """Resolve a dependency's owner role via the scheduler, if it exposes it.

    The real Scheduler implements ``task_owner``; lighter stubs may not. We probe
    defensively so the dual-validator gate degrades gracefully (it only fires
    when owner resolution is available) rather than breaking callers that pass a
    minimal scheduler.
    """
    owner_fn = getattr(scheduler, "task_owner", None)
    if owner_fn is None:
        return None
    result = owner_fn(task_id)
    return result if isinstance(result, str) else None


def _find_review_dependency(
    scheduler: _SchedulerLike | None, depends_on: list[str]
) -> str | None:
    """Return the first dependency owned by review_validator, or None.

    Generic resolution: scans the behavior task's declared dependencies and
    matches on owner role, so the chain gate never hardcodes specific task IDs.
    """
    for dep_id in depends_on:
        if _scheduler_task_owner(scheduler, dep_id) == Role.REVIEW_VALIDATOR.value:
            return dep_id
    return None


def _require_orchestrator(ctx: TaskContext, tool: str) -> None:
    owner = ctx.task.owner
    owner_str = owner.value if hasattr(owner, "value") else str(owner)
    if owner_str != Role.ORCHESTRATOR.value:
        raise PermissionDeniedError(
            tool, f"only orchestrator may call (current owner: {owner_str})"
        )


# ---------------------------------------------------------------------------
# dispatch_task
# ---------------------------------------------------------------------------


def make_dispatch_task(ctx: TaskContext, *, scheduler: _SchedulerLike | None = None) -> Any:
    @function_tool
    async def dispatch_task(
        task_id: str,
        owner: str,
        goal: str,
        background: str,
        acceptance_criteria: list[str],
        depends_on: list[str] | None = None,
        input_artifacts: list[str] | None = None,
        required_outputs: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        network_policy: str = "none",
        max_tokens: int = 100_000,
        max_runtime_sec: int = 600,
        risk_level: str = "low",
        milestone_id: str | None = None,
    ) -> dict[str, Any]:
        """Schedule a task in the mission DAG. Orchestrator-only.

        `milestone_id` tags the task with the milestone it belongs to (use the
        plan.md milestone name, matching mission_state.current_milestone). When
        omitted, the task inherits the Orchestrator turn's own milestone.
        """
        _require_orchestrator(ctx, "dispatch_task")
        check_tool_allowed(ctx.task.permission, "dispatch_task")

        # Acceptance criteria must reference assertions present in the contract.
        try:
            contract = ctx.store.load_validation_contract()
        except FileNotFoundError as e:
            raise ArtifactError("dispatch_task: validation_contract.yaml not yet locked") from e
        known_assertions: set[str] = set()
        for feature in contract.features:
            for assertion in feature.assertions:
                known_assertions.add(assertion.id)
        for ac in acceptance_criteria:
            if ac not in known_assertions:
                raise AssertionUnknownError(
                    f"dispatch_task: acceptance_criteria id {ac!r} not in contract "
                    f"(known: {sorted(known_assertions)})"
                )

        if scheduler is not None and scheduler.has_task(task_id):
            raise TaskAlreadyDispatchedError(f"dispatch_task: task_id={task_id!r} already exists")

        try:
            np_enum = NetworkPolicy(network_policy)
        except ValueError as e:
            raise ArtifactError(f"dispatch_task: invalid network_policy: {e}") from e
        try:
            risk_enum = RiskLevel(risk_level)
        except ValueError as e:
            raise ArtifactError(f"dispatch_task: invalid risk_level: {e}") from e
        try:
            owner_enum = Role(owner)
        except ValueError as e:
            raise ArtifactError(f"dispatch_task: invalid owner: {e}") from e

        # Dual-validator chain — structural gate (Phase D §D3, condition (a)):
        # a behavior_validator task MUST depend on at least one review_validator
        # task. The verdict-PASS check (condition (b)) happens later, when the
        # task is actually about to run (Scheduler._is_ready), because the review
        # verdict file does not exist yet at DAG-construction time. We resolve the
        # review dependency generically by owner role — never by literal ID.
        if owner_enum is Role.BEHAVIOR_VALIDATOR and scheduler is not None:
            review_dep = _find_review_dependency(scheduler, depends_on or [])
            if review_dep is None:
                raise ValidatorChainError(
                    f"dispatch_task: behavior_validator task {task_id!r} must depend on a "
                    f"review_validator task (depends_on={depends_on or []}); refusing dispatch"
                )

        task = Task(
            task_id=task_id,
            parent_milestone=milestone_id or ctx.task.parent_milestone,
            owner=owner_enum,
            priority=risk_enum,
            risk_level=risk_enum,
            goal=goal,
            background=background,
            acceptance_criteria=acceptance_criteria,
            input_artifacts=input_artifacts or [],
            required_outputs=required_outputs or [],
            permission=Permission(
                allowed_paths=allowed_paths or [],
                allowed_tools=allowed_tools or [],
                network_policy=np_enum,
            ),
            budget=TaskBudget(max_tokens=max_tokens, max_runtime_sec=max_runtime_sec),
            depends_on=depends_on or [],
        )

        if scheduler is None:
            handle = TaskHandle(task_id=task_id, dispatched_at=time.monotonic())
            ctx.event_log.log_task_dispatched(
                mission_id=ctx.mission_id,
                task_id=task_id,
                owner=owner_enum.value,
                priority=risk_enum.value,
            )
        else:
            handle = await scheduler.add_task(task)

        record_tool_call(ctx, "dispatch_task", f"task_id={task_id} owner={owner_enum.value}")
        return {
            "task_id": handle.task_id,
            "dispatched_at": handle.dispatched_at,
        }

    return dispatch_task


# ---------------------------------------------------------------------------
# read_artifact / save_artifact
# ---------------------------------------------------------------------------


def make_read_artifact(ctx: TaskContext) -> Any:
    @function_tool
    async def read_artifact(path: str) -> str:
        """Read an artifact from the mission blackboard."""
        check_tool_allowed(ctx.task.permission, "read_artifact")
        try:
            content = ctx.store.read_text(path)
        except FileNotFoundError as e:
            raise ArtifactError(f"read_artifact: {path}: not found") from e
        except Exception as e:
            raise ArtifactError(f"read_artifact: {path}: {e}") from e
        record_tool_call(ctx, "read_artifact", f"path={path}")
        # Truncate very large content for the agent's view, full content remains on disk.
        if len(content) > 1_048_576:
            return content[:1_048_576] + "\n... [TRUNCATED at 1MB]"
        return content

    return read_artifact


def make_save_artifact(ctx: TaskContext) -> Any:
    @function_tool
    async def save_artifact(path: str, content: str) -> str:
        """Write a top-level Orchestrator artifact to the mission blackboard."""
        _require_orchestrator(ctx, "save_artifact")
        check_tool_allowed(ctx.task.permission, "save_artifact")
        if not any(path == p or path.startswith(p) for p in _ORCH_ALLOWED_SAVE_PREFIXES):
            raise PermissionDeniedError(
                path,
                f"orchestrator may not save_artifact to {path!r}; "
                f"allowed prefixes: {_ORCH_ALLOWED_SAVE_PREFIXES}",
            )
        if path == "validation_contract.yaml" and ctx.store.exists(path):
            raise ContractAlreadyLockedError(
                "save_artifact: validation_contract.yaml is write-once and already exists"
            )
        try:
            written = ctx.store.write_text(path, content)
        except Exception as e:
            raise ArtifactError(f"save_artifact: {path}: {e}") from e
        record_tool_call(ctx, "save_artifact", f"path={path} bytes={len(content)}")
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor="orchestrator",
            path=path,
            task_id=ctx.task.task_id,
        )
        return str(written)

    return save_artifact


# ---------------------------------------------------------------------------
# emit_event
# ---------------------------------------------------------------------------


def make_emit_event(ctx: TaskContext) -> Any:
    @function_tool
    async def emit_event(kind: str, payload: dict[str, Any] | None = None) -> None:
        """Emit a custom event to the mission EventLog."""
        _require_orchestrator(ctx, "emit_event")
        check_tool_allowed(ctx.task.permission, "emit_event")
        from ...blackboard.event_log import Event

        ctx.event_log.append(
            Event(
                kind=kind,
                mission_id=ctx.mission_id,
                trace_id=ctx.mission_id,
                task_id=ctx.task.task_id,
                actor="orchestrator",
                payload=payload or {},
            )
        )
        record_tool_call(ctx, "emit_event", f"kind={kind}")

    return emit_event


# ---------------------------------------------------------------------------
# escalate_to_human_gate
# ---------------------------------------------------------------------------


def make_escalate_to_human_gate(ctx: TaskContext) -> Any:
    @function_tool
    async def escalate_to_human_gate(
        reason: str,
        options: list[str],
        recommendation: str | None = None,
        timeout_action: str = "pause_mission",
        timeout_hours: int = 24,
    ) -> None:
        """Create user_messages/_pending_<ts>.md requesting human approval."""
        _require_orchestrator(ctx, "escalate_to_human_gate")
        check_tool_allowed(ctx.task.permission, "escalate_to_human_gate")
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = f"user_messages/_pending_{ts}.md"
        body_lines = [
            "# Escalation pending Human Gate",
            f"_created: {datetime.now(UTC).isoformat()}_",
            "",
            "## Reason",
            reason,
            "",
            "## Options",
        ]
        for i, opt in enumerate(options, 1):
            body_lines.append(f"{i}. {opt}")
        if recommendation:
            body_lines += ["", "## Recommendation", recommendation]
        body_lines += [
            "",
            f"_timeout_action: {timeout_action} after {timeout_hours}h_",
        ]
        ctx.store.write_text(path, "\n".join(body_lines) + "\n")
        ctx.event_log.log_escalation(
            mission_id=ctx.mission_id,
            target="human_gate",
            reason=reason,
            task_id=ctx.task.task_id,
        )
        record_tool_call(ctx, "escalate_to_human_gate", f"reason={reason[:60]}")

    return escalate_to_human_gate


# ---------------------------------------------------------------------------
# create_checkpoint
# ---------------------------------------------------------------------------


def make_create_checkpoint(ctx: TaskContext) -> Any:
    @function_tool
    async def create_checkpoint(milestone_id: str) -> dict[str, Any]:
        """Snapshot mission at a milestone boundary (git tag + sandbox commit)."""
        _require_orchestrator(ctx, "create_checkpoint")
        check_tool_allowed(ctx.task.permission, "create_checkpoint")
        git_tag = f"mission/{ctx.mission_id}/{milestone_id}"
        # Best-effort git tag in the sandbox. Non-fatal if it fails.
        tag_res = await ctx.sandbox.exec(f"git tag -f {git_tag}", cwd="/workspace", timeout_sec=30)
        try:
            snapshot_id = await ctx.sandbox.commit_snapshot(image_tag=git_tag)
        except Exception as e:
            logger.warning("commit_snapshot failed: %r", e)
            snapshot_id = ""
        archive_rel = f"checkpoints/{milestone_id}/"
        ctx.store.write_text(
            archive_rel + "MANIFEST.txt",
            f"milestone={milestone_id}\ngit_tag={git_tag}\n",
        )

        # Update mission_state if it exists.
        try:
            ms = ctx.store.load_mission_state()
            if milestone_id not in ms.completed_milestones:
                ms.completed_milestones.append(milestone_id)
            ms.last_checkpoint_at = datetime.now(UTC)
            ctx.store.save_mission_state(ms)
        except FileNotFoundError:
            logger.warning("create_checkpoint: mission_state.json missing")
        except Exception as e:
            logger.exception("create_checkpoint: mission_state update failed: %r", e)

        checkpoint = Checkpoint(
            mission_id=ctx.mission_id,
            milestone_id=milestone_id,
            git_tag=git_tag,
            sandbox_snapshot_id=snapshot_id or "unknown",
            artifact_archive_path=archive_rel,
            cumulative_cost_usd=ctx.event_log.total_cost_usd(),
            cumulative_wall_clock_hours=0.0,
        )
        ctx.store.save_checkpoint(checkpoint)
        ctx.event_log.log_checkpoint_created(
            mission_id=ctx.mission_id,
            milestone_id=milestone_id,
            git_tag=git_tag,
            snapshot_id=snapshot_id or "",
        )
        record_tool_call(
            ctx,
            "create_checkpoint",
            f"milestone={milestone_id} tag_exit={tag_res.exit_code}",
        )
        return {
            "milestone_id": milestone_id,
            "git_tag": git_tag,
            "snapshot_id": snapshot_id,
            "archive_path": archive_rel,
            "cumulative_cost_usd": checkpoint.cumulative_cost_usd,
        }

    return create_checkpoint


# ---------------------------------------------------------------------------
# poll_user_messages / mark_user_message_processed
# ---------------------------------------------------------------------------


def make_poll_user_messages(ctx: TaskContext) -> Any:
    @function_tool
    async def poll_user_messages() -> list[dict[str, Any]]:
        """Return unprocessed user messages, urgent first."""
        _require_orchestrator(ctx, "poll_user_messages")
        check_tool_allowed(ctx.task.permission, "poll_user_messages")
        # Single source of truth for inbox read/parse lives in orchestrator.inbox.
        from ...orchestrator.inbox import read_inbox_entries

        entries = [
            {
                "filename": e.filename,
                "path": e.path,
                "content": e.content,
                "urgent": e.urgent,
                "created_at": e.created_at,
            }
            for e in read_inbox_entries(ctx.store)
        ]
        record_tool_call(ctx, "poll_user_messages", f"count={len(entries)}")
        return entries

    return poll_user_messages


def make_mark_user_message_processed(ctx: TaskContext) -> Any:
    @function_tool
    async def mark_user_message_processed(filename: str) -> None:
        """Move user_messages/<filename> -> processed_messages/<filename>."""
        _require_orchestrator(ctx, "mark_user_message_processed")
        check_tool_allowed(ctx.task.permission, "mark_user_message_processed")
        # Single source of truth for archive logic lives in orchestrator.inbox.
        from ...orchestrator.inbox import archive_message

        try:
            archive_message(ctx.store, filename)
        except FileNotFoundError as e:
            raise ArtifactError(f"mark_user_message_processed: {e}") from e
        except OSError as e:
            raise ArtifactError(f"mark_user_message_processed: {e}") from e
        record_tool_call(ctx, "mark_user_message_processed", f"filename={filename}")

    return mark_user_message_processed


# ---------------------------------------------------------------------------
# get_mission_state / update_mission_state
# ---------------------------------------------------------------------------


def make_get_mission_state(ctx: TaskContext) -> Any:
    @function_tool
    async def get_mission_state() -> dict[str, Any]:
        """Return current mission_state.json as a dict."""
        _require_orchestrator(ctx, "get_mission_state")
        check_tool_allowed(ctx.task.permission, "get_mission_state")
        try:
            ms = ctx.store.load_mission_state()
        except FileNotFoundError as e:
            raise ArtifactError("get_mission_state: mission_state.json missing") from e
        record_tool_call(ctx, "get_mission_state", "")
        return ms.model_dump(mode="json")

    return get_mission_state


def make_update_mission_state(ctx: TaskContext) -> Any:
    @function_tool
    async def update_mission_state(updates: dict[str, Any]) -> dict[str, Any]:
        """Patch mission_state.json. Only the agent-patchable subset is allowed."""
        _require_orchestrator(ctx, "update_mission_state")
        check_tool_allowed(ctx.task.permission, "update_mission_state")
        bad = set(updates) - _MS_AGENT_PATCHABLE
        if bad:
            raise PermissionDeniedError(
                "update_mission_state",
                f"keys {sorted(bad)} are framework-managed; allowed: {sorted(_MS_AGENT_PATCHABLE)}",
            )
        try:
            ms = ctx.store.load_mission_state()
        except FileNotFoundError as e:
            raise ArtifactError("update_mission_state: mission_state.json missing") from e
        patched = ms.model_dump()
        patched.update(updates)
        from ...schemas import MissionState

        new_ms = MissionState.model_validate(patched)
        ctx.store.save_mission_state(new_ms)
        record_tool_call(ctx, "update_mission_state", f"keys={sorted(updates.keys())}")
        return new_ms.model_dump(mode="json")

    return update_mission_state


# ---------------------------------------------------------------------------
# get_budget_status
# ---------------------------------------------------------------------------


def make_complete_mission(ctx: TaskContext) -> Any:
    @function_tool
    async def complete_mission(summary: str) -> dict[str, Any]:
        """Declare the mission goal fully delivered (sets mission_complete).

        Call this ONLY after the FINAL milestone's validators have PASSED (verdicts
        on disk) and the goal is met — typically in a turn that dispatches no new
        work. The Driver's per-milestone loop stops re-invoking the Orchestrator
        once this flag is set; calling it prematurely ends the mission early.
        """
        _require_orchestrator(ctx, "complete_mission")
        check_tool_allowed(ctx.task.permission, "complete_mission")
        try:
            ms = ctx.store.load_mission_state()
        except FileNotFoundError as e:
            raise ArtifactError("complete_mission: mission_state.json missing") from e
        ctx.store.save_mission_state(ms.model_copy(update={"mission_complete": True}))
        record_tool_call(ctx, "complete_mission", summary[:200])
        return {"mission_complete": True}

    return complete_mission


def make_get_budget_status(ctx: TaskContext) -> Any:
    @function_tool
    async def get_budget_status() -> dict[str, Any]:
        """Return current budget state derived from EventLog."""
        _require_orchestrator(ctx, "get_budget_status")
        check_tool_allowed(ctx.task.permission, "get_budget_status")
        cost = ctx.event_log.total_cost_usd()
        ti, to = ctx.event_log.total_tokens()
        # Budget config lives in budget.yaml; if absent, fall back to defaults.
        try:
            budget_cfg = ctx.store.read_yaml("budget.yaml")
        except Exception:
            budget_cfg = {}
        alert_threshold = float(budget_cfg.get("alert_threshold_usd", 50.0))
        # Naive projection: linear from current burn (no time window known here).
        projected = cost * 1.0
        result = {
            "tokens_used": ti + to,
            "cost_usd": cost,
            "alert_threshold_usd": alert_threshold,
            "projected_total_usd": projected,
            "wall_clock_vs_estimate_pct": 100.0,
            "current_mode": "cost_conscious" if cost >= alert_threshold else "normal",
        }
        record_tool_call(ctx, "get_budget_status", "")
        return result

    return get_budget_status


# ---------------------------------------------------------------------------
# save_retro  (Phase F — F-memory; additive, grouped for clean merge)
# ---------------------------------------------------------------------------


def make_save_retro(ctx: TaskContext) -> Any:
    @function_tool
    async def save_retro(
        goal: str,
        what_worked: list[str] | None = None,
        what_failed: list[str] | None = None,
        surprises: list[str] | None = None,
        global_lessons: list[str] | None = None,
        modules: list[str] | None = None,
    ) -> dict[str, Any]:
        """Assemble + persist the mission retro into cross-mission memory.

        Drafts a RetroEntry from the EventLog plus the supplied narrative,
        writes mission_retro.md to the blackboard, and ingests the retro into
        the per-repo ProjectMemory (global_lessons promoted to GlobalLessons).
        Orchestrator-only.
        """
        _require_orchestrator(ctx, "save_retro")
        check_tool_allowed(ctx.task.permission, "save_retro")

        # Lazy import keeps the memory package off the hot import path and
        # avoids any import cycle through agents/.
        from ...memory import assemble_retro, ingest_retro, render_retro_markdown
        from ...memory.paths import open_global_lessons, open_project_memory

        entry = assemble_retro(
            mission_id=ctx.mission_id,
            goal=goal,
            event_log=ctx.event_log,
            extra_worked=what_worked or [],
            extra_failed=what_failed or [],
            extra_surprises=surprises or [],
            global_lessons=global_lessons or [],
            modules=modules or [],
        )
        ctx.store.write_text("mission_retro.md", render_retro_markdown(entry))

        memory = open_project_memory(ctx.store)
        global_store = open_global_lessons(ctx.store)
        try:
            records = ingest_retro(entry, memory, global_lessons=global_store)
        finally:
            memory.close()
            global_store.close()

        record_tool_call(ctx, "save_retro", f"goal={goal[:50]} records={len(records)}")
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor="orchestrator",
            path="mission_retro.md",
            task_id=ctx.task.task_id,
        )
        return {
            "mission_id": ctx.mission_id,
            "records_ingested": len(records),
            "global_lessons": len(entry.global_lessons),
        }

    return save_retro


# ---------------------------------------------------------------------------
# create_pr  (F-pr: PR workflow — Build Plan §Phase F · F5)
# ---------------------------------------------------------------------------
# Mission-end action: run the gitleaks pre-PR secret gate, then the gh/glab
# wrapper. Refuses (does not call the CLI) if secrets are found. All process
# exec routes through ctx.sandbox.exec inside integrations.vcs. Kept grouped
# here (between the orchestrator factories and build_orchestrator_tools) so the
# concurrent F-memory addition stays merge-clean.


def make_create_pr(ctx: TaskContext) -> Any:
    @function_tool
    async def create_pr(
        repo_path: str,
        head_branch: str,
        base_branch: str = "main",
        provider: str = "gh",
        draft: bool = False,
        title: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        """Open a PR/MR from the finished mission. Orchestrator-only.

        Runs a final gitleaks secret scan first — if secrets are found the PR is
        REFUSED and the findings are surfaced (no CLI invoked). Otherwise a PR
        description is generated from the mission artifacts, the artifact
        directory is linked, and `gh pr create` / `glab mr create` runs in the
        sandbox. Returns the create result (url on success, refusal otherwise).
        """
        _require_orchestrator(ctx, "create_pr")
        check_tool_allowed(ctx.task.permission, "create_pr")

        # Lazy import keeps the integrations layer out of the agents import graph
        # until a PR is actually opened.
        from ...integrations.vcs import (
            build_artifact_links,
            create_pull_request,
            render_pr_body,
        )
        from ...schemas.pr import PullRequestSpec, VcsProvider

        try:
            provider_enum = VcsProvider(provider)
        except ValueError as e:
            raise ArtifactError(f"create_pr: invalid provider: {e}") from e

        artifact_links = build_artifact_links(ctx.store)
        body = render_pr_body(
            mission_id=ctx.mission_id,
            store=ctx.store,
            event_log=ctx.event_log,
            goal=goal,
            artifact_links=artifact_links,
        )
        pr_title = title or f"MAF-Coder: {ctx.mission_id}"
        spec = PullRequestSpec(
            mission_id=ctx.mission_id,
            title=pr_title,
            body=body,
            head_branch=head_branch,
            base_branch=base_branch,
            provider=provider_enum,
            draft=draft,
            repo_path=repo_path,
            artifact_links=artifact_links,
        )
        result = await create_pull_request(ctx, spec)
        record_tool_call(
            ctx,
            "create_pr",
            f"provider={provider} created={result.created} refused={result.refused}",
        )
        if result.refused:
            ctx.event_log.log_escalation(
                mission_id=ctx.mission_id,
                target="human_gate",
                reason=result.refusal_reason or "gitleaks gate refused PR",
                task_id=ctx.task.task_id,
            )
        return result.model_dump(mode="json")

    return create_pr


# ---------------------------------------------------------------------------
# Factory entry
# ---------------------------------------------------------------------------


def build_orchestrator_tools(
    ctx: TaskContext, *, scheduler: _SchedulerLike | None = None
) -> list[Any]:
    return [
        make_dispatch_task(ctx, scheduler=scheduler),
        make_read_artifact(ctx),
        make_save_artifact(ctx),
        make_emit_event(ctx),
        make_escalate_to_human_gate(ctx),
        make_create_checkpoint(ctx),
        make_poll_user_messages(ctx),
        make_mark_user_message_processed(ctx),
        make_get_mission_state(ctx),
        make_update_mission_state(ctx),
        make_complete_mission(ctx),
        make_get_budget_status(ctx),
        # Phase F — F-memory + F-pr (additive, grouped for clean merge)
        make_save_retro(ctx),
        make_create_pr(ctx),
    ]


__all__ = [
    "build_orchestrator_tools",
    "make_complete_mission",
    "make_create_checkpoint",
    "make_create_pr",  # F-pr
    "make_dispatch_task",
    "make_emit_event",
    "make_escalate_to_human_gate",
    "make_get_budget_status",
    "make_get_mission_state",
    "make_mark_user_message_processed",
    "make_poll_user_messages",
    "make_read_artifact",
    "make_save_artifact",
    # Phase F — F-memory (additive, grouped for clean merge)
    "make_save_retro",
    "make_update_mission_state",
]
