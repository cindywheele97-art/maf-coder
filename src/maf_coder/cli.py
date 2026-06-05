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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from .sandbox import SandboxClient

logger = logging.getLogger(__name__)

# Default Rust sandbox image (built by `scripts/build_sandbox.sh`).
_DEFAULT_SANDBOX_IMAGE = "maf-coder:rust-sandbox"

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
    """Locate droid_whispering.yaml. Canonical location is `config/`, but a
    repo-root copy is also honored. Checks the cwd then the installed repo root."""
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        Path.cwd() / "config" / "droid_whispering.yaml",
        Path.cwd() / "droid_whispering.yaml",
        repo_root / "config" / "droid_whispering.yaml",
        repo_root / "droid_whispering.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "droid_whispering.yaml not found in ./config, ., or the repo root. "
        "Pass --router-config explicitly."
    )


def _build_sandbox_factory(sandbox: str, image: str) -> Callable[[], SandboxClient]:
    """Map a ``--sandbox`` choice to a SandboxClient factory.

    ``docker`` fails loud (no silent fallback) when docker-py/the daemon is
    unavailable: the operator asked for isolation deliberately, so degrading to
    the unisolated host shell would be a dangerous surprise.
    """
    from .sandbox import DockerSandbox, LocalShellSandbox

    if sandbox == "local":
        return lambda: LocalShellSandbox()
    if sandbox == "docker":
        if not DockerSandbox.is_available():
            raise RuntimeError(
                "Docker sandbox requested (--sandbox docker) but Docker is "
                "unavailable. Install docker-py (`pip install docker`), ensure the "
                "daemon is running, and build the image with "
                "`bash scripts/build_sandbox.sh`."
            )
        return lambda: DockerSandbox(image=image)
    raise ValueError(
        f"unknown --sandbox value: {sandbox!r} (expected 'local' or 'docker')"
    )


def _resolve_sandbox(sandbox: str | None, *, dry_run: bool) -> str:
    """Resolve the effective sandbox backend (secure-by-default).

    An explicit ``--sandbox`` always wins. Otherwise a REAL run defaults to
    ``docker`` (container isolation — autonomous agents must not run shell/cargo
    on the host), while a dry-run defaults to ``local``: a dry-run executes no
    agent code, so isolation is moot and Docker should not gate the cheap
    profile/dry-run/smoke path.
    """
    if sandbox is not None:
        return sandbox
    return "local" if dry_run else "docker"


