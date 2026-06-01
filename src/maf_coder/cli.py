"""CLI entry point — `maf-coder` (AGENT_TOOLS_SPEC §17 step 10).

Phase B subcommands:

- `maf-coder mission new <goal> --repo <path>`     start a new mission
- `maf-coder mission status <mission_id>`          inspect mission state
- `maf-coder mission profile --repo <path>`        print the ProjectProfile

Typer is optional at runtime: if it's not installed, `app` is a thin
argparse-based shim so the module is still importable in test environments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import typer

    app = typer.Typer(
        name="maf-coder",
        help="MAF-Coder — multi-agent framework for autonomous Rust coding missions.",
        no_args_is_help=True,
    )
    mission_app = typer.Typer(
        name="mission", help="Mission lifecycle commands.", no_args_is_help=True
    )
    app.add_typer(mission_app, name="mission")
    _TYPER_AVAILABLE = True
except ImportError:  # pragma: no cover
    typer = None  # type: ignore[assignment]
    app = None  # type: ignore[assignment]
    mission_app = None  # type: ignore[assignment]
    _TYPER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Implementations — independent of Typer so they're directly testable.
# ---------------------------------------------------------------------------


def _missions_root() -> Path:
    return Path(os.environ.get("MAF_MISSIONS_ROOT", str(Path.cwd() / "missions")))


def _default_router_config() -> Path:
    """Locate droid_whispering.yaml; prefer repo-local, fall back to packaged copy."""
    cwd_local = Path.cwd() / "droid_whispering.yaml"
    if cwd_local.exists():
        return cwd_local
    repo_local = Path(__file__).resolve().parents[2] / "droid_whispering.yaml"
    if repo_local.exists():
        return repo_local
    raise FileNotFoundError("droid_whispering.yaml not found. Pass --router-config explicitly.")


def cmd_mission_new(
    *,
    goal: str,
    repo: Path,
    mission_id: str | None = None,
    router_config: Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Bootstrap a new mission. Returns a JSON-serializable summary."""
    from .orchestrator import MissionConfig, MissionDriver

    mid = mission_id or _generate_mission_id()
    cfg = MissionConfig(
        missions_root=_missions_root(),
        repo_path=repo.resolve(),
        router_config=(router_config or _default_router_config()).resolve(),
        goal=goal,
        dry_run=dry_run,
    )
    driver = MissionDriver(mission_id=mid, config=cfg)
    asyncio.run(driver.start())
    return {
        "mission_id": mid,
        "missions_root": str(cfg.missions_root),
        "dry_run": dry_run,
    }


def cmd_mission_status(mission_id: str) -> dict[str, Any]:
    """Read mission_state.json + event totals for an existing mission."""
    from .blackboard import ArtifactStore

    store = ArtifactStore(_missions_root(), mission_id)
    try:
        ms = store.load_mission_state()
    except FileNotFoundError:
        return {"mission_id": mission_id, "error": "mission_state.json missing"}
    ev = store.event_log()
    return {
        "mission_id": mission_id,
        "current_milestone": ms.current_milestone,
        "completed_milestones": list(ms.completed_milestones),
        "cumulative_cost_usd": ev.total_cost_usd(),
        "cumulative_tokens": sum(ev.total_tokens()),
        "coder_provider_in_use": ms.coder_provider_in_use,
    }


def cmd_mission_routing_stats(mission_id: str) -> dict[str, Any]:
    """Tail ROUTE_DECISION events (SR-3) and summarise tier usage + savings.

    Sums ``saved_vs_baseline_usd`` across priced decisions; unpriced ones (None)
    are counted separately rather than treated as zero, so the total stays honest
    about how much of the routing it could actually estimate.
    """
    from .blackboard import ArtifactStore, EventKind

    store = ArtifactStore(_missions_root(), mission_id)
    ev = store.event_log()

    decisions = list(ev.filter_kind(EventKind.ROUTE_DECISION))
    by_tier: dict[str, int] = {}
    total_saved = 0.0
    priced = 0
    unpriced = 0
    for e in decisions:
        tier = str(e.payload.get("tier", "unknown"))
        by_tier[tier] = by_tier.get(tier, 0) + 1
        saved = e.payload.get("saved_vs_baseline_usd")
        if saved is None:
            unpriced += 1
        else:
            priced += 1
            total_saved += float(saved)

    return {
        "mission_id": mission_id,
        "route_decisions": len(decisions),
        "by_tier": by_tier,
        "total_saved_vs_baseline_usd": total_saved,
        "priced_decisions": priced,
        "unpriced_decisions": unpriced,
    }


