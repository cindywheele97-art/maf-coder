"""Validation Contract (soul.md §11.4).

The contract is THE single most important artifact in the framework:
- Locked at planning phase (before any code is written)
- Coder may not modify
- Every assertion has a verification target that ReviewValidator / BehaviorValidator checks
- Stored as YAML at missions/<id>/validation_contract.yaml

This file is the empirical core of "soul.md §2 总体工作原则" — "验证合约未签发，
实现阶段不得启动".
"""
from __future__ import annotations

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field

from .common import VerificationMethod


class Assertion(BaseModel):
    """A single verifiable claim.

    Statements should be testable without knowing implementation details.
    Bad: "Uses tokio::spawn for the handler"  (implementation-coupled)
    Good: "GET /health returns 200 with status field in body"  (behavior-only)
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="E.g. 'f1.a1'")
    statement: str = Field(description="What must be true, in implementation-agnostic terms")
    verification_method: VerificationMethod
    verification_target: str = Field(
        description="Where this is verified, e.g. 'tests/foo.rs::test_bar' or "
        "'behavior_probe::backend_service_health_probe::endpoint_health'"
    )


class Feature(BaseModel):
    """A group of related assertions covering one user-facing feature."""

    model_config = ConfigDict(extra="forbid")

    feature_id: str = Field(description="E.g. 'f1'")
    description: str
    assertions: list[Assertion] = Field(min_length=1)


class ValidationContract(BaseModel):
    """The locked acceptance contract for a mission.

    Stored at: missions/<mission_id>/validation_contract.yaml
    Mutated only by: Orchestrator, before any Coder/Worker task is dispatched.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    mission_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "orchestrator"
    locked: bool = Field(
        default=True,
        description="Once True, contract is immutable. Required to be True before coding starts.",
    )
    project_profile_ref: str = Field(
        default="project_profile.yaml",
        description="Relative path to the ProjectProfile this contract was drafted against",
    )
    features: list[Feature] = Field(min_length=1)
    non_goals: list[str] = Field(
        default_factory=list,
        description="Explicit out-of-scope items, to prevent scope creep mid-mission",
    )
    risk_acknowledgements: list[str] = Field(
        default_factory=list,
        description="Known risks accepted at planning time. E.g. 'axum/tokio compat may break'",
    )
