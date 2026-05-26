"""Tests for ArtifactStore.

Phase A 退出门槛: `pytest tests/test_artifact_store.py` 全过.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maf_coder.blackboard import (
    ArtifactStore,
    ContractAlreadyLockedError,
    PathEscapeError,
)
from maf_coder.schemas import (
    Assertion,
    BehaviorObservation,
    BehaviorProbeSpec,
    BehaviorVerdict,
    BudgetStatus,
    CargoGateResults,
    Checkpoint,
    Crate,
    Feature,
    Handoff,
    MilestoneStatus,
    MissionState,
    ProjectProfile,
    ProjectType,
    ReviewVerdict,
    SecurityFinding,
    SecurityVerdict,
    Severity,
    StatusReport,
    ValidationContract,
    VerdictResult,
    VerificationMethod,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-test-001")


@pytest.fixture
def sample_contract() -> ValidationContract:
    return ValidationContract(
        mission_id="m-test-001",
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
        non_goals=["Not refactoring routing"],
    )


@pytest.fixture
def sample_profile() -> ProjectProfile:
    return ProjectProfile(
        project_type=ProjectType.BACKEND_SERVICE,
        crate_layout="single",
        crates=[Crate(name="api", type="binary", targets=["api"])],
        behavior_probe=BehaviorProbeSpec(
            strategy="backend_service_health_probe",
            start_command="cargo run --bin api",
            ready_check="curl -sf http://localhost:8080/health",
            endpoints_to_probe=["/api/v1/healthz"],
        ),
    )


# ---------------------------------------------------------------------------
# Construction & path safety
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_creates_mission_dir(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path / "missions", "m-001")
        assert (tmp_path / "missions" / "m-001").is_dir()

    def test_resume_existing_mission_dir(self, tmp_path: Path) -> None:
        # First create, write something
        s1 = ArtifactStore(tmp_path / "missions", "m-001")
        s1.write_text("plan.md", "first")
        # Second instance against same dir should succeed and see the file
        s2 = ArtifactStore(tmp_path / "missions", "m-001")
        assert s2.read_text("plan.md") == "first"

    def test_rejects_traversal_mission_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ArtifactStore(tmp_path / "missions", "../escape")

    def test_rejects_slash_in_mission_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ArtifactStore(tmp_path / "missions", "a/b")

    def test_rejects_empty_mission_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ArtifactStore(tmp_path / "missions", "")


class TestPathSafety:
    def test_normal_write_ok(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "hello")
        assert store.read_text("plan.md") == "hello"

    def test_subdirectory_lazy_create(self, store: ArtifactStore) -> None:
        store.write_text("research_notes/topic-a.md", "notes")
        assert store.exists("research_notes/topic-a.md")

    def test_rejects_dotdot_escape(self, store: ArtifactStore) -> None:
        with pytest.raises(PathEscapeError):
            store.write_text("../escape.txt", "bad")

    def test_rejects_absolute_path_escape(self, store: ArtifactStore, tmp_path: Path) -> None:
        with pytest.raises(PathEscapeError):
            store.write_text(str(tmp_path / "outside.txt"), "bad")


# ---------------------------------------------------------------------------
# Generic read/write
# ---------------------------------------------------------------------------


class TestGenericIO:
    def test_text_roundtrip(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "the plan\n包含中文")
        assert store.read_text("plan.md") == "the plan\n包含中文"

    def test_json_roundtrip_dict(self, store: ArtifactStore) -> None:
        store.write_json("data.json", {"a": 1, "b": [2, 3]})
        assert store.read_json("data.json") == {"a": 1, "b": [2, 3]}

    def test_yaml_roundtrip_dict(self, store: ArtifactStore) -> None:
        store.write_yaml("data.yaml", {"key": "value", "list": [1, 2, 3]})
        assert store.read_yaml("data.yaml") == {"key": "value", "list": [1, 2, 3]}

    def test_exists(self, store: ArtifactStore) -> None:
        assert store.exists("plan.md") is False
        store.write_text("plan.md", "x")
        assert store.exists("plan.md") is True

    def test_list_dir_empty_for_missing(self, store: ArtifactStore) -> None:
        assert store.list_dir("research_notes") == []

    def test_list_dir_returns_sorted_files(self, store: ArtifactStore) -> None:
        store.write_text("research_notes/b.md", "b")
        store.write_text("research_notes/a.md", "a")
        store.write_text("research_notes/c.md", "c")
        files = store.list_dir("research_notes")
        assert [p.name for p in files] == ["a.md", "b.md", "c.md"]


# ---------------------------------------------------------------------------
# Atomic write — visible behavior is "no partial file ever appears"
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_overwrite_replaces_cleanly(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "first version")
        store.write_text("plan.md", "second version")
        assert store.read_text("plan.md") == "second version"
        # No leftover .tmp files
        leftovers = [p for p in store.mission_dir.iterdir() if p.name.startswith(".plan.md.")]
        assert leftovers == []

    def test_no_tmp_file_after_normal_write(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "content")
        all_files = list(store.mission_dir.iterdir())
        tmp_files = [p for p in all_files if ".tmp" in p.name]
        assert tmp_files == []


# ---------------------------------------------------------------------------
# Validation contract — write-once enforcement (the soul.md §2 hard rule)
# ---------------------------------------------------------------------------


class TestContractWriteOnce:
    def test_first_save_succeeds(
        self, store: ArtifactStore, sample_contract: ValidationContract
    ) -> None:
        store.save_validation_contract(sample_contract)
        assert store.exists("validation_contract.yaml")

    def test_second_save_blocked_by_default(
        self, store: ArtifactStore, sample_contract: ValidationContract
    ) -> None:
        store.save_validation_contract(sample_contract)
        with pytest.raises(ContractAlreadyLockedError):
            store.save_validation_contract(sample_contract)

    def test_allow_overwrite_explicit(
        self, store: ArtifactStore, sample_contract: ValidationContract
    ) -> None:
        store.save_validation_contract(sample_contract)
        # Override only allowed when caller explicitly asserts authorization
        store.save_validation_contract(sample_contract, allow_overwrite=True)
        loaded = store.load_validation_contract()
        assert loaded.mission_id == sample_contract.mission_id

    def test_contract_yaml_roundtrip_preserves_assertions(
        self, store: ArtifactStore, sample_contract: ValidationContract
    ) -> None:
        store.save_validation_contract(sample_contract)
        loaded = store.load_validation_contract()
        assert loaded.features[0].assertions[0].id == "f1.a1"
        assert loaded.non_goals == ["Not refactoring routing"]


# ---------------------------------------------------------------------------
# Typed round trips: profile, handoff, verdicts, mission state, checkpoint
# ---------------------------------------------------------------------------


class TestTypedRoundtrips:
    def test_project_profile(self, store: ArtifactStore, sample_profile: ProjectProfile) -> None:
        store.save_project_profile(sample_profile)
        loaded = store.load_project_profile()
        assert loaded.project_type == sample_profile.project_type
        assert loaded.crates[0].name == "api"
        assert loaded.behavior_probe.strategy == "backend_service_health_probe"

    def test_handoff(self, store: ArtifactStore) -> None:
        h = Handoff(
            task_id="t1",
            completed=["Implemented health endpoint"],
            incomplete=["Docstrings still TODO"],
            next_recommended_action="Send to review_validator",
        )
        store.save_handoff("t1", h)
        loaded = store.load_handoff("t1")
        assert loaded.completed == ["Implemented health endpoint"]
        assert loaded.triggers_second_pass is False  # incomplete is non-empty

    def test_review_verdict(self, store: ArtifactStore) -> None:
        v = ReviewVerdict(
            task_id="t1",
            result=VerdictResult.PASS,
            precise_reason="All gates clean",
            next_action_recommendation="To behavior validator",
            cargo_gate_results=CargoGateResults(build=True, test=True, clippy=True, fmt=True),
        )
        store.save_review_verdict("t1", v)
        loaded = store.load_review_verdict("t1")
        assert loaded.result == "pass"
        assert loaded.cargo_gate_results.clippy is True

    def test_behavior_verdict(self, store: ArtifactStore) -> None:
        v = BehaviorVerdict(
            task_id="t1",
            result=VerdictResult.PASS,
            probe_strategy="backend_service_health_probe",
            observations=[
                BehaviorObservation(
                    assertion_id="f1.a1",
                    observed="200 OK",
                    expected="200",
                    matched=True,
                ),
            ],
            evidence_path="behavior_evidence/t1/",
        )
        store.save_behavior_verdict("t1", v)
        loaded = store.load_behavior_verdict("t1")
        assert loaded.observations[0].matched is True

    def test_security_verdict_with_critical_blocks_pr(self, store: ArtifactStore) -> None:
        v = SecurityVerdict(
            task_id="t1",
            findings=[
                SecurityFinding(
                    severity=Severity.CRITICAL,
                    category="audit",
                    description="CVE-2025-12345 in foo crate",
                ),
            ],
        )
        store.save_security_verdict("t1", v)
        loaded = store.load_security_verdict("t1")
        assert loaded.critical_count == 1
        assert loaded.blocks_pr is True

    def test_mission_state(self, store: ArtifactStore) -> None:
        s = MissionState(
            mission_id="m-test-001",
            started_at=datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
            current_milestone="m2",
            completed_milestones=["m1"],
            cumulative_cost_usd=12.34,
            coder_provider_in_use="anthropic",
        )
        store.save_mission_state(s)
        loaded = store.load_mission_state()
        assert loaded.completed_milestones == ["m1"]
        assert loaded.cumulative_cost_usd == 12.34
        assert loaded.coder_provider_in_use == "anthropic"

    def test_checkpoint(self, store: ArtifactStore) -> None:
        cp = Checkpoint(
            mission_id="m-test-001",
            milestone_id="m1",
            git_tag="mission/m-test-001/m1",
            sandbox_snapshot_id="docker-image-id-deadbeef",
            artifact_archive_path="checkpoints/m1/",
            cumulative_cost_usd=5.50,
            cumulative_wall_clock_hours=2.5,
        )
        store.save_checkpoint(cp)
        loaded = store.load_checkpoint("m1")
        assert loaded.git_tag == "mission/m-test-001/m1"


# ---------------------------------------------------------------------------
# StatusReport — special: writes both .json and .md
# ---------------------------------------------------------------------------


class TestStatusReport:
    def _sample(self, n: int = 1) -> StatusReport:
        return StatusReport(
            report_number=n,
            mission_id="m-test-001",
            mission_started_at=datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
            elapsed_hours=4.5,
            milestones=[
                MilestoneStatus(milestone_id="m1", state="complete"),
                MilestoneStatus(milestone_id="m2", state="in_progress"),
            ],
            current_activity="Coder working on f2: /version endpoint",
            budget_status=BudgetStatus(
                tokens_used=125000,
                cost_usd=8.45,
                alert_threshold_usd=100.0,
                projected_total_usd=42.10,
                wall_clock_vs_estimate_pct=105.0,
            ),
            next_milestone_eta_hours=2.0,
        )

    def test_writes_both_json_and_md(self, store: ArtifactStore) -> None:
        report = self._sample(n=1)
        json_path, md_path = store.save_status_report(report)
        assert json_path.exists()
        assert md_path.exists()
        assert json_path.suffix == ".json"
        assert md_path.suffix == ".md"

    def test_md_contains_key_sections(self, store: ArtifactStore) -> None:
        report = self._sample(n=3)
        _, md_path = store.save_status_report(report)
        md = md_path.read_text()
        assert "Status Report #3" in md
        assert "Mission Progress" in md
        assert "Budget Status" in md
        assert "How to Steer" in md

    def test_report_number_pads_filename(self, store: ArtifactStore) -> None:
        report = self._sample(n=42)
        json_path, _ = store.save_status_report(report)
        assert json_path.name == "status_0042.json"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_list_handoffs(self, store: ArtifactStore) -> None:
        for tid in ["t1", "t2", "t3"]:
            h = Handoff(
                task_id=tid,
                completed=[f"task {tid}"],
                issues_discovered=[f"some issue in {tid}"],
                next_recommended_action="next",
            )
            store.save_handoff(tid, h)
        assert sorted(store.list_handoffs()) == ["t1", "t2", "t3"]

    def test_list_research_notes(self, store: ArtifactStore) -> None:
        store.write_text("research_notes/axum.md", "...")
        store.write_text("research_notes/tokio.md", "...")
        store.write_text("research_notes/serde.md", "...")
        assert sorted(store.list_research_notes()) == ["axum", "serde", "tokio"]

    def test_iter_all_files(self, store: ArtifactStore) -> None:
        store.write_text("plan.md", "p")
        store.write_text("research_notes/a.md", "a")
        store.write_text("handoff/t1.json", "{}")
        files = sorted(p.name for p in store.iter_all_files())
        assert files == ["a.md", "plan.md", "t1.json"]
