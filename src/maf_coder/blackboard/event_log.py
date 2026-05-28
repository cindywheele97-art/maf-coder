"""EventLog — append-only jsonl log of every meaningful mission event.

Why this exists:
    Multi-day missions need a single replayable event stream for:
    - Cost tracking (sum LLM call costs to drive budget guard + status report)
    - Token usage analysis (split by role, model, day)
    - Failure forensics (what was Coder doing 4 hours ago when it stalled)
    - Status Report generation (last N hours of activity)
    - mission_retro.md drafting (the post-hoc story of what happened)
    - Replay for debugging (re-run a mission from the event stream against a
      different agent version)

    Without a single canonical log, each agent invents its own logging format
    and the retro / status / cost-tracking layers become maintenance pits.

Design:
    - Append-only jsonl (one JSON object per line, no rewrites)
    - `Event.kind` is a free-form string (but `EventKind` enum lists conventions)
    - `Event.payload` is a plain dict — schema not enforced per-kind, deliberately,
      to keep the log flexible across phases. Schema tightening (discriminated
      unions) is a Phase D refactor if the dict-typed payload starts hurting.
    - Convenience `log_*` helpers wrap the most common event kinds so callers
      don't have to construct Event manually for every emit.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event kinds (vocabulary — not enforced, but conventional)
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    """Canonical event kinds. Treat this as a vocabulary, not a closed set.

    Adding a new kind is a non-breaking change. Renaming or removing one is
    a breaking change that requires a retro-compat shim in the reader.
    """

    # Mission lifecycle
    MISSION_START = "mission_start"
    MISSION_END = "mission_end"

    # Task lifecycle
    TASK_DISPATCHED = "task_dispatched"
    TASK_COMPLETE = "task_complete"
    TASK_FAILED = "task_failed"

    # Agent activity
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    ARTIFACT_WRITTEN = "artifact_written"

    # Multi-day infrastructure
    CHECKPOINT_CREATED = "checkpoint_created"
    STATUS_REPORT_EMITTED = "status_report_emitted"
    USER_MESSAGE_RECEIVED = "user_message_received"
    USER_MESSAGE_PROCESSED = "user_message_processed"

    # Quality gates
    VALIDATOR_VERDICT = "validator_verdict"
    SECOND_PASS_TRIGGERED = "second_pass_triggered"  # v3.1 — handoff completeness rule fired
    SECURITY_FINDING = "security_finding"

    # External / network (Phase C — soul.md §7)
    EXTERNAL_CONTENT_RECEIVED = "external_content_received"
    EGRESS_REQUEST = "egress_request"

    # Escalation & budget
    ESCALATION_TRIGGERED = "escalation_triggered"
    BUDGET_ALERT = "budget_alert"
    BUDGET_MODE_CHANGED = "budget_mode_changed"


# ---------------------------------------------------------------------------
# Event schema
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """A single event in the log.

    `payload` is a plain dict — schema flexibility wins over per-kind types here
    because the event stream evolves faster than the schema layer.
    """

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: str
    mission_id: str
    trace_id: str | None = None
    task_id: str | None = None
    actor: str | None = Field(default=None, description="Role name, e.g. 'coder_worker'")
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# EventLog
# ---------------------------------------------------------------------------


class EventLog:
    """Append-only jsonl event log.

    One file per mission. Use `ArtifactStore.event_log()` to get an instance
    bound to a mission's `events.jsonl`, or construct directly with a path
    for tests / non-mission use.

    Append safety: Python's text-mode file with `os.O_APPEND` is atomic for
    writes smaller than PIPE_BUF (typically 4096 bytes) on POSIX. Event lines
    are normally well under that. If you log >4KB events you may see interleaved
    writes under heavy concurrency — but Phase A doesn't have concurrent writers.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # -- Core append --------------------------------------------------------

    def append(self, event: Event) -> None:
        """Write one event as a single line of JSON, append-only."""
        line = event.model_dump_json() + "\n"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    # -- Convenience emitters ----------------------------------------------

    def log_mission_start(
        self,
        *,
        mission_id: str,
        goal: str,
        repo: str | None = None,
        expected_budget_usd: float | None = None,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.MISSION_START.value,
                mission_id=mission_id,
                trace_id=mission_id,
                payload={"goal": goal, "repo": repo, "expected_budget_usd": expected_budget_usd},
            )
        )

    def log_mission_end(
        self,
        *,
        mission_id: str,
        result: str,
        total_cost_usd: float,
        total_wall_clock_hours: float,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.MISSION_END.value,
                mission_id=mission_id,
                trace_id=mission_id,
                payload={
                    "result": result,
                    "total_cost_usd": total_cost_usd,
                    "total_wall_clock_hours": total_wall_clock_hours,
                },
            )
        )

    def log_task_dispatched(
        self,
        *,
        mission_id: str,
        task_id: str,
        owner: str,
        priority: str,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.TASK_DISPATCHED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor="orchestrator",
                payload={"owner": owner, "priority": priority},
            )
        )

    def log_task_complete(
        self,
        *,
        mission_id: str,
        task_id: str,
        actor: str,
        duration_sec: float,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.TASK_COMPLETE.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={"duration_sec": duration_sec},
            )
        )

    def log_task_failed(
        self,
        *,
        mission_id: str,
        task_id: str,
        actor: str,
        reason: str,
        will_retry: bool,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.TASK_FAILED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={"reason": reason, "will_retry": will_retry},
            )
        )

    def log_llm_call(
        self,
        *,
        mission_id: str,
        actor: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_sec: float,
        task_id: str | None = None,
        fallback_used: bool = False,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.LLM_CALL.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={
                    "model": model,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "cost_usd": cost_usd,
                    "latency_sec": latency_sec,
                    "fallback_used": fallback_used,
                },
            )
        )

    def log_tool_call(
        self,
        *,
        mission_id: str,
        actor: str,
        tool: str,
        args_summary: str,
        exit_code: int | None = None,
        duration_sec: float | None = None,
        task_id: str | None = None,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.TOOL_CALL.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={
                    "tool": tool,
                    "args_summary": args_summary,
                    "exit_code": exit_code,
                    "duration_sec": duration_sec,
                },
            )
        )

    def log_artifact_written(
        self,
        *,
        mission_id: str,
        actor: str,
        path: str,
        task_id: str | None = None,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.ARTIFACT_WRITTEN.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={"path": path},
            )
        )

    def log_checkpoint_created(
        self,
        *,
        mission_id: str,
        milestone_id: str,
        git_tag: str,
        snapshot_id: str,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.CHECKPOINT_CREATED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                actor="orchestrator",
                payload={
                    "milestone_id": milestone_id,
                    "git_tag": git_tag,
                    "snapshot_id": snapshot_id,
                },
            )
        )

    def log_status_report_emitted(
        self,
        *,
        mission_id: str,
        report_number: int,
        cost_usd: float,
        elapsed_hours: float,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.STATUS_REPORT_EMITTED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                actor="orchestrator",
                payload={
                    "report_number": report_number,
                    "cost_usd": cost_usd,
                    "elapsed_hours": elapsed_hours,
                },
            )
        )

    def log_validator_verdict(
        self,
        *,
        mission_id: str,
        task_id: str,
        validator: str,
        result: str,
        triggered_second_pass: bool = False,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.VALIDATOR_VERDICT.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=validator,
                payload={
                    "validator": validator,
                    "result": result,
                    "triggered_second_pass": triggered_second_pass,
                },
            )
        )

    def log_second_pass_triggered(
        self,
        *,
        mission_id: str,
        task_id: str,
        reason: str,
    ) -> None:
        """v3.1 — emitted when handoff completeness rule fires."""
        self.append(
            Event(
                kind=EventKind.SECOND_PASS_TRIGGERED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor="review_validator",
                payload={"reason": reason},
            )
        )

    def log_security_finding(
        self,
        *,
        mission_id: str,
        severity: str,
        category: str,
        description: str,
        task_id: str | None = None,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.SECURITY_FINDING.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor="security_worker",
                payload={
                    "severity": severity,
                    "category": category,
                    "description": description,
                },
            )
        )

    def log_escalation(
        self,
        *,
        mission_id: str,
        target: str,
        reason: str,
        task_id: str | None = None,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.ESCALATION_TRIGGERED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                payload={"target": target, "reason": reason},
            )
        )

    def log_budget_alert(
        self,
        *,
        mission_id: str,
        threshold_pct: float,
        cost_usd: float,
        budget_usd: float,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.BUDGET_ALERT.value,
                mission_id=mission_id,
                trace_id=mission_id,
                actor="orchestrator",
                payload={
                    "threshold_pct": threshold_pct,
                    "cost_usd": cost_usd,
                    "budget_usd": budget_usd,
                },
            )
        )

    def log_user_message_received(
        self,
        *,
        mission_id: str,
        message_path: str,
        urgent: bool,
    ) -> None:
        self.append(
            Event(
                kind=EventKind.USER_MESSAGE_RECEIVED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                actor="orchestrator",
                payload={"message_path": message_path, "urgent": urgent},
            )
        )

    def log_external_content_received(
        self,
        *,
        mission_id: str,
        actor: str,
        original_url: str,
        final_url: str,
        content_type: str,
        sanitization_actions: list[str],
        task_id: str | None = None,
    ) -> None:
        """Soul.md §7 — record that the sanitizer accepted external content."""
        self.append(
            Event(
                kind=EventKind.EXTERNAL_CONTENT_RECEIVED.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={
                    "original_url": original_url,
                    "final_url": final_url,
                    "content_type": content_type,
                    "sanitization_actions": sanitization_actions,
                },
            )
        )

    def log_egress_request(
        self,
        *,
        mission_id: str,
        actor: str,
        url: str,
        domain: str,
        status_code: int | None = None,
        bytes_received: int | None = None,
        blocked_reason: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Soul.md §7.3 — record every outbound request (allowed or blocked)."""
        self.append(
            Event(
                kind=EventKind.EGRESS_REQUEST.value,
                mission_id=mission_id,
                trace_id=mission_id,
                task_id=task_id,
                actor=actor,
                payload={
                    "url": url,
                    "domain": domain,
                    "status_code": status_code,
                    "bytes_received": bytes_received,
                    "blocked_reason": blocked_reason,
                },
            )
        )

    # -- Read / iterate -----------------------------------------------------

    def iter_events(self) -> Iterator[Event]:
        """Stream events from disk in order."""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    yield Event.model_validate_json(line)
                except Exception as e:
                    logger.warning("Skipping malformed event log line: %r (%s)", line[:200], e)

    def filter_kind(self, kind: str | EventKind) -> Iterator[Event]:
        kind_str = kind.value if isinstance(kind, EventKind) else kind
        return (e for e in self.iter_events() if e.kind == kind_str)

    def last_event(self) -> Event | None:
        last: Event | None = None
        for e in self.iter_events():
            last = e
        return last

    # -- Aggregations -------------------------------------------------------

    def total_cost_usd(self) -> float:
        """Sum cost_usd across all LLM_CALL events."""
        return sum(
            float(e.payload.get("cost_usd", 0.0)) for e in self.filter_kind(EventKind.LLM_CALL)
        )

    def total_tokens(self) -> tuple[int, int]:
        """Sum (tokens_in, tokens_out) across all LLM_CALL events."""
        ti = 0
        to = 0
        for e in self.filter_kind(EventKind.LLM_CALL):
            ti += int(e.payload.get("tokens_in", 0))
            to += int(e.payload.get("tokens_out", 0))
        return ti, to

    def cost_by_actor(self) -> dict[str, float]:
        """Cost per role/actor — drives 'who burned the budget' analysis."""
        result: dict[str, float] = {}
        for e in self.filter_kind(EventKind.LLM_CALL):
            actor = e.actor or "unknown"
            result[actor] = result.get(actor, 0.0) + float(e.payload.get("cost_usd", 0.0))
        return result

    def task_outcomes(self) -> dict[str, str]:
        """Latest outcome per task_id: 'complete' | 'failed' | 'dispatched' | ..."""
        result: dict[str, str] = {}
        for e in self.iter_events():
            if e.task_id is None:
                continue
            if e.kind == EventKind.TASK_DISPATCHED.value:
                result[e.task_id] = "dispatched"
            elif e.kind == EventKind.TASK_COMPLETE.value:
                result[e.task_id] = "complete"
            elif e.kind == EventKind.TASK_FAILED.value:
                result[e.task_id] = "failed"
        return result
