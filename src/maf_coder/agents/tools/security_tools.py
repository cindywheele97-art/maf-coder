"""Security Worker tool factories (AGENT_TOOLS_SPEC §10).

Read-only on the codebase. Each cargo / gitleaks / trufflehog invocation
runs in the sandbox; missing tools degrade gracefully (the agent sees
`installed=False` and `note=…` rather than an exception) so a project
that doesn't ship every audit tool still produces a partial verdict.

Tool surface:
    cargo                 : cargo_audit, cargo_deny_check, cargo_geiger
    secrets               : gitleaks_detect, trufflehog_scan
    output                : save_security_verdict, save_security_notes
"""

from __future__ import annotations

import json
from typing import Any

from ...schemas import Severity
from ...schemas.verdict import SecurityFinding, SecurityVerdict
from .._sdk import function_tool
from ..base import TaskContext
from ..errors import ArtifactError, ToolError
from ..permissions import check_tool_allowed
from ..results import CommandResult
from . import record_tool_call, time_block

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _actor(ctx: TaskContext) -> str:
    owner = ctx.task.owner
    return owner.value if hasattr(owner, "value") else str(owner)


def _is_missing(res: CommandResult) -> bool:
    """Return True iff the command failed because the binary isn't installed.

    127 is the canonical "command not found" exit code on POSIX, but some
    sandbox shells emit other codes — also match the textual marker.
    """
    if res.exit_code == 127:
        return True
    haystack = (res.stderr + " " + res.stdout).lower()
    return "command not found" in haystack or "no such file" in haystack


def _summarize_result(
    ctx: TaskContext,
    *,
    tool_name: str,
    sandbox_cmd: str,
    res: CommandResult,
    parse_json: bool,
    t0: float,
) -> dict[str, Any]:
    """Shape a CommandResult into the standard audit-tool return dict.

    Returns a dict with:
      - installed: bool
      - exit_code: int
      - findings: list[dict] | None       (parsed JSON; None if not JSON-shaped)
      - raw_output: str                    (truncated stdout, for fallback display)
      - note: str                          (free-form explanation when degraded)
    """
    duration = time_block() - t0
    record_tool_call(ctx, tool_name, sandbox_cmd, exit_code=res.exit_code, duration_sec=duration)

    if _is_missing(res):
        return {
            "installed": False,
            "exit_code": res.exit_code,
            "findings": None,
            "raw_output": "",
            "note": f"{tool_name} binary not installed in sandbox; skipping",
        }

    findings: list[dict[str, Any]] | None = None
    if parse_json and res.stdout.strip():
        try:
            data = json.loads(res.stdout)
            if isinstance(data, list):
                findings = data
            elif isinstance(data, dict):
                # Some tools nest findings under "vulnerabilities" / "results" / "advisories"
                for key in ("vulnerabilities", "results", "advisories", "findings"):
                    inner = data.get(key)
                    if isinstance(inner, list):
                        findings = inner
                        break
                if findings is None and "data" in data:
                    findings = []
        except json.JSONDecodeError:
            findings = None

    return {
        "installed": True,
        "exit_code": res.exit_code,
        "findings": findings,
        "raw_output": res.stdout[:8000],
        "note": (
            ""
            if res.exit_code == 0
            else f"{tool_name} exited with code {res.exit_code}; see raw_output / stderr"
        ),
    }


# ---------------------------------------------------------------------------
# Cargo-based scanners
# ---------------------------------------------------------------------------


async def _run_and_summarize(
    ctx: TaskContext,
    *,
    tool_name: str,
    sandbox_cmd: str,
    timeout_sec: int = 180,
    parse_json: bool = True,
) -> dict[str, Any]:
    check_tool_allowed(ctx.task.permission, tool_name)
    t0 = time_block()
    res = await ctx.sandbox.exec(sandbox_cmd, cwd="/workspace", timeout_sec=timeout_sec)
    return _summarize_result(
        ctx,
        tool_name=tool_name,
        sandbox_cmd=sandbox_cmd,
        res=res,
        parse_json=parse_json,
        t0=t0,
    )


