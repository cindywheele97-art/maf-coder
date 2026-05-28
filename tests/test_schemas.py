"""Sanity tests for the schema layer.

Phase A 退出门槛: `pytest tests/test_schemas.py` 全过.
"""

from __future__ import annotations

import pytest
import yaml as pyyaml
from pydantic import ValidationError

from maf_coder.schemas import (
    Assertion,
    Budgets,
    CommandRun,
    EgressRecord,
    Feature,
    Handoff,
    Intent,
    Message,
    ReviewVerdict,
    Role,
    ValidationContract,
    VerdictResult,
    VerificationMethod,
)
from maf_coder.schemas.verdict import CargoGateResults

# ============================================================================
# Message
# ============================================================================


class TestMessage:
    def test_minimal_valid(self) -> None:
        msg = Message(
            task_id="t1",
            trace_id="m1",
            sender=Role.ORCHESTRATOR,
            recipient=Role.CODER_WORKER,
            intent=Intent.IMPLEMENT,
            summary="Implement health endpoint",
            output_contract="patch.diff + handoff.md",
            budgets=Budgets(max_tokens=32000, max_runtime_sec=600),
        )
        assert msg.task_id == "t1"
        assert msg.budgets.max_retries == 2  # default

    def test_summary_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(
                task_id="t1",
                trace_id="m1",
                sender=Role.ORCHESTRATOR,
                recipient=Role.CODER_WORKER,
                intent=Intent.IMPLEMENT,
                summary="x" * 3000,
                output_contract="x",
                budgets=Budgets(max_tokens=1000, max_runtime_sec=60),
            )

    def test_extra_fields_rejected(self) -> None:
        """Schema is strict — typos in field names must fail loudly."""
        with pytest.raises(ValidationError):
            Message(
                task_id="t1",
                trace_id="m1",
                sender=Role.ORCHESTRATOR,
                recipient=Role.CODER_WORKER,
                intent=Intent.IMPLEMENT,
                summary="x",
                output_contract="x",
                budgets=Budgets(max_tokens=1000, max_runtime_sec=60),
                extra_typo="oops",  # type: ignore[call-arg]
            )


# ============================================================================
# Handoff — v3.1 完备性规则 is the heart of this file
# ============================================================================


def _base_handoff(**overrides: object) -> Handoff:
    defaults: dict[str, object] = dict(
        task_id="t1",
        completed=["Implemented /health endpoint"],
        next_recommended_action="Send to review_validator",
    )
    defaults.update(overrides)
    return Handoff(**defaults)  # type: ignore[arg-type]


class TestHandoffCompletenessRule:
    """v3.1: incomplete/issues_discovered/deviations_from_plan all empty -> second-pass."""

    def test_all_three_empty_triggers_second_pass(self) -> None:
        h = _base_handoff()
        assert h.triggers_second_pass is True

    def test_incomplete_only_no_second_pass(self) -> None:
        h = _base_handoff(incomplete=["TODO: docs for new endpoint"])
        assert h.triggers_second_pass is False

    def test_issues_only_no_second_pass(self) -> None:
        h = _base_handoff(issues_discovered=["Found pre-existing flaky test"])
        assert h.triggers_second_pass is False

    def test_deviations_only_no_second_pass(self) -> None:
        h = _base_handoff(deviations_from_plan=["Used axum 0.7 not 0.6 as plan suggested"])
        assert h.triggers_second_pass is False

    def test_all_three_filled_no_second_pass(self) -> None:
        h = _base_handoff(
            incomplete=["X TODO"],
            issues_discovered=["Y found"],
            deviations_from_plan=["Z deviated"],
        )
        assert h.triggers_second_pass is False


class TestHandoffStructure:
    def test_command_records(self) -> None:
        h = _base_handoff(
            commands_run=[
                CommandRun(command="cargo test", exit_code=0, summary="47 passed"),
                CommandRun(command="cargo clippy -- -D warnings", exit_code=0, summary="clean"),
            ]
        )
        assert len(h.commands_run) == 2
        assert h.commands_run[0].exit_code == 0


