"""ArtifactStore — the only sanctioned read/write path to a mission's artifacts.

Why this exists:
    soul.md §2 hard rule: "工件优先于口头说明". If a Worker can't write a structured
    artifact, the work didn't happen. If a Validator can't read structured artifacts
    instead of the Worker's narration, the verification didn't happen.

    Without ArtifactStore, every agent invents its own filesystem layout and the
    multi-day mission breaks the first time you try to resume.

Responsibilities:

1. Enforce the mission directory layout from soul.md §11.2.
2. Atomic writes — partially-written files are never visible to other agents.
3. Type-safe load/save for every Pydantic schema (no dict-juggling at call sites).
4. Write-once enforcement for `validation_contract.yaml` (the soul.md §2 lock).
5. Path-traversal protection — relpath escape is rejected, not silently routed
   outside the mission directory.

Non-responsibilities (kept elsewhere):

- Concurrency control: workers are scheduled by Orchestrator with write-serial /
  read-parallel guarantees (soul.md §2). The store does not lock files.
- Event tracking: see EventLog. ArtifactStore writes raw files; EventLog records
  that a write happened.
- Memory / retrieval: see future memory/ package (Phase F). ArtifactStore deals
  with the current mission only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel

from ..schemas import (
    BehaviorVerdict,
    Checkpoint,
    Handoff,
    MissionState,
    ProjectProfile,
    ReviewVerdict,
    SecurityVerdict,
    StatusReport,
    ValidationContract,
)

if TYPE_CHECKING:
    from .event_log import EventLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ArtifactStoreError(Exception):
    """Base for all artifact store errors."""


class PathEscapeError(ArtifactStoreError):
    """Raised when a relpath would resolve outside the mission directory."""


class ContractAlreadyLockedError(ArtifactStoreError):
    """Raised on attempt to overwrite a locked validation_contract.yaml.

    The soul.md §2 hard rule: '验证合约未签发，实现阶段不得启动'. The flip side
    is: once签发, 不得修改. Coder Worker errors that try to mutate the contract
    must surface, not be silently overwritten.
    """


# ---------------------------------------------------------------------------
# Canonical paths
# ---------------------------------------------------------------------------

# Relative paths within mission_dir for every well-known artifact.
# Keeping these as constants prevents typo-driven layout drift across the codebase.

_PROJECT_PROFILE = "project_profile.yaml"
_PLAN = "plan.md"
_TASKS = "tasks.yaml"
_VALIDATION_CONTRACT = "validation_contract.yaml"
_RISK_REGISTER = "risk_register.md"
_BUDGET = "budget.yaml"
_MISSION_STATE = "mission_state.json"
_EVENTS_LOG = "events.jsonl"
_EGRESS_LOG = "egress.jsonl"
_FINAL_ANSWER = "final_answer.md"
_MISSION_RETRO = "mission_retro.md"

# Subdirectories — created lazily on first write
_DIRS = {
    "research_notes": "research_notes",
    "code_map": "code_map",
    "handoff": "handoff",
    "patches": "patches",
    "reports": "reports",
    "dependency_diff": "dependency_diff",
    "security_audit": "security_audit",
    "security_notes": "security_notes",
    "verdicts": "verdicts",
    "review_notes": "review_notes",
    "behavior_trace": "behavior_trace",
    "behavior_evidence": "behavior_evidence",
    "adversarial_tests": "adversarial_tests",
    "status_reports": "status_reports",
    "checkpoints": "checkpoints",
    "user_messages": "user_messages",
    "processed_messages": "processed_messages",
}


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------


_COMPUTED_FIELDS_CACHE: dict[type, set[str]] = {}


def _computed_field_names(cls: type) -> set[str]:
    """Return the computed-field names for a Pydantic v2 model.

    Computed fields are derived from other fields and re-derive on load, so we
    must exclude them from the serialized payload — re-loading would otherwise
    fail under `extra="forbid"` because the computed key would look like an
    unknown input. Cached per class to avoid repeated introspection.
    """
    if cls not in _COMPUTED_FIELDS_CACHE:
        names: set[str] = set()
        computed = getattr(cls, "model_computed_fields", None)
        if computed:
            names = set(computed.keys())
        _COMPUTED_FIELDS_CACHE[cls] = names
    return _COMPUTED_FIELDS_CACHE[cls]


def _atomic_write(target: Path, content: bytes) -> None:
    """Write `content` to `target` atomically.

    Strategy: write to temp file in same directory, fsync, then os.replace.
    os.replace is atomic on both POSIX and Windows. Same-directory temp ensures
    the rename doesn't cross filesystems (which would make it non-atomic).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of orphan tmp file
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


