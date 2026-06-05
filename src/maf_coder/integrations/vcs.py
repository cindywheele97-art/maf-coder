"""VCS / PR workflow (Build Plan §Phase F · F5).

This module is the mission-end PR workflow. It does three disjoint jobs:

1. **gh / glab CLI wrappers** — thin async functions that *compose* a
   ``gh pr create`` / ``glab mr create`` command and run it through
   ``sandbox.exec`` (never the host shell), mirroring how ``security_tools``
   wraps gitleaks: capture stdout/stderr/exit, parse the PR URL from stdout.

2. **PR-description generation** — assemble a sectioned PR body from the
   mission's artifacts (goal, changes, validation verdicts, cost record) plus
   an auto-link to the mission artifact directory. The Build Plan §9.2 template
   is a forward reference with no literal body, so we use a clean sectioned
   structure: Summary / Changes / Validation / Cost / Artifacts.

3. **gitleaks pre-PR gate** — REUSE the existing ``make_gitleaks_detect`` tool
   (``security_tools``) as a final secret scan before opening the PR. If any
   secret is found the PR is REFUSED and the finding is surfaced; gitleaks is
   never reimplemented here.

``create_pull_request`` chains gate → wrapper and returns a ``PullRequestResult``.
Everything is dependency-injected (``ctx`` carries the sandbox + store + event
log), so tests stub ``sandbox.exec`` and never touch a real network.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from ..agents.results import CommandResult
from ..agents.tools.security_tools import make_gitleaks_detect
from ..schemas.pr import PullRequestResult, PullRequestSpec, VcsProvider

if TYPE_CHECKING:
    from ..agents.base import TaskContext

# A PR/MR URL looks like https://github.com/<o>/<r>/pull/<n> or
# https://gitlab.com/<g>/<p>/-/merge_requests/<n>. gh/glab print it on stdout;
# we grab the first http(s) URL token rather than over-fitting to one host.
_URL_RE = re.compile(r"https?://\S+")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitleaksGateError(Exception):
    """Raised internally when the gitleaks pre-PR gate finds secrets.

    Surfaced to callers as a ``PullRequestResult(refused=True)`` — this
    exception only flows inside ``create_pull_request`` so the gate and the
    wrapper stay independently testable.
    """

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = findings
        super().__init__(f"gitleaks found {len(findings)} secret(s); refusing PR")


class GitleaksUnavailableError(Exception):
    """Raised when the gitleaks secret-scan gate cannot run (binary absent).

    A gate that cannot execute MUST fail closed (H2): treating "tool missing"
    as "no secrets" would silently let a PR open with secrets in it. Surfaced
    to callers as a ``PullRequestResult(refused=True)``.
    """


# ---------------------------------------------------------------------------
# Artifact-link builder + PR-description generation
# ---------------------------------------------------------------------------


def build_artifact_links(store: Any) -> list[str]:
    """Collect mission-relative artifact paths worth linking from the PR body.

    Probes the well-known top-level artifacts and the verdicts/ directory. Only
    paths that exist are returned, so the body never links to a missing file.
    The mission artifact directory itself is always first.
    """
    links: list[str] = ["."]  # mission artifact directory root
    for rel in ("plan.md", "validation_contract.yaml", "mission_retro.md"):
        try:
            if store.exists(rel):
                links.append(rel)
        except Exception:  # pragma: no cover - defensive
            continue
    try:
        for p in store.list_dir("verdicts"):
            if p.suffix == ".json":
                links.append(f"verdicts/{p.name}")
    except Exception:  # pragma: no cover - defensive
        pass
    return links


def _read_text_or(store: Any, rel: str, default: str) -> str:
    try:
        if store.exists(rel):
            return store.read_text(rel).strip() or default
    except Exception:  # pragma: no cover - defensive
        pass
    return default


def _validation_lines(store: Any) -> list[str]:
    """Summarize review/behavior verdicts as one bullet per verdict file."""
    lines: list[str] = []
    try:
        verdict_files = store.list_dir("verdicts")
    except Exception:  # pragma: no cover - defensive
        verdict_files = []
    for p in sorted(verdict_files):
        if p.suffix != ".json":
            continue
        try:
            data = store.read_json(f"verdicts/{p.name}")
        except Exception:  # pragma: no cover - defensive
            continue
        if not isinstance(data, dict):
            continue
        kind = "review" if p.name.endswith(".review.json") else (
            "behavior" if p.name.endswith(".behavior.json") else "security"
        )
        result = data.get("result")
        if result is not None:
            lines.append(f"- `{p.name}` ({kind}): **{result}**")
        else:
            # Security verdict has no top-level result; report finding counts.
            findings = data.get("findings") or []
            lines.append(f"- `{p.name}` ({kind}): {len(findings)} finding(s)")
    if not lines:
        lines.append("- No validator verdicts recorded for this mission.")
    return lines


def _cost_lines(event_log: Any) -> list[str]:
    """Render the mission cost record from the EventLog, defensively."""
    try:
        cost = event_log.total_cost_usd()
        tin, tout = event_log.total_tokens()
        return [
            f"- Cost: ${cost:.2f}",
            f"- Tokens: {tin + tout:,} ({tin:,} in / {tout:,} out)",
        ]
    except Exception:  # pragma: no cover - defensive
        return ["- Cost record unavailable."]


def render_pr_body(
    *,
    mission_id: str,
    store: Any,
    event_log: Any,
    goal: str | None = None,
    artifact_links: list[str] | None = None,
) -> str:
    """Assemble the PR description from mission artifacts.

    Sections: Summary / Changes / Validation / Cost / Artifacts. The Artifacts
    section auto-links the mission directory + every collected artifact path.
    `goal` overrides the goal line; otherwise it is read from plan.md's first
    non-empty line, falling back to a placeholder.
    """
    links = artifact_links if artifact_links is not None else build_artifact_links(store)

    if goal is None:
        plan = _read_text_or(store, "plan.md", "")
        goal = next((ln.lstrip("# ").strip() for ln in plan.splitlines() if ln.strip()), "")
    summary = goal or "_Mission goal not recorded._"

    changes = _read_text_or(
        store,
        "final_answer.md",
        "_See linked handoffs and patches under the mission artifact directory._",
    )

    parts: list[str] = []
    parts.append(f"## Summary\n\n{summary}\n")
    parts.append(f"## Changes\n\n{changes}\n")
    parts.append("## Validation\n\n" + "\n".join(_validation_lines(store)) + "\n")
    parts.append("## Cost\n\n" + "\n".join(_cost_lines(event_log)) + "\n")
    artifact_block = "\n".join(
        f"- `{link}`" if link != "." else f"- Mission artifacts: `missions/{mission_id}/`"
        for link in links
    )
    parts.append("## Artifacts\n\n" + artifact_block + "\n")
    parts.append(f"\n---\n_Generated by MAF-Coder for mission `{mission_id}`._\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# gh / glab command composition + wrapper
# ---------------------------------------------------------------------------


def compose_pr_command(spec: PullRequestSpec) -> str:
    """Compose the provider-specific create command for `spec`.

    Returns a single shell string (quoted via shlex) to hand to sandbox.exec.
    gh:   ``gh pr create --title T --body B --base base --head head [--draft]``
    glab: ``glab mr create --title T --description B --target-branch base
            --source-branch head [--draft]``
    """
    # `use_enum_values=True` stores provider as its string value at runtime;
    # normalize to the enum so this stays correct whether a str or enum is set.
    provider = VcsProvider(spec.provider)
    title = shlex.quote(spec.title)
    body = shlex.quote(spec.body)
    base = shlex.quote(spec.base_branch)
    head = shlex.quote(spec.head_branch)
    if provider is VcsProvider.GH:
        cmd = f"gh pr create --title {title} --body {body} --base {base} --head {head}"
        if spec.draft:
            cmd += " --draft"
        return cmd
    if provider is VcsProvider.GLAB:
        cmd = (
            f"glab mr create --title {title} --description {body} "
            f"--target-branch {base} --source-branch {head}"
        )
        if spec.draft:
            cmd += " --draft"
        return cmd
    raise ValueError(f"unsupported provider: {spec.provider!r}")


def _parse_pr_url(res: CommandResult) -> str | None:
    """Extract the first http(s) URL printed by gh/glab (stdout, then stderr)."""
    for stream in (res.stdout, res.stderr):
        match = _URL_RE.search(stream or "")
        if match:
            return match.group(0).rstrip(".,)")
    return None


async def run_vcs_create(
    ctx: TaskContext,
    spec: PullRequestSpec,
    *,
    timeout_sec: int = 120,
) -> PullRequestResult:
    """Run the composed gh/glab create command through the sandbox.

    Mirrors security_tools: non-zero exit codes are returned, not raised. On
    success the PR URL is parsed from stdout. This wrapper does NOT run the
    gitleaks gate — `create_pull_request` chains them — so the wrapper stays
    independently testable.
    """
    cmd = compose_pr_command(spec)
    res = await ctx.sandbox.exec(cmd, cwd=spec.repo_path, timeout_sec=timeout_sec)
    url = _parse_pr_url(res) if res.exit_code == 0 else None
    return PullRequestResult(
        mission_id=spec.mission_id,
        provider=VcsProvider(spec.provider),
        created=res.exit_code == 0 and url is not None,
        url=url,
        command=cmd,
        exit_code=res.exit_code,
        stderr=res.stderr or None,
        artifact_links=list(spec.artifact_links),
    )


# ---------------------------------------------------------------------------
# gitleaks pre-PR gate (REUSE the existing tool — do not reimplement)
# ---------------------------------------------------------------------------


async def run_gitleaks_gate(ctx: TaskContext, *, path: str = ".") -> list[dict[str, Any]]:
    """Run the EXISTING gitleaks_detect tool as a pre-PR secret scan.

    Returns the list of findings (empty => clean). Reuses
    ``make_gitleaks_detect`` so the gitleaks invocation and the JSON parsing
    stay in one place.

    Fails closed (H2): if gitleaks is not installed in the sandbox, raises
    ``GitleaksUnavailableError`` instead of degrading to "clean". A secret-scan
    gate that cannot run must refuse the PR, not silently pass it.
    """
    gitleaks_detect = make_gitleaks_detect(ctx)
    result = await gitleaks_detect(path)
    if result.get("installed") is False:
        note = str(result.get("note") or "gitleaks binary not installed in sandbox")
        raise GitleaksUnavailableError(note)
    findings = result.get("findings")
    if isinstance(findings, list):
        return findings
    return []


# ---------------------------------------------------------------------------
# Orchestration: gate → wrapper
# ---------------------------------------------------------------------------


async def create_pull_request(
    ctx: TaskContext,
    spec: PullRequestSpec,
    *,
    scan_path: str = ".",
    timeout_sec: int = 120,
) -> PullRequestResult:
    """Mission-end create-PR action: gitleaks gate, then gh/glab wrapper.

    If gitleaks finds secrets the PR is REFUSED (``refused=True``) with the
    findings surfaced and no CLI command run. If gitleaks cannot run at all
    (binary absent), the gate fails closed and the PR is likewise REFUSED (H2).
    Otherwise the composed create command runs and the parsed PR URL is returned.
    """
    try:
        findings = await run_gitleaks_gate(ctx, path=scan_path)
    except GitleaksUnavailableError as e:
        return PullRequestResult(
            mission_id=spec.mission_id,
            provider=VcsProvider(spec.provider),
            created=False,
            refused=True,
            refusal_reason=(
                f"gitleaks secret-scan gate could not run ({e}); refusing to open PR. "
                "Install gitleaks in the sandbox image and re-run."
            ),
            gitleaks_findings=[],
            artifact_links=list(spec.artifact_links),
        )
    if findings:
        return PullRequestResult(
            mission_id=spec.mission_id,
            provider=VcsProvider(spec.provider),
            created=False,
            refused=True,
            refusal_reason=(
                f"gitleaks found {len(findings)} secret(s); refusing to open PR. "
                "Remove the secret(s) and re-run."
            ),
            gitleaks_findings=findings,
            artifact_links=list(spec.artifact_links),
        )
    return await run_vcs_create(ctx, spec, timeout_sec=timeout_sec)


__all__ = [
    "GitleaksGateError",
    "GitleaksUnavailableError",
    "build_artifact_links",
    "compose_pr_command",
    "create_pull_request",
    "render_pr_body",
    "run_gitleaks_gate",
    "run_vcs_create",
]