# ============================================================================
# ValidationContract
# ============================================================================


class TestValidationContract:
    def test_minimal_valid(self) -> None:
        contract = ValidationContract(
            mission_id="m1",
            features=[
                Feature(
                    feature_id="f1",
                    description="Add /health endpoint",
                    assertions=[
                        Assertion(
                            id="f1.a1",
                            statement="GET /health returns 200",
                            verification_method=VerificationMethod.BEHAVIOR_PROBE,
                            verification_target="behavior_probe::http_health",
                        ),
                    ],
                ),
            ],
        )
        assert contract.locked is True
        assert len(contract.features) == 1

    def test_empty_features_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ValidationContract(mission_id="m1", features=[])

    def test_feature_without_assertions_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ValidationContract(
                mission_id="m1",
                features=[Feature(feature_id="f1", description="X", assertions=[])],
            )

    def test_yaml_roundtrip(self) -> None:
        """Contract must survive YAML serialization — it's stored as YAML on disk."""
        contract = ValidationContract(
            mission_id="m1",
            features=[
                Feature(
                    feature_id="f1",
                    description="X",
                    assertions=[
                        Assertion(
                            id="f1.a1",
                            statement="Y",
                            verification_method=VerificationMethod.UNIT_TEST,
                            verification_target="tests/foo.rs::test_y",
                        ),
                    ],
                ),
            ],
            non_goals=["Not refactoring routing"],
        )
        yaml_str = pyyaml.safe_dump(contract.model_dump(mode="json"))
        restored = ValidationContract.model_validate(pyyaml.safe_load(yaml_str))
        assert restored.mission_id == contract.mission_id
        assert restored.features[0].assertions[0].id == "f1.a1"
        assert restored.non_goals == ["Not refactoring routing"]


# ============================================================================
# ReviewVerdict — v3.1 additions
# ============================================================================


class TestReviewVerdict:
    def _base_gates(self, *, all_pass: bool = True) -> CargoGateResults:
        return CargoGateResults(build=all_pass, test=all_pass, clippy=all_pass, fmt=all_pass)

    def test_default_no_second_pass_no_hardcoded_warnings(self) -> None:
        verdict = ReviewVerdict(
            task_id="t1",
            result=VerdictResult.PASS,
            precise_reason="All gates clean",
            next_action_recommendation="Send to behavior validator",
            cargo_gate_results=self._base_gates(),
        )
        assert verdict.triggered_second_pass is False
        assert verdict.hardcoded_test_warnings == []
        assert verdict.adversarial_findings == []

    def test_second_pass_with_findings(self) -> None:
        """v3.1: when triggers_second_pass=True, validator should populate findings."""
        verdict = ReviewVerdict(
            task_id="t1",
            result=VerdictResult.PARTIAL,
            precise_reason="Adversarial sub-agent found f1.a3 not covered",
            next_action_recommendation="Coder补强 f1.a3",
            cargo_gate_results=self._base_gates(),
            triggered_second_pass=True,
            adversarial_findings=["f1.a3 has no explicit test"],
            hardcoded_test_warnings=[
                "test_health_status uses assert_eq!(body, 'ok'), should compare semantically"
            ],
        )
        assert verdict.triggered_second_pass is True
        assert len(verdict.hardcoded_test_warnings) == 1


# ============================================================================
# External (SanitizedContent + EgressRecord)
# ============================================================================


class TestEgressRecord:
    def test_minimal_valid(self) -> None:
        r = EgressRecord(
            mission_id="m1",
            task_id="t1",
            url="https://crates.io/api/v1/crates/serde",
            domain="crates.io",
            status_code=200,
            bytes_received=4242,
        )
        assert r.method == "GET"
        assert r.blocked_reason is None

    def test_blocked_record(self) -> None:
        r = EgressRecord(
            mission_id="m1",
            task_id="t1",
            url="https://evil.example.com",
            domain="evil.example.com",
            blocked_reason="domain not in whitelist",
        )
        assert r.status_code is None
        assert r.blocked_reason == "domain not in whitelist"
