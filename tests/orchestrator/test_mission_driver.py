"""MissionDriver dry-run tests (AGENT_TOOLS_SPEC §14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.orchestrator import MissionConfig, MissionDriver
from maf_coder.sandbox import LocalShellSandbox


def _write_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n[[bin]]\nname = "demo"\n',
        encoding="utf-8",
    )


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


@pytest.mark.asyncio
async def test_dry_run_produces_profile_and_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_repo(repo)
    router_cfg = tmp_path / "droid.yaml"
    _write_router(router_cfg)
    cfg = MissionConfig(
        missions_root=tmp_path / "missions",
        repo_path=repo,
        router_config=router_cfg,
        goal="demo",
        sandbox_factory=lambda: LocalShellSandbox(),
        dry_run=True,
    )
    driver = MissionDriver(mission_id="m-dry", config=cfg)
    await driver.start()
    assert (cfg.missions_root / "m-dry" / "project_profile.yaml").exists()
    assert (cfg.missions_root / "m-dry" / "mission_state.json").exists()
    events = list(driver.event_log.iter_events())
    kinds = [e.kind for e in events]
    assert "mission_start" in kinds
    assert "mission_end" in kinds