def cmd_mission_new(
    *,
    goal: str,
    repo: Path,
    mission_id: str | None = None,
    router_config: Path | None = None,
    dry_run: bool = True,
    coder_provider: str | None = None,
    budget_usd: float | None = None,
    sandbox: str | None = None,
    sandbox_image: str = _DEFAULT_SANDBOX_IMAGE,
) -> dict[str, Any]:
    """Bootstrap a new mission. Returns a JSON-serializable summary.

    `coder_provider` overrides the Coder's provider for the 异-provider rule.
    Left None (the usual case), the MissionDriver derives it from the router's
    coder_worker primary model.

    `budget_usd` sets the full mission budget seeded into budget.yaml (the budget
    guard's ceiling). Left None, the guard's default is used.

    `sandbox` selects the execution backend: "local" (host shell, no isolation)
    or "docker" (isolated container, image `sandbox_image`).
    """
    from .orchestrator import MissionConfig, MissionDriver

    mid = mission_id or _generate_mission_id()
    cfg = MissionConfig(
        missions_root=_missions_root(),
        repo_path=repo.resolve(),
        router_config=(router_config or _default_router_config()).resolve(),
        goal=goal,
        dry_run=dry_run,
        coder_provider_in_use=coder_provider,
        total_budget_usd=budget_usd,
        sandbox_factory=_build_sandbox_factory(
            _resolve_sandbox(sandbox, dry_run=dry_run), sandbox_image
        ),
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
        "budget_mode": ms.budget_mode,
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
    sandbox: str | None = None,
    sandbox_image: str = _DEFAULT_SANDBOX_IMAGE,
) -> dict[str, Any]:
    """Resume an existing mission from a checkpoint. JSON-serializable summary.

    `sandbox` must match the backend the mission ran under (see cmd_mission_new);
    defaults to docker for a real resume, local for a dry-run resume.
    """
    from .orchestrator import MissionConfig, MissionDriver

    cfg = MissionConfig(
        missions_root=_missions_root(),
        repo_path=repo.resolve(),
        router_config=(router_config or _default_router_config()).resolve(),
        goal="(resume)",
        dry_run=dry_run,
        sandbox_factory=_build_sandbox_factory(
            _resolve_sandbox(sandbox, dry_run=dry_run), sandbox_image
        ),
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


def cmd_metrics(
    *, missions_root: Path | None = None, markdown: bool = False
) -> dict[str, Any] | str:
    """Compute the G3 health-metric baseline across all missions under the root.

    Returns the rendered markdown when ``markdown`` is set, else the report dict.
    """
    from .metrics import compute_baseline, render_baseline_markdown

    root = missions_root or _missions_root()
    report = compute_baseline(root)
    if markdown:
        return render_baseline_markdown(report)
    return report.model_dump(mode="json")


def cmd_preflight(
    *,
    repo: Path | None = None,
    router_config: Path | None = None,
    sandbox: str = "docker",
    sandbox_image: str | None = None,
) -> dict[str, Any]:
    """Production-readiness gate: keys, router config, repo, Docker + image.

    Inspect-only (no LLM calls, no spend). Returns ``{"ok": bool, "checks":
    [...]}``. The Typer wrapper renders it and sets the process exit code.
    """
    from .orchestrator.preflight import DEFAULT_SANDBOX_IMAGE, run_preflight

    report = run_preflight(
        (router_config or _default_router_config()).resolve(),
        repo_path=repo,
        sandbox=sandbox,
        image=sandbox_image or DEFAULT_SANDBOX_IMAGE,
    )
    return {
        "ok": report.ok,
        "checks": [
            {
                "name": c.name,
                "status": c.status,
                "detail": c.detail,
                "remediation": c.remediation,
            }
            for c in report.checks
        ],
    }


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
        coder_provider: str | None = typer.Option(
            None,
            "--coder-provider",
            help="Override the Coder's provider for the 异-provider rule "
            "(e.g. 'anthropic'). Default: derived from the router's coder_worker model.",
        ),
        budget_usd: float | None = typer.Option(
            None,
            "--budget-usd",
            help="Full mission budget in USD, seeded into budget.yaml (the budget "
            "guard's ceiling). Default: the guard's built-in default.",
        ),
        sandbox: str | None = typer.Option(
            None,
            "--sandbox",
            help="Execution backend: 'docker' (isolated container) or 'local' "
            "(host shell, no isolation). Default: docker for a real run, local "
            "for a dry-run (which executes no agent code).",
        ),
        sandbox_image: str = typer.Option(
            _DEFAULT_SANDBOX_IMAGE,
            "--sandbox-image",
            help="Docker image for --sandbox docker (built by scripts/build_sandbox.sh).",
        ),
    ) -> None:
        result = cmd_mission_new(
            goal=goal,
            repo=repo,
            mission_id=mission_id,
            router_config=router_config,
            dry_run=dry_run,
            coder_provider=coder_provider,
            budget_usd=budget_usd,
            sandbox=sandbox,
            sandbox_image=sandbox_image,
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
        sandbox: str | None = typer.Option(
            None,
            "--sandbox",
            help="Execution backend; should match the mission's original backend "
            "('docker' or 'local'). Default: docker for a real resume, local for dry-run.",
        ),
        sandbox_image: str = typer.Option(
            _DEFAULT_SANDBOX_IMAGE,
            "--sandbox-image",
            help="Docker image for --sandbox docker.",
        ),
    ) -> None:
        result = cmd_resume(
            mission_id=mission_id,
            repo=repo,
            from_milestone=from_milestone,
            router_config=router_config,
            dry_run=dry_run,
            sandbox=sandbox,
            sandbox_image=sandbox_image,
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


    @app.command("metrics")
    def _metrics(
        missions_root: Path | None = typer.Option(
            None, "--missions-root", help="Missions root (defaults to $MAF_MISSIONS_ROOT or ./missions)."
        ),
        markdown: bool = typer.Option(
            False, "--markdown/--json", help="Render the baseline as markdown instead of JSON."
        ),
    ) -> None:
        result = cmd_metrics(missions_root=missions_root, markdown=markdown)
        typer.echo(result if isinstance(result, str) else json.dumps(result, indent=2))

    @app.command("preflight")
    def _preflight(
        repo: Path | None = typer.Option(
            None, "--repo", "-r", help="Target Rust repo to profile-check (optional)."
        ),
        router_config: Path | None = typer.Option(
            None, "--router-config", help="Path to droid_whispering.yaml."
        ),
        sandbox: str = typer.Option(
            "docker", "--sandbox", help="Backend to check readiness for: 'docker' or 'local'."
        ),
        sandbox_image: str | None = typer.Option(
            None, "--sandbox-image", help="Docker image to check for (default maf-coder:rust-sandbox)."
        ),
    ) -> None:
        """Production-readiness gate. Exits non-zero if any check fails."""
        result = cmd_preflight(
            repo=repo,
            router_config=router_config,
            sandbox=sandbox,
            sandbox_image=sandbox_image,
        )
        _sigil = {"pass": "✓", "fail": "✗", "warn": "!"}
        for c in result["checks"]:
            line = f"  {_sigil.get(c['status'], '?')} {c['name']}: {c['detail']}"
            if c["remediation"] and c["status"] != "pass":
                line += f"\n      → {c['remediation']}"
            typer.echo(line)
        if result["ok"]:
            typer.echo("\n✓ Preflight GO — ready for a real run.")
        else:
            typer.echo("\n✗ Preflight NO-GO — resolve the ✗ items above, then re-run.")
            raise typer.Exit(code=1)


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
    "cmd_metrics",
    "cmd_preflight",
    "cmd_mission_routing_stats",
    "cmd_mission_status",
    "cmd_pr",
    "cmd_resume",
    "cmd_rollback",
    "main",
]
