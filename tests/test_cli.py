"""CLI smoke tests (AGENT_TOOLS_SPEC §17 step 10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder import cli


def _write_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n[lib]\nname = "demo"\n',
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


def test_cmd_mission_profile(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    _write_repo(repo)
    out = cli.cmd_mission_profile(repo)
    assert "project_type" in out
    assert out["crate_layout"] == "single"


def test_cmd_mission_new_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "r"
    _write_repo(repo)
    router = tmp_path / "droid.yaml"
    _write_router(router)
    monkeypatch.setenv("MAF_MISSIONS_ROOT", str(tmp_path / "missions"))
    out = cli.cmd_mission_new(
        goal="demo", repo=repo, mission_id="m-cli", router_config=router, dry_run=True
    )
    assert out["mission_id"] == "m-cli"
    assert out["dry_run"] is True


def test_cmd_mission_status_for_running_mission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "r"
    _write_repo(repo)
    router = tmp_path / "droid.yaml"
    _write_router(router)
    monkeypatch.setenv("MAF_MISSIONS_ROOT", str(tmp_path / "missions"))
    cli.cmd_mission_new(
        goal="demo", repo=repo, mission_id="m-cli", router_config=router, dry_run=True
    )
    status = cli.cmd_mission_status("m-cli")
    assert status["mission_id"] == "m-cli"
    assert "cumulative_cost_usd" in status


def test_cmd_mission_status_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAF_MISSIONS_ROOT", str(tmp_path / "missions"))
    status = cli.cmd_mission_status("does-not-exist")
    assert "error" in status


def test_app_is_typer() -> None:
    # In dev environments typer is a declared dependency.
    if cli._TYPER_AVAILABLE:
        assert cli.app is not None
        cmds = {c.name for c in cli.mission_app.registered_commands}
        assert {"new", "status", "profile"}.issubset(cmds)
