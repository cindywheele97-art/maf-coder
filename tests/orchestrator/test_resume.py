"""resume / rollback / snapshot-GC tests (Phase E E-recovery).

WHY each test matters (not just what it does):

- resume must put the mission *back* to a checkpoint: state position reset +
  sandbox restored. If only one of those happens, a multi-day mission resumes
  into an inconsistent world. We assert BOTH.
- rollback must move strictly backward and truncate completed_milestones; a
  rollback that silently rolls forward, or leaves later milestones marked
  complete, corrupts the resume target set. We assert truncation AND forward
  refusal.
- GC must never delete a retained snapshot (data loss = unrecoverable mission)
  but must reclaim orphans. We assert both halves.
- Missing checkpoints must be a clear error, not a crash (operators resume by
  hand and typo milestone ids).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maf_coder.blackboard import ArtifactStore
from maf_coder.orchestrator import (
    CheckpointStore,
    MissionConfig,
    MissionDriver,
)
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import Checkpoint, MissionState


def _write_router(path: Path) -> None:
    path.write_text(
        "version: 1\n"
        "roles:\n"
        "  orchestrator:\n"
        "    primary: {model: openai/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  coder_worker:\n"
        "    primary: {model: anthropic/x, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
        "  review_validator:\n"
        "    primary: {model: openai/x, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback: []\n",
        encoding="utf-8",
    )


async def _make_checkpoint(
    store: ArtifactStore,
    repo: Path,
    milestone_id: str,
    *,
    file_content: str,
) -> Checkpoint:
    """Commit a real LocalShellSandbox snapshot and save a Checkpoint for it."""
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=repo)
    await sb.write_file("state.txt", file_content)
    snap = await sb.commit_snapshot(f"mission/{store.mission_id}/{milestone_id}")
    await sb.stop()
    cp = Checkpoint(
        mission_id=store.mission_id,
        milestone_id=milestone_id,
        git_tag=f"mission/{store.mission_id}/{milestone_id}",
        sandbox_snapshot_id=snap,
        artifact_archive_path=f"checkpoints/{milestone_id}/",
        cumulative_cost_usd=1.0,
        cumulative_wall_clock_hours=2.0,
    )
    store.save_checkpoint(cp)
    return cp


def _config(missions_root: Path, repo: Path, router: Path, *, dry_run: bool) -> MissionConfig:
    return MissionConfig(
        missions_root=missions_root,
        repo_path=repo,
        router_config=router,
        goal="resume-test",
        sandbox_factory=lambda: LocalShellSandbox(),
        dry_run=dry_run,
    )


@pytest.fixture
def mission_env(tmp_path: Path):
    missions_root = tmp_path / "missions"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    router = tmp_path / "droid.yaml"
    _write_router(router)
    store = ArtifactStore(missions_root, "m-resume")
    state = MissionState(
        mission_id="m-resume",
        started_at=datetime.now(UTC),
        completed_milestones=["m1", "m2"],
        current_milestone="m3",
    )
    store.save_mission_state(state)
    return missions_root, repo, router, store


class TestResume:
    @pytest.mark.asyncio
    async def test_dry_run_resume_restores_state_and_sandbox(self, mission_env) -> None:
        missions_root, repo, router, store = mission_env
        await _make_checkpoint(store, repo, "m2", file_content="at-m2")

        # Mutate the live workspace AFTER the checkpoint to prove restore wins.
        (repo / "state.txt").write_text("dirty-post-checkpoint", encoding="utf-8")

        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-resume", config=cfg)
        await driver.resume(from_milestone="m2")

        # Sandbox restored to the checkpoint's snapshot.
        assert (repo / "state.txt").read_text(encoding="utf-8") == "at-m2"
        # State reset to the checkpoint position: m3 dropped, current = m2.
        reloaded = store.load_mission_state()
        assert reloaded.completed_milestones == ["m1", "m2"]
        assert reloaded.current_milestone == "m2"

    @pytest.mark.asyncio
    async def test_resume_latest_when_no_from_milestone(self, mission_env) -> None:
        missions_root, repo, router, store = mission_env
        await _make_checkpoint(store, repo, "m1", file_content="at-m1")
        await _make_checkpoint(store, repo, "m2", file_content="at-m2")

        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-resume", config=cfg)
        await driver.resume()  # no from_milestone -> latest completed (m2)

        assert (repo / "state.txt").read_text(encoding="utf-8") == "at-m2"
        assert store.load_mission_state().current_milestone == "m2"

    @pytest.mark.asyncio
    async def test_resume_missing_checkpoint_raises(self, mission_env) -> None:
        missions_root, repo, router, _store = mission_env
        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-resume", config=cfg)
        with pytest.raises(FileNotFoundError):
            await driver.resume(from_milestone="m99")

    @pytest.mark.asyncio
    async def test_resume_missing_mission_state_raises(self, tmp_path: Path) -> None:
        missions_root = tmp_path / "missions"
        repo = tmp_path / "repo"
        repo.mkdir(parents=True)
        router = tmp_path / "droid.yaml"
        _write_router(router)
        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-nope", config=cfg)
        with pytest.raises(FileNotFoundError):
            await driver.resume()


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_truncates_and_restores(self, mission_env) -> None:
        missions_root, repo, router, store = mission_env
        await _make_checkpoint(store, repo, "m1", file_content="at-m1")
        await _make_checkpoint(store, repo, "m2", file_content="at-m2")
        (repo / "state.txt").write_text("current", encoding="utf-8")

        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-resume", config=cfg)
        await driver.rollback(to_milestone="m1")

        # Earlier snapshot restored.
        assert (repo / "state.txt").read_text(encoding="utf-8") == "at-m1"
        # completed_milestones truncated to <= m1, current = m1.
        reloaded = store.load_mission_state()
        assert reloaded.completed_milestones == ["m1"]
        assert reloaded.current_milestone == "m1"

    @pytest.mark.asyncio
    async def test_rollback_refuses_forward(self, mission_env) -> None:
        missions_root, repo, router, store = mission_env
        await _make_checkpoint(store, repo, "m2", file_content="at-m2")
        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-resume", config=cfg)
        # m5 is not in completed_milestones -> would be rolling forward.
        with pytest.raises(ValueError):
            await driver.rollback(to_milestone="m5")
        # State untouched.
        assert store.load_mission_state().completed_milestones == ["m1", "m2"]

    @pytest.mark.asyncio
    async def test_rollback_missing_checkpoint_raises(self, mission_env) -> None:
        missions_root, repo, router, _store = mission_env
        # m1 is completed but has no checkpoint.json on disk.
        cfg = _config(missions_root, repo, router, dry_run=True)
        driver = MissionDriver(mission_id="m-resume", config=cfg)
        with pytest.raises(FileNotFoundError):
            await driver.rollback(to_milestone="m1")


class TestCheckpointStoreGC:
    @pytest.mark.asyncio
    async def test_gc_keeps_retained_deletes_orphans(self, mission_env) -> None:
        _missions_root, repo, _router, store = mission_env
        cp1 = await _make_checkpoint(store, repo, "m1", file_content="a")
        cp2 = await _make_checkpoint(store, repo, "m2", file_content="b")
        # An orphan checkpoint: NOT in completed_milestones (state has m1,m2).
        cp_orphan = await _make_checkpoint(store, repo, "m0_orphan", file_content="c")

        cp_store = CheckpointStore(store)
        state = store.load_mission_state()
        sandbox_root = repo.parent  # where local tarballs land

        deleted = cp_store.gc_snapshots(state, sandbox_root=sandbox_root)

        assert cp_orphan.sandbox_snapshot_id in deleted
        assert not Path(cp_orphan.sandbox_snapshot_id).exists()
        # Retained snapshots survive.
        assert Path(cp1.sandbox_snapshot_id).exists()
        assert Path(cp2.sandbox_snapshot_id).exists()

    @pytest.mark.asyncio
    async def test_gc_dry_run_deletes_nothing(self, mission_env) -> None:
        _missions_root, repo, _router, store = mission_env
        await _make_checkpoint(store, repo, "m1", file_content="a")
        cp_orphan = await _make_checkpoint(store, repo, "m0_orphan", file_content="c")

        cp_store = CheckpointStore(store)
        state = store.load_mission_state()
        would = cp_store.gc_snapshots(
            state, sandbox_root=repo.parent, dry_run=True
        )
        assert cp_orphan.sandbox_snapshot_id in would
        assert Path(cp_orphan.sandbox_snapshot_id).exists()  # untouched

    def test_list_milestones_and_resolve_target(self, mission_env) -> None:
        _missions_root, _repo, _router, store = mission_env
        cp_store = CheckpointStore(store)
        assert cp_store.list_milestones() == []
        # No checkpoints -> resolve raises a clear error.
        with pytest.raises(FileNotFoundError):
            cp_store.resolve_target(store.load_mission_state())


class TestCli:
    def test_cmd_resume_calls_driver(self, monkeypatch, tmp_path: Path) -> None:
        from maf_coder import cli

        called: dict[str, object] = {}

        class _StubDriver:
            def __init__(self, *, mission_id, config):
                called["mission_id"] = mission_id
                called["config"] = config

            async def resume(self, *, from_milestone=None):
                called["from_milestone"] = from_milestone

        monkeypatch.setattr("maf_coder.orchestrator.MissionDriver", _StubDriver)
        router = tmp_path / "droid.yaml"
        _write_router(router)
        out = cli.cmd_resume(
            mission_id="m-x",
            repo=tmp_path,
            from_milestone="m2",
            router_config=router,
            dry_run=True,
        )
        assert called["mission_id"] == "m-x"
        assert called["from_milestone"] == "m2"
        assert out["action"] == "resume"
        assert out["from_milestone"] == "m2"

    def test_cmd_rollback_calls_driver(self, monkeypatch, tmp_path: Path) -> None:
        from maf_coder import cli

        called: dict[str, object] = {}

        class _StubDriver:
            def __init__(self, *, mission_id, config):
                called["mission_id"] = mission_id

            async def rollback(self, *, to_milestone):
                called["to_milestone"] = to_milestone

        monkeypatch.setattr("maf_coder.orchestrator.MissionDriver", _StubDriver)
        router = tmp_path / "droid.yaml"
        _write_router(router)
        out = cli.cmd_rollback(
            mission_id="m-y",
            repo=tmp_path,
            to_milestone="m1",
            router_config=router,
        )
        assert called["mission_id"] == "m-y"
        assert called["to_milestone"] == "m1"
        assert out["action"] == "rollback"
        assert out["to_milestone"] == "m1"
