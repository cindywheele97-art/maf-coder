"""Task definition (soul.md §16 task template).

Tasks are nodes in the mission DAG. Each task is owned by one role and has a
permission boundary that the sandbox enforces at runtime.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import NetworkPolicy, RiskLevel, Role


class Permission(BaseModel):
    """Per-task permission boundary. Enforced by sandbox + PreToolUse hooks."""

    model_config = ConfigDict(extra="forbid")

    allowed_paths: list[str] = Field(
        default_factory=list,
        description="Absolute paths or globs Worker may read/write. Empty = sandbox-default (workspace only).",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Allow-list of tool names. E.g. ['cargo', 'git', 'rg']",
    )
    network_policy: NetworkPolicy = NetworkPolicy.NONE
    human_approval_required: bool = Field(
        default=False,
        description="If True, task waits for Human Gate before executing.",
    )


class TaskBudget(BaseModel):
    """Per-task budget. Stricter than message Budgets; sum across retries."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(gt=0, default=100_000)
    max_runtime_sec: int = Field(gt=0, default=600)
    cost_ceiling_usd: float | None = Field(
        default=None,
        description="Optional per-task hard cost cap. None = inherits from mission budget.",
    )


class FailureHandling(BaseModel):
    """What to do when this task fails."""

    model_config = ConfigDict(extra="forbid")

    retry_budget: int = Field(ge=0, default=1)
    escalation_target: Role = Role.ORCHESTRATOR
    rollback_checkpoint: str | None = Field(
        default=None,
        description="Checkpoint to roll back to on terminal failure. E.g. 'm2'.",
    )


class Task(BaseModel):
    """A single task in the DAG.

    Tasks are immutable once dispatched. Modifications produce new tasks
    with new IDs to preserve audit trail.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    task_id: str
    parent_milestone: str
    owner: Role
    priority: RiskLevel = RiskLevel.MEDIUM
    risk_level: RiskLevel = RiskLevel.LOW

    goal: str = Field(description="One-line objective")
    background: str = Field(description="Why this task exists + scope context")

    acceptance_criteria: list[str] = Field(
        description="Contract assertion IDs covered. E.g. ['f1.a1', 'f1.a2']",
    )

    input_artifacts: list[str] = Field(
        default_factory=list,
        description="URIs to required input artifacts (spec://, code://, research://, etc.)",
    )
    required_outputs: list[str] = Field(
        description="What this task must produce. E.g. ['patch.diff', 'handoff.md', 'test_report.json']",
    )

    permission: Permission
    budget: TaskBudget = Field(default_factory=TaskBudget)
    failure_handling: FailureHandling = Field(default_factory=FailureHandling)

    depends_on: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this one can start",
    )
