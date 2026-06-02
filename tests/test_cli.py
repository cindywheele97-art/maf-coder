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


def test_default_router_config_resolves_to_existing_file() -> None:
    """`mission new` without --router-config must find the shipped config — it
    lives in config/, which the resolver now checks (regression for a path bug
    that made the default always raise FileNotFoundError)."""
    p = cli._default_router_config()
    assert p.exists()
    assert p.name == "droid_whispering.yaml"


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


def test_cmd_mission_new_coder_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--coder-provider flows into mission_state; omitting it derives from the
    router's coder_worker primary (anthropic in the test config)."""
    from maf_coder.blackboard import ArtifactStore

    repo = tmp_path / "r"
    _write_repo(repo)
    router = tmp_path / "droid.yaml"
    _write_router(router)
    root = tmp_path / "missions"
    monkeypatch.setenv("MAF_MISSIONS_ROOT", str(root))

    # explicit override
    cli.cmd_mission_new(
        goal="demo", repo=repo, mission_id="m-ovr", router_config=router,
        dry_run=True, coder_provider="openai",
    )
    assert ArtifactStore(root, "m-ovr").load_mission_state().coder_provider_in_use == "openai"

    # derived (no flag) -> anthropic from the test router
    cli.cmd_mission_new(
        goal="demo", repo=repo, mission_id="m-der", router_config=router, dry_run=True
    )
    assert ArtifactStore(root, "m-der").load_mission_state().coder_provider_in_use == "anthropic"


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
        top = {c.name for c in cli.app.registered_commands}
        assert "metrics" in top


def test_cmd_metrics_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_metrics computes the baseline over the missions root; markdown and
    json forms both work."""
    from datetime import UTC, datetime

    from maf_coder.blackboard import ArtifactStore
    from maf_coder.schemas import MissionState

    root = tmp_path / "missions"
    monkeypatch.setenv("MAF_MISSIONS_ROOT", str(root))
    store = ArtifactStore(root, "m-cli")
    store.save_mission_state(MissionState(mission_id="m-cli", started_at=datetime.now(UTC)))
    store.event_log().log_mission_end(
        mission_id="m-cli", result="complete", total_cost_usd=0.0, total_wall_clock_hours=1.0
    )

    js = cli.cmd_metrics()
    assert isinstance(js, dict)
    assert js["mission_count"] == 1
    assert js["final_pass_rate"] == 1.0

    md = cli.cmd_metrics(markdown=True)
    assert isinstance(md, str)
    assert "Health Metric Baseline" in md


# ---------------------------------------------------------------------------
# F-pr: pr command
# ---------------------------------------------------------------------------


class _CliStubSandbox:
    """Stub sandbox: gitleaks-clean, then returns a canned PR URL from gh."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exec(self, cmd: str, *, cwd: str = "/workspace", timeout_sec: int = 60):
        from maf_coder.agents.results import CommandResult

        self.calls.append(cmd)
        if "gitleaks" in cmd:
            return CommandResult(command=cmd, exit_code=0, stdout="[]", stderr="", duration_sec=0.0)
        return CommandResult(
            command=cmd,
            exit_code=0,
            stdout="https://github.com/acme/widget/pull/5",
            stderr="",
            duration_sec=0.0,
        )


def test_cmd_pr_calls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "r"
    _write_repo(repo)
    router = tmp_path / "droid.yaml"
    _write_router(router)
    monkeypatch.setenv("MAF_MISSIONS_ROOT", str(tmp_path / "missions"))
    sandbox = _CliStubSandbox()
    out = cli.cmd_pr(
        mission_id="m-pr-cli",
        repo=repo,
        head_branch="feature/x",
        provider="gh",
        router_config=router,
        sandbox=sandbox,
    )
    assert out["created"] is True
    assert out["url"] == "https://github.com/acme/widget/pull/5"
    assert any(c.startswith("gh pr create") for c in sandbox.calls)


def test_app_has_pr_command() -> None:
    if cli._TYPER_AVAILABLE:
        names = {c.name for c in cli.app.registered_commands}
        assert "pr" in names
