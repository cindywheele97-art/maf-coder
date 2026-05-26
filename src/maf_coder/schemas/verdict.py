"""Validator verdicts: Review, Behavior, Security.

Verdicts are the *only* gate between Coder output and PR creation.
Each one is independently signed by an agent and stored as JSON for audit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .common import Severity, VerdictResult


class AssertionResult(BaseModel):
    """Per-assertion verification outcome."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    result: VerdictResult
    detail: str | None = None


class CargoGateResults(BaseModel):
    """Outcome of the cargo gate set ReviewValidator runs."""

    model_config = ConfigDict(extra="forbid")

    build: bool
    test: bool
    clippy: bool
    fmt: bool
    nextest: bool | None = None  # None if not available
    doc_test: bool | None = None


class ReviewVerdict(BaseModel):
    """ReviewValidator output (soul.md §3.5).

    Stored at: missions/<id>/verdicts/<task_id>.review.json
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    task_id: str
    result: VerdictResult
    precise_reason: str = Field(
        description="If FAIL: file:line + assertion details. Vague 'tests failed' not acceptable."
    )
    next_action_recommendation: str = Field(
        description="E.g. 'fix clippy warning at src/foo.rs:42' or 'send to behavior validator'"
    )
    cargo_gate_results: CargoGateResults
    assertion_results: list[AssertionResult] = Field(default_factory=list)
    triggered_second_pass: bool = Field(
        default=False,
        description="v3.1 — True iff handoff completeness rule triggered adversarial second pass",
    )
    adversarial_findings: list[str] = Field(
        default_factory=list,
        description="Issues raised by adversarial sub-agent that Coder did not flag in handoff",
    )
    hardcoded_test_warnings: list[str] = Field(
        default_factory=list,
        description="v3.1 — Tests where adversarial sub-agent suspects intent is not verified",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BehaviorObservation(BaseModel):
    """One probe observation tied to an assertion."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    observed: str
    expected: str
    matched: bool


class BehaviorVerdict(BaseModel):
    """BehaviorValidator output (soul.md §3.6).

    Stored at: missions/<id>/verdicts/<task_id>.behavior.json
    Only runs AFTER ReviewVerdict is PASS.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    task_id: str
    result: VerdictResult
    probe_strategy: str
    observations: list[BehaviorObservation] = Field(default_factory=list)
    evidence_path: str = Field(description="Relative path to behavior_evidence/ directory")
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SecurityFinding(BaseModel):
    """One security audit finding."""

    model_config = ConfigDict(extra="forbid")

    severity: Severity
    category: str = Field(
        description="'audit' | 'deny' | 'geiger' | 'secret' | 'unsafe' | 'license'"
    )
    description: str
    location: str | None = Field(default=None, description="file:line or crate name")
    suggestion: str | None = None


class SecurityVerdict(BaseModel):
    """Security Worker output (soul.md §3.4).

    Stored at: missions/<id>/verdicts/<task_id>.security.json
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    task_id: str
    findings: list[SecurityFinding]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL.value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.HIGH.value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def blocks_pr(self) -> bool:
        """Critical → block PR + escalate to Human Gate."""
        return self.critical_count > 0