def cmd_resume(
    *,
    mission_id: str,
    repo: Path,
    from_milestone: str | None = None,
    router_config: Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Resume an existing mission from a checkpoint. JSON-serializable summary."""
    from .orchestrator import MissionConfig, MissionDriver

    cfg = MissionConfig(
        missions_root=_missions_root(),
        repo_path=repo.resolve(),
        router_config=(router_config or _default_router_config()).resolve(),
        goal="(resume)",
        dry_run=dry_run,
    )
    driver = MissionDriver(mission_id=mission_id, config=cfg)
    asyncio.run(driver.resume(from_milestone=from_milestone))
    return {
        "mission_id": mission_id,
        "action": "resume",
        "from_milestone": from_milestone,
        "dry_run": dry_run,
    }


def cmd_rollback(
    *,
    mission_id: str,
    repo: Path,
    to_milestone: str,
    router_config: Path | None = None,
) -> dict[str, Any]:
    """Roll a mission back to an earlier checkpoint. JSON-serializable summary."""
    from .orchestrator import MissionConfig, MissionDriver

    cfg = MissionConfig(
        missions_root=_missions_root(),
        repo_path=repo.resolve(),
        router_config=(router_config or _default_router_config()).resolve(),
        goal="(rollback)",
        dry_run=True,
    )
    driver = MissionDriver(mission_id=mission_id, config=cfg)
    asyncio.run(driver.rollback(to_milestone=to_milestone))
    return {
        "mission_id": mission_id,
        "action": "rollback",
        "to_milestone": to_milestone,
    }


# ---------------------------------------------------------------------------
# F-pr: PR workflow command (Build Plan §Phase F · F5)
# ---------------------------------------------------------------------------


def cmd_pr(
    *,
    mission_id: str,
    repo: Path,
    head_branch: str,
    base_branch: str = "main",
    provider: str = "gh",
    draft: bool = False,
    title: str | None = None,
    goal: str | None = None,
    router_config: Path | None = None,
    sandbox: Any | None = None,
) -> dict[str, Any]:
    """Open a PR/MR from a finished mission. JSON-serializable summary.

    Constructs a ``PullRequestSpec`` from the mission's artifacts (description
    generated by ``integrations.vcs``) and runs the gitleaks gate + gh/glab
    wrapper through the sandbox. The PR is REFUSED (created=False, refused=True)
    when the gitleaks gate finds secrets.

    `sandbox` is injectable so tests can stub ``exec``; when omitted a
    ``LocalShellSandbox`` rooted at `repo` is started. All process exec routes
    through the sandbox — never the host shell.
    """
    from .agents.base import TaskContext
    from .blackboard import ArtifactStore
    from .integrations.vcs import build_artifact_links, create_pull_request, render_pr_body
    from .models import ModelRouter
    from .sandbox import LocalShellSandbox
    from .schemas import (
        NetworkPolicy,
        Permission,
        PullRequestSpec,
        Role,
        Task,
        TaskBudget,
        VcsProvider,
    )

    repo_path = repo.resolve()
    store = ArtifactStore(_missions_root(), mission_id)
    event_log = store.event_log()
    router = ModelRouter((router_config or _default_router_config()).resolve())

    async def _run() -> dict[str, Any]:
        sb = sandbox
        owns_sandbox = sb is None
        if sb is None:
            sb = LocalShellSandbox()
            await sb.start(workspace_mount=repo_path)
        try:
            task = Task(
                task_id=f"pr-{mission_id}",
                parent_milestone="pr",
                owner=Role.ORCHESTRATOR,
                goal="open pull request",
                background="mission-end PR workflow",
                acceptance_criteria=[],
                required_outputs=[],
                permission=Permission(
                    allowed_paths=["**"],
                    allowed_tools=[],
                    network_policy=NetworkPolicy.NONE,
                ),
                budget=TaskBudget(max_tokens=1000, max_runtime_sec=120),
            )
            ctx = TaskContext(
                task=task,
                mission_id=mission_id,
                store=store,
                event_log=event_log,
                router=router,
                sandbox=sb,
            )
            try:
                provider_enum = VcsProvider(provider)
            except ValueError as e:
                raise ValueError(f"invalid provider {provider!r}: gh|glab") from e
            artifact_links = build_artifact_links(store)
            body = render_pr_body(
                mission_id=mission_id,
                store=store,
                event_log=event_log,
                goal=goal,
                artifact_links=artifact_links,
            )
            spec = PullRequestSpec(
                mission_id=mission_id,
                title=title or f"MAF-Coder: {mission_id}",
                body=body,
                head_branch=head_branch,
                base_branch=base_branch,
                provider=provider_enum,
                draft=draft,
                repo_path=str(repo_path),
                artifact_links=artifact_links,
            )
            result = await create_pull_request(ctx, spec)
            return result.model_dump(mode="json")
        finally:
            if owns_sandbox and sb is not None:
                await sb.stop()

    return asyncio.run(_run())


def cmd_mission_profile(repo: Path) -> dict[str, Any]:
    """Run project profiler against a repo path and return the profile dict."""
    from .orchestrator import profile_project

    profile = profile_project(repo)
    return profile.model_dump(mode="json")


def _generate_mission_id() -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    return f"m-{ts}"


# ---------------------------------------------------------------------------
# Typer bindings (only registered when typer is available)
# ---------------------------------------------------------------------------


if _TYPER_AVAILABLE:

    @mission_app.command("new")
    def _mission_new(
        goal: str = typer.Argument(..., help="One-line mission goal."),
        repo: Path = typer.Option(..., "--repo", "-r", help="Path to the target Rust repo."),
        mission_id: str | None = typer.Option(None, "--id", help="Override mission id."),
        router_config: Path | None = typer.Option(
            None, "--router-config", help="Path to droid_whispering.yaml."
        ),
        dry_run: bool = typer.Option(
            True,
            "--dry-run/--no-dry-run",
            help="Dry run skips agent execution; produces profile + state only.",
        ),
    ) -> None:
        result = cmd_mission_new(
            goal=goal,
            repo=repo,
            mission_id=mission_id,
            router_config=router_config,
            dry_run=dry_run,
        )
        typer.echo(json.dumps(result, indent=2))

    @mission_app.command("status")
    def _mission_status(
        mission_id: str = typer.Argument(..., help="Mission id."),
    ) -> None:
        result = cmd_mission_status(mission_id)
        typer.echo(json.dumps(result, indent=2))

    @mission_app.command("stats")
    def _mission_stats(
        mission_id: str = typer.Argument(..., help="Mission id."),
        routing: bool = typer.Option(
            False, "--routing", help="Summarise Smart Router (SR-3) route decisions + savings."
        ),
    ) -> None:
        if not routing:
            typer.echo(
                json.dumps({"error": "pass --routing for the route-decision summary"}, indent=2)
            )
            raise typer.Exit(code=2)
        result = cmd_mission_routing_stats(mission_id)
        typer.echo(json.dumps(result, indent=2))

    @mission_app.command("profile")
    def _mission_profile(
        repo: Path = typer.Option(..., "--repo", "-r", help="Path to the target Rust repo."),
    ) -> None:
        result = cmd_mission_profile(repo)
        typer.echo(json.dumps(result, indent=2))

    @app.command("resume")
    def _resume(
        mission_id: str = typer.Argument(..., help="Mission id to resume."),
        repo: Path = typer.Option(..., "--repo", "-r", help="Path to the target Rust repo."),
        from_milestone: str | None = typer.Option(
            None, "--from", help="Checkpoint milestone to resume from (default: latest)."
        ),
        router_config: Path | None = typer.Option(
            None, "--router-config", help="Path to droid_whispering.yaml."
        ),
        dry_run: bool = typer.Option(
            True,
            "--dry-run/--no-dry-run",
            help="Dry run restores state+sandbox without re-running execution.",
        ),
    ) -> None:
        result = cmd_resume(
            mission_id=mission_id,
            repo=repo,
            from_milestone=from_milestone,
            router_config=router_config,
            dry_run=dry_run,
        )
        typer.echo(json.dumps(result, indent=2))

    @app.command("pr")
    def _pr(
        mission_id: str = typer.Argument(..., help="Finished mission id to open a PR for."),
        repo: Path = typer.Option(..., "--repo", "-r", help="Path to the target git repo."),
        head_branch: str = typer.Option(
            ..., "--head", help="Source branch the PR/MR is opened from."
        ),
        base_branch: str = typer.Option("main", "--base", help="Target branch to merge into."),
        provider: str = typer.Option("gh", "--provider", help="gh (GitHub) | glab (GitLab)."),
        draft: bool = typer.Option(False, "--draft", help="Open as a draft PR/MR."),
        title: str | None = typer.Option(None, "--title", help="Override the PR title."),
        router_config: Path | None = typer.Option(
            None, "--router-config", help="Path to droid_whispering.yaml."
        ),
    ) -> None:
        result = cmd_pr(
            mission_id=mission_id,
            repo=repo,
            head_branch=head_branch,
            base_branch=base_branch,
            provider=provider,
            draft=draft,
            title=title,
            router_config=router_config,
        )
        typer.echo(json.dumps(result, indent=2))

    @app.command("rollback")
    def _rollback(
        mission_id: str = typer.Argument(..., help="Mission id to roll back."),
        to_milestone: str = typer.Option(
            ..., "--to", help="Completed milestone to roll back to."
        ),
        repo: Path = typer.Option(..., "--repo", "-r", help="Path to the target Rust repo."),
        router_config: Path | None = typer.Option(
            None, "--router-config", help="Path to droid_whispering.yaml."
        ),
    ) -> None:
        result = cmd_rollback(
            mission_id=mission_id,
            repo=repo,
            to_milestone=to_milestone,
            router_config=router_config,
        )
        typer.echo(json.dumps(result, indent=2))


def main() -> None:  # pragma: no cover - thin shell entry
    if _TYPER_AVAILABLE:
        app()
        return
    print("typer is not installed; install with 'pip install typer'", file=sys.stderr)
    sys.exit(2)


__all__ = [
    "app",
    "cmd_mission_new",
    "cmd_mission_profile",
    "cmd_mission_routing_stats",
    "cmd_mission_status",
    "cmd_pr",
    "cmd_resume",
    "cmd_rollback",
    "main",
]
