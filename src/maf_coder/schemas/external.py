"""External network audit schema (soul.md §7.3 — egress log).

`SanitizedContent` is intentionally kept as a frozen dataclass in
`agents.results` (it is a tool return type, not a persisted artifact).
The Pydantic model here is only the persisted egress record.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class EgressRecord(BaseModel):
    """One row of the per-mission egress log (soul.md §7.3).

    Records every outbound HTTP request the Research Worker (or any tool)
    makes, so we can audit network usage after the fact and enforce
    per-domain rate / budget caps.
    """

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    url: str
    domain: str
    method: str = "GET"
    status_code: int | None = None
    bytes_received: int | None = None
    sanitization_actions: list[str] = Field(default_factory=list)
    blocked_reason: str | None = Field(
        default=None,
        description="If non-null, the request was denied (policy / budget / blacklist).",
    )
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
