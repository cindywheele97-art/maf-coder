"""Runtime lifecycle artifacts: StatusReport, Checkpoint, MissionState.

These three together make multi-day missions resumable and observable:
- StatusReport: 4-8h sync to user (soul.md §5.2)
- Checkpoint: per-milestone resume point (soul.md §5.3)
- MissionState: ever-updating runtime state of the mission
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class BudgetStatus(BaseModel):
    """Current budget snapshot embedded in StatusReport."""

    model_config = ConfigDict(extra="forbid")

    tokens_used: int
    cost_usd: float
    alert_threshold_usd: float
    projected_total_usd: float = Field(
        description="Linear extrapolation from current burn rate to mission end"
    )
    wall_clock_vs_estimate_pct: float = Field(
        description="100 = on plan, 110 = 10% over, 50 = halfway through allotted time"
    )


class MilestoneStatus(BaseModel):
    """One milestone's state in the StatusReport."""

    model_config = ConfigDict(extra="forbid")

    milestone_id: str
    state: str = Field(description="'complete' | 'in_progress' | 'pending' | 'blocked'")


class StatusReport(BaseModel):
    """Periodic 4-8h sync to user (soul.md §5.2).

    Stored at: missions/<id>/status_reports/status_<n>.md (rendered) +
              missions/<id>/status_reports/status_<n>.json (machine-readable)

    Does NOT block execution — Orchestrator emits then continues working.
    User can drop notes into user_messages/ inbox; Orchestrator polls at
    each milestone boundary.
    """

    model_config = ConfigDict(extra="forbid")

    report_number: int = Field(ge=1)
    mission_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    mission_started_at: datetime
    elapsed_hours: float
    milestones: list[MilestoneStatus]
    current_activity: str = Field(
        description="E.g. 'Coder working on feature f2: adding /health endpoint'"
    )
    budget_status: BudgetStatus
    risks_discovered_since_last: list[str] = Field(default_factory=list)
    decisions_awaiting_user: list[str] = Field(default_factory=list)
    next_milestone_eta_hours: float | None = None


class Checkpoint(BaseModel):
    """Resume point after each milestone (soul.md §5.3).

    Triple-store:
    1. git tag in worktree (e.g. 'mission/<id>/m2')
    2. Docker container commit (sandbox snapshot)
    3. Artifact archive copy

    Resume can target any checkpoint: `maf-coder resume <mission_id> --from m2`
    """

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    milestone_id: str
    git_tag: str = Field(description="E.g. 'mission/<id>/m2'")
    sandbox_snapshot_id: str = Field(
        description="Docker image/container ID for the sandbox state at this checkpoint"
    )
    artifact_archive_path: str = Field(description="Relative path to archived artifacts directory")
    cumulative_cost_usd: float
    cumulative_wall_clock_hours: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MissionState(BaseModel):
    """Ever-updating runtime state of a mission.

    Persisted to: missions/<mission_id>/mission_state.json
    Updated on every meaningful event (task complete, checkpoint, budget tick).

    `coder_provider_in_use` is the critical field for §4 Droid Whispering enforcement:
    ReviewValidator and adversarial_subagent MUST use a different provider than Coder.
    """

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    started_at: datetime
    current_milestone: str | None = None
    completed_milestones: list[str] = Field(default_factory=list)
    cumulative_cost_usd: float = 0.0
    cumulative_wall_clock_hours: float = 0.0
    cumulative_tokens: int = 0
    last_status_report_at: datetime | None = None
    last_checkpoint_at: datetime | None = None
    coder_provider_in_use: str | None = Field(
        default=None,
        description="Provider used by Coder this mission. Drives Validator异-provider enforcement.",
    )
    last_user_message_processed_at: datetime | None = Field(
        default=None,
        description="Tracks Orchestrator polling of user_messages/ inbox.",
    )