def make_cargo_audit(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_audit() -> dict[str, Any]:
        """Run `cargo audit --json`, return parsed findings.

        Degrades gracefully when cargo-audit isn't installed (returns
        installed=False with a note).
        """
        return await _run_and_summarize(
            ctx, tool_name="cargo_audit", sandbox_cmd="cargo audit --json"
        )

    return cargo_audit


def make_cargo_deny_check(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_deny_check() -> dict[str, Any]:
        """Run `cargo deny check --format json`, return parsed findings."""
        return await _run_and_summarize(
            ctx,
            tool_name="cargo_deny_check",
            sandbox_cmd="cargo deny check --format json",
        )

    return cargo_deny_check


def make_cargo_geiger(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_geiger() -> dict[str, Any]:
        """Run `cargo geiger --output-format Json` — counts of `unsafe` per crate."""
        return await _run_and_summarize(
            ctx,
            tool_name="cargo_geiger",
            sandbox_cmd="cargo geiger --output-format Json",
            timeout_sec=240,
        )

    return cargo_geiger


# ---------------------------------------------------------------------------
# Secret scanners
# ---------------------------------------------------------------------------


def make_gitleaks_detect(ctx: TaskContext) -> Any:
    @function_tool
    async def gitleaks_detect(path: str = ".") -> dict[str, Any]:
        """Run `gitleaks detect --report-format json --report-path -`."""
        cmd = (
            f"gitleaks detect --no-banner --report-format json "
            f"--report-path /dev/stdout --source {path}"
        )
        return await _run_and_summarize(ctx, tool_name="gitleaks_detect", sandbox_cmd=cmd)

    return gitleaks_detect


def make_trufflehog_scan(ctx: TaskContext) -> Any:
    @function_tool
    async def trufflehog_scan(path: str = ".") -> dict[str, Any]:
        """Run `trufflehog filesystem --json` on `path`."""
        cmd = f"trufflehog filesystem --json {path}"
        return await _run_and_summarize(ctx, tool_name="trufflehog_scan", sandbox_cmd=cmd)

    return trufflehog_scan


# ---------------------------------------------------------------------------
# Output: save verdict + free-form notes
# ---------------------------------------------------------------------------


_ALLOWED_CATEGORIES = {"audit", "deny", "geiger", "secret", "unsafe", "license"}


def _coerce_findings(raw: list[dict[str, Any]]) -> list[SecurityFinding]:
    out: list[SecurityFinding] = []
    for i, f in enumerate(raw):
        sev = (f.get("severity") or "low").lower()
        try:
            severity = Severity(sev)
        except ValueError as e:
            raise ToolError(
                f"finding[{i}].severity={sev!r} must be one of {[s.value for s in Severity]}"
            ) from e
        category = (f.get("category") or "audit").lower()
        if category not in _ALLOWED_CATEGORIES:
            raise ToolError(
                f"finding[{i}].category={category!r} must be one of {sorted(_ALLOWED_CATEGORIES)}"
            )
        description = f.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ToolError(f"finding[{i}].description must be a non-empty string")
        out.append(
            SecurityFinding(
                severity=severity,
                category=category,
                description=description,
                location=f.get("location"),
                suggestion=f.get("suggestion"),
            )
        )
    return out


def make_save_security_verdict(ctx: TaskContext) -> Any:
    @function_tool
    async def save_security_verdict(
        task_id: str,
        findings: list[dict[str, Any]],
    ) -> str:
        """Save verdicts/<task_id>.security.json.

        `findings` is a list of dicts with keys:
          severity   : 'critical' | 'high' | 'medium' | 'low'
          category   : 'audit' | 'deny' | 'geiger' | 'secret' | 'unsafe' | 'license'
          description: non-empty string
          location   : optional 'file:line' or crate name
          suggestion : optional remediation hint

        `blocks_pr` is derived from severity counts — do not pass it.
        """
        check_tool_allowed(ctx.task.permission, "save_security_verdict")
        try:
            coerced = _coerce_findings(findings)
            verdict = SecurityVerdict(task_id=task_id, findings=coerced)
        except ToolError:
            raise
        except Exception as e:
            raise ArtifactError(f"save_security_verdict: validation failed: {e}") from e

        try:
            path = ctx.store.save_security_verdict(task_id, verdict)
        except Exception as e:
            raise ArtifactError(f"save_security_verdict: store rejected: {e}") from e
        record_tool_call(
            ctx,
            "save_security_verdict",
            f"task_id={task_id} findings={len(findings)} blocks_pr={verdict.blocks_pr}",
        )
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor=_actor(ctx),
            path=str(path),
            task_id=task_id,
        )
        for f in coerced:
            ctx.event_log.log_security_finding(
                mission_id=ctx.mission_id,
                severity=f.severity if isinstance(f.severity, str) else f.severity.value,
                category=f.category,
                description=f.description,
                task_id=task_id,
            )
        return str(path)

    return save_security_verdict


def make_save_security_notes(ctx: TaskContext) -> Any:
    @function_tool
    async def save_security_notes(task_id: str, content_markdown: str) -> str:
        """Save security_notes/<task_id>.md — human-readable companion to the JSON verdict."""
        check_tool_allowed(ctx.task.permission, "save_security_notes")
        relpath = f"security_notes/{task_id}.md"
        try:
            ctx.store.write_text(relpath, content_markdown)
        except Exception as e:
            raise ArtifactError(f"save_security_notes: {e}") from e
        record_tool_call(
            ctx,
            "save_security_notes",
            f"task_id={task_id} bytes={len(content_markdown)}",
        )
        ctx.event_log.log_artifact_written(
            mission_id=ctx.mission_id,
            actor=_actor(ctx),
            path=relpath,
            task_id=task_id,
        )
        return relpath

    return save_security_notes


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_security_tools(ctx: TaskContext) -> list[Any]:
    """Build the full set of Security Worker tools bound to `ctx`."""
    return [
        make_cargo_audit(ctx),
        make_cargo_deny_check(ctx),
        make_cargo_geiger(ctx),
        make_gitleaks_detect(ctx),
        make_trufflehog_scan(ctx),
        make_save_security_verdict(ctx),
        make_save_security_notes(ctx),
    ]


__all__ = [
    "build_security_tools",
    "make_cargo_audit",
    "make_cargo_deny_check",
    "make_cargo_geiger",
    "make_gitleaks_detect",
    "make_save_security_notes",
    "make_save_security_verdict",
    "make_trufflehog_scan",
]
