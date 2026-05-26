"""Inter-agent message envelope (soul.md §11.1).

Every message between agents — Orchestrator -> Worker, Worker -> Validator, etc. —
must conform to this schema. Free-form chat between agents is forbidden by design.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field

from .common import Intent, RiskLevel, Role


class Budgets(BaseModel):
    """Per-message budget envelope.

    Enforced by the scheduler before dispatch. Exceeding any → escalation.
    """

    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(gt=0, description="Hard token cap for this task")
    max_runtime_sec: int = Field(gt=0, description="Wall-clock cap; exceeded → fail-fast")
    max_retries: int = Field(ge=0, default=2)


class RiskFlag(BaseModel):
    """Single risk marker attached to a message."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="e.g. 'unsafe_introduced', 'new_dependency', 'breaking_api'")
    level: RiskLevel
    description: str


class Message(BaseModel):
    """Inter-agent message — enforces §11.1 schema from soul.md.

    Fields are deliberately verbose: the message *is* the protocol.
    Truncating or skipping fields is forbidden by design — every agent
    that emits a message must fill all required fields.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    task_id: str = Field(description="Unique task identifier")
    parent_task_id: str | None = Field(default=None, description="Parent milestone task ID")
    trace_id: str = Field(description="Mission-level tracking ID (mission_id)")
    sender: Role
    recipient: Role
    intent: Intent
    summary: str = Field(
        max_length=2000,
        description="Short summary (<500 字 / ~2000 chars). Must preserve goal/constraints/open-issues/next-step.",
    )
    artifact_refs: list[str] = Field(
        default_factory=list,
        description="Paths/URIs to referenced artifacts. Important facts cite artifacts, not narrate.",
    )
    output_contract: str = Field(description="Expected output description")
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    budgets: Budgets
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
