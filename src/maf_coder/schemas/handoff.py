"""Worker handoff document (soul.md §11.3 + v3.1 完备性规则).

The handoff is THE artifact that lets multi-day missions survive context loss.
A worker that doesn't produce a structured handoff has not finished its task.

v3.1 addition: a "too clean" handoff (no incomplete/issues/deviations) is
automatically suspect and triggers ReviewValidator second-pass — see §11.3.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field


class CommandRun(BaseModel):
    """Single command execution record."""

    model_config = ConfigDict(extra="forbid")

    command: str
    exit_code: int
    summary: str = Field(description="Short result summary, e.g. '47 tests passed' or 'clippy clean'")


class DependencyChange(BaseModel):
    """Cargo.toml / Cargo.lock change record."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Crate name")
    action: str = Field(description="'added' | 'removed' | 'upgraded' | 'downgraded'")
    detail: str = Field(description="Version diff, e.g. '1.0.218 -> 1.0.220'")
    rationale: str = Field(description="Why this change was necessary")


class ContractCoverage(BaseModel):
    """Per-assertion coverage record. References validation_contract.yaml assertions."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(description="E.g. 'f1.a1'")
    covered: bool
    location: str | None = Field(
        default=None,
        description="Where this assertion is verified, e.g. 'tests/api_test.rs::test_health'",
    )
    reason_if_uncovered: str | None = None


class UnsafeUsage(BaseModel):
    """Record of any new unsafe block introduced."""

    model_config = ConfigDict(extra="forbid")

    location: str = Field(description="file:line of the unsafe block")
    rationale: str = Field(description="Why unsafe is necessary here")
    encapsulation: str = Field(description="How the unsafe surface is contained")


class Handoff(BaseModel):
    """Mandatory structured handoff.

    Enforces soul.md §11.3 schema. The v3.1 完备性规则 (completeness rule) is
    not enforced as a validation error — a Coder may legitimately have nothing
    to flag — but `triggers_second_pass` exposes the condition so ReviewValidator
    can run a sub-agent skeptic pass.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    task_id: str

    # Core deliverable narrative
    completed: list[str] = Field(description="Concrete items completed by this task")
    incomplete: list[str] = Field(default_factory=list)
    commands_run: list[CommandRun] = Field(default_factory=list)
    issues_discovered: list[str] = Field(default_factory=list)
    deviations_from_plan: list[str] = Field(default_factory=list)

    # Contract & dependency tracking
    contract_coverage: list[ContractCoverage] = Field(default_factory=list)
    dependency_changes: list[DependencyChange] = Field(default_factory=list)

    # Safety
    unsafe_usage: list[UnsafeUsage] = Field(default_factory=list)

    # Next step
    next_recommended_action: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def triggers_second_pass(self) -> bool:
        """v3.1 完备性规则.

        Returns True iff `incomplete`, `issues_discovered`, and `deviations_from_plan`
        are all empty. In that case the ReviewValidator must run a sub-agent
        skeptic pass: load the patch + tests + contract independently and look for
        what the Coder might have missed.

        The skeptic pass is documented in soul.md §11.3. Empty-handoff is not an
        error (the work may legitimately be that clean), but it is suspicious enough
        to warrant adversarial verification.
        """
        return not (
            self.incomplete or self.issues_discovered or self.deviations_from_plan
        )