class ArtifactStore:
    """File-backed artifact store for a single mission.

    Construct one per mission. The store binds to `<missions_root>/<mission_id>/`
    and refuses any access outside that directory.

    Example:

        store = ArtifactStore("/workspace/missions", "m-2026-05-22-001")
        store.save_validation_contract(contract)
        loaded = store.load_validation_contract()
    """

    def __init__(
        self,
        missions_root: str | os.PathLike[str],
        mission_id: str,
    ) -> None:
        if not mission_id or "/" in mission_id or ".." in mission_id:
            raise ValueError(f"Invalid mission_id: {mission_id!r}")
        self.missions_root = Path(missions_root).resolve()
        self.mission_dir = (self.missions_root / mission_id).resolve()
        # Ensure mission_dir is actually under missions_root (defense in depth)
        if (
            self.missions_root not in self.mission_dir.parents
            and self.mission_dir != self.missions_root
        ):
            raise PathEscapeError(
                f"mission_dir {self.mission_dir} not under missions_root {self.missions_root}"
            )
        self.mission_id = mission_id
        self.mission_dir.mkdir(parents=True, exist_ok=True)

    # -- Path safety -------------------------------------------------------

    def _resolve(self, relpath: str) -> Path:
        """Resolve relpath inside mission_dir. Reject any escape attempt."""
        candidate = (self.mission_dir / relpath).resolve()
        # parents excludes the path itself, so include direct equality check
        if self.mission_dir not in candidate.parents and candidate != self.mission_dir:
            raise PathEscapeError(f"relpath {relpath!r} resolves outside mission dir: {candidate}")
        return candidate

    # -- Generic read/write ------------------------------------------------

    def exists(self, relpath: str) -> bool:
        return self._resolve(relpath).exists()

    def write_text(self, relpath: str, content: str) -> Path:
        target = self._resolve(relpath)
        _atomic_write(target, content.encode("utf-8"))
        return target

    def read_text(self, relpath: str) -> str:
        return self._resolve(relpath).read_text(encoding="utf-8")

    def write_json(self, relpath: str, data: dict[str, Any] | list[Any] | BaseModel) -> Path:
        if isinstance(data, BaseModel):
            payload = json.dumps(
                data.model_dump(mode="json", exclude=_computed_field_names(type(data))),
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        else:
            payload = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        return self.write_text(relpath, payload + "\n")

    def read_json(self, relpath: str) -> dict[str, Any] | list[Any]:
        result: dict[str, Any] | list[Any] = json.loads(self.read_text(relpath))
        return result

    def write_yaml(self, relpath: str, data: dict[str, Any] | BaseModel) -> Path:
        if isinstance(data, BaseModel):
            payload_dict = data.model_dump(mode="json", exclude=_computed_field_names(type(data)))
        else:
            payload_dict = data
        return self.write_text(
            relpath,
            yaml.safe_dump(
                payload_dict, sort_keys=False, allow_unicode=True, default_flow_style=False
            ),
        )

    def read_yaml(self, relpath: str) -> dict[str, Any]:
        return yaml.safe_load(self.read_text(relpath)) or {}

    def list_dir(self, relpath: str) -> list[Path]:
        """List files under `relpath` (relative to mission_dir). Empty if missing."""
        target = self._resolve(relpath)
        if not target.is_dir():
            return []
        return sorted(target.iterdir())

    # -- Typed save/load: project profile ---------------------------------

    def save_project_profile(self, profile: ProjectProfile) -> Path:
        return self.write_yaml(_PROJECT_PROFILE, profile)

    def load_project_profile(self) -> ProjectProfile:
        return ProjectProfile.model_validate(self.read_yaml(_PROJECT_PROFILE))

    # -- Typed save/load: validation contract (WRITE-ONCE) ----------------

    def save_validation_contract(
        self,
        contract: ValidationContract,
        *,
        allow_overwrite: bool = False,
    ) -> Path:
        """Save the validation contract.

        Default behavior: write-once. Once `validation_contract.yaml` exists,
        attempting to write again raises ContractAlreadyLockedError. This
        enforces soul.md §2 ("Validation contract locked at planning phase").

        `allow_overwrite=True` exists for two legitimate cases:
        - Tests that want to set up fixtures
        - Orchestrator replanning AFTER a Human Gate explicitly authorized contract revision
        """
        if not allow_overwrite and self.exists(_VALIDATION_CONTRACT):
            raise ContractAlreadyLockedError(
                f"Mission {self.mission_id}: validation_contract.yaml already exists. "
                "Contracts are write-once. Pass allow_overwrite=True only after Human Gate approval."
            )
        return self.write_yaml(_VALIDATION_CONTRACT, contract)

    def load_validation_contract(self) -> ValidationContract:
        return ValidationContract.model_validate(self.read_yaml(_VALIDATION_CONTRACT))

    # -- Typed save/load: handoff -----------------------------------------

    def save_handoff(self, task_id: str, handoff: Handoff) -> Path:
        return self.write_json(f"{_DIRS['handoff']}/{task_id}.json", handoff)

    def load_handoff(self, task_id: str) -> Handoff:
        return Handoff.model_validate(self.read_json(f"{_DIRS['handoff']}/{task_id}.json"))

    # -- Typed save/load: verdicts ----------------------------------------

    def save_review_verdict(self, task_id: str, verdict: ReviewVerdict) -> Path:
        return self.write_json(f"{_DIRS['verdicts']}/{task_id}.review.json", verdict)

    def load_review_verdict(self, task_id: str) -> ReviewVerdict:
        return ReviewVerdict.model_validate(
            self.read_json(f"{_DIRS['verdicts']}/{task_id}.review.json")
        )

    def save_behavior_verdict(self, task_id: str, verdict: BehaviorVerdict) -> Path:
        return self.write_json(f"{_DIRS['verdicts']}/{task_id}.behavior.json", verdict)

    def load_behavior_verdict(self, task_id: str) -> BehaviorVerdict:
        return BehaviorVerdict.model_validate(
            self.read_json(f"{_DIRS['verdicts']}/{task_id}.behavior.json")
        )

    def save_security_verdict(self, task_id: str, verdict: SecurityVerdict) -> Path:
        return self.write_json(f"{_DIRS['verdicts']}/{task_id}.security.json", verdict)

    def load_security_verdict(self, task_id: str) -> SecurityVerdict:
        return SecurityVerdict.model_validate(
            self.read_json(f"{_DIRS['verdicts']}/{task_id}.security.json")
        )

    # -- Typed save/load: mission lifecycle -------------------------------

    def save_mission_state(self, state: MissionState) -> Path:
        return self.write_json(_MISSION_STATE, state)

    def load_mission_state(self) -> MissionState:
        return MissionState.model_validate(self.read_json(_MISSION_STATE))

    def save_status_report(self, report: StatusReport) -> tuple[Path, Path]:
        """Save both the machine-readable .json and the rendered .md."""
        n = report.report_number
        json_path = self.write_json(f"{_DIRS['status_reports']}/status_{n:04d}.json", report)
        md_path = self.write_text(
            f"{_DIRS['status_reports']}/status_{n:04d}.md",
            _render_status_report_markdown(report),
        )
        return json_path, md_path

    def save_checkpoint(self, checkpoint: Checkpoint) -> Path:
        m = checkpoint.milestone_id
        return self.write_json(f"{_DIRS['checkpoints']}/{m}/checkpoint.json", checkpoint)

    def load_checkpoint(self, milestone_id: str) -> Checkpoint:
        return Checkpoint.model_validate(
            self.read_json(f"{_DIRS['checkpoints']}/{milestone_id}/checkpoint.json")
        )

    # -- Convenience: bound EventLog --------------------------------------

    def event_log(self) -> EventLog:
        """Return an EventLog bound to this mission's events.jsonl.

        Lazy import to avoid circular dependency with event_log module.
        """
        from .event_log import EventLog

        return EventLog(self._resolve(_EVENTS_LOG))

    def egress_log_path(self) -> Path:
        """Path to the egress log. Caller owns the EgressLog wrapper if needed."""
        return self._resolve(_EGRESS_LOG)

    # -- Convenience: research / patches list iterators -------------------

    def list_handoffs(self) -> list[str]:
        """Return task_ids that have a handoff on disk."""
        return [p.stem for p in self.list_dir(_DIRS["handoff"]) if p.suffix == ".json"]

    def list_research_notes(self) -> list[str]:
        """Return topic names (filename stem) for research notes."""
        return [p.stem for p in self.list_dir(_DIRS["research_notes"]) if p.suffix == ".md"]

    # -- Discovery / dump --------------------------------------------------

    def iter_all_files(self) -> Iterable[Path]:
        """Recursive iterator over every file under mission_dir.

        Useful for retro / archival / debug. Order not guaranteed.
        """
        return (p for p in self.mission_dir.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Markdown rendering for StatusReport (kept here so save_status_report is
# one-call; if rendering grows, lift to its own module)
# ---------------------------------------------------------------------------


def _render_status_report_markdown(report: StatusReport) -> str:
    lines: list[str] = []
    lines.append(f"# Status Report #{report.report_number} — {report.mission_id}")
    lines.append(f"_Created: {report.created_at.isoformat()}_")
    lines.append("")
    lines.append("## Mission Progress")
    lines.append(f"- Started: {report.mission_started_at.isoformat()}")
    lines.append(f"- Elapsed: {report.elapsed_hours:.1f}h")
    completed = [m for m in report.milestones if m.state == "complete"]
    in_progress = [m for m in report.milestones if m.state == "in_progress"]
    pending = [m for m in report.milestones if m.state == "pending"]
    lines.append(
        f"- Milestones: {len(completed)} complete · {len(in_progress)} in progress · {len(pending)} pending"
    )
    lines.append(f"- Current activity: {report.current_activity}")
    lines.append("")
    lines.append("## Budget Status")
    bs = report.budget_status
    lines.append(f"- Tokens used: {bs.tokens_used:,}")
    lines.append(f"- Cost: ${bs.cost_usd:.2f}")
    lines.append(f"- Alert threshold: ${bs.alert_threshold_usd:.2f}")
    lines.append(f"- Projected total: ${bs.projected_total_usd:.2f}")
    lines.append(f"- Wall-clock vs estimate: {bs.wall_clock_vs_estimate_pct:.0f}%")
    lines.append("")
    if report.risks_discovered_since_last:
        lines.append("## Risks Discovered Since Last Report")
        for r in report.risks_discovered_since_last:
            lines.append(f"- {r}")
        lines.append("")
    if report.decisions_awaiting_user:
        lines.append("## Decisions Awaiting Your Input")
        for d in report.decisions_awaiting_user:
            lines.append(f"- {d}")
        lines.append("")
    else:
        lines.append("## Decisions Awaiting Your Input")
        lines.append("- None")
        lines.append("")
    if report.next_milestone_eta_hours is not None:
        lines.append(f"## Next Milestone ETA: ~{report.next_milestone_eta_hours:.1f}h remaining")
        lines.append("")
    lines.append("## How to Steer")
    lines.append("- Drop a `.md` file into `user_messages/` to inject instructions")
    lines.append("- Use `!urgent` prefix in the filename for immediate-check priority")
    lines.append("")
    return "\n".join(lines)
