"""Research Worker tool factories (AGENT_TOOLS_SPEC §9).

The Research Worker is read-only on the codebase and the only role
allowed to make outbound HTTP. Every external fetch passes through
`sanitizer.sanitize()` before reaching the agent, and every request
(allowed or blocked) is logged via `EventLog.log_egress_request`.

Tool surface:
    fetch                 : fetch_url
    save (notes)          : save_research_note, save_code_map,
                            save_dependency_brief, save_workspace_overview
    inspect (read-only)   : cargo_metadata, cargo_tree, grep, glob
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ...sanitizer import sanitize
from .._sdk import function_tool
from ..base import TaskContext
from ..errors import ExternalContentError, ToolError
from ..permissions import (
    check_network_allowed,
    check_path_access,
    check_tool_allowed,
)
from ..results import CommandResult, GrepMatch, SanitizedContent
from . import record_tool_call, time_block

# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------

USER_AGENT = "maf-coder/Research-Worker"

_FetchFn = Callable[[str, int], tuple[str, str, int, str]]
"""Pluggable HTTP transport: (url, timeout_sec) -> (final_url, content_type, status, body)."""


def _http_get_default(url: str, timeout_sec: int) -> tuple[str, str, int, str]:
    """Default urllib-based fetcher. Returns (final_url, content_type, status, body)."""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    # Scheme is restricted to http/https by check_network_allowed (run in fetch_url
    # before this transport), so urlopen can't reach file:// / ftp:// / etc.
    with urlopen(req, timeout=timeout_sec) as resp:  # nosec B310
        body = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return (resp.geturl(), resp.headers.get_content_type() or "text/plain", resp.status, text)


def make_fetch_url(
    ctx: TaskContext,
    *,
    fetcher: _FetchFn | None = None,
    domain_whitelist: list[str] | None = None,
) -> Any:
    """Build the fetch_url tool. `fetcher` is injectable for testing."""
    transport = fetcher or _http_get_default

    @function_tool
    async def fetch_url(url: str, timeout_sec: int = 30) -> SanitizedContent:
        """HTTP GET an external URL, sanitize the response, return wrapped content.

        Permission: the task's `network_policy` decides which hosts are reachable.
        For `crates_only`, the allowlist is crates.io / docs.rs / github.com /
        *.github.io / raw.githubusercontent.com.

        Sanitizer:
          - Strips <script>, <style>, <iframe>, <object>, <embed>, <form>, <svg>,
            <canvas>, <noscript>
          - Strips zero-width / RLO / control characters
          - Flags known prompt-injection markers
          - Wraps the body in <external source="..." retrieved="..."> tags

        Emits `egress_request` (always) and `external_content_received` (on
        success) events on the mission log.
        """
        check_tool_allowed(ctx.task.permission, "fetch_url")
        try:
            check_network_allowed(ctx.task.permission, url, domain_whitelist)
        except ToolError:
            parsed = urlparse(url)
            ctx.event_log.log_egress_request(
                mission_id=ctx.mission_id,
                actor=_actor(ctx),
                url=url,
                domain=(parsed.hostname or "").lower(),
                blocked_reason="permission-denied",
                task_id=ctx.task.task_id,
            )
            raise

        t0 = time_block()
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        try:
            final_url, content_type, status, body = transport(url, timeout_sec)
        except Exception as e:
            ctx.event_log.log_egress_request(
                mission_id=ctx.mission_id,
                actor=_actor(ctx),
                url=url,
                domain=domain,
                blocked_reason=f"fetch-error: {type(e).__name__}",
                task_id=ctx.task.task_id,
            )
            raise ExternalContentError(f"fetch_url({url}) failed: {e}") from e

        sanitized = sanitize(
            raw=body,
            content_type=content_type,
            original_url=url,
            final_url=final_url,
        )

        ctx.event_log.log_egress_request(
            mission_id=ctx.mission_id,
            actor=_actor(ctx),
            url=url,
            domain=domain,
            status_code=status,
            bytes_received=len(body),
            task_id=ctx.task.task_id,
        )
        ctx.event_log.log_external_content_received(
            mission_id=ctx.mission_id,
            actor=_actor(ctx),
            original_url=url,
            final_url=final_url,
            content_type=content_type,
            sanitization_actions=list(sanitized.sanitization_actions),
            task_id=ctx.task.task_id,
        )
        record_tool_call(
            ctx,
            "fetch_url",
            f"url={url} status={status} bytes={len(body)}",
            exit_code=0,
            duration_sec=time_block() - t0,
        )
        return sanitized

    return fetch_url


def _actor(ctx: TaskContext) -> str:
    owner = ctx.task.owner
    return owner.value if hasattr(owner, "value") else str(owner)


# ---------------------------------------------------------------------------
# Save-note tools
# ---------------------------------------------------------------------------

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _validate_slug(slug: str, *, field_name: str) -> None:
    if not _KEBAB_RE.match(slug):
        raise ToolError(
            f"{field_name}={slug!r} must be kebab-case (lowercase alphanumerics + hyphens)"
        )


def _save_markdown(ctx: TaskContext, *, relpath: str, content: str, tool_name: str) -> str:
    try:
        ctx.store.write_text(relpath, content)
    except Exception as e:
        raise ToolError(f"{tool_name}: store rejected: {e}") from e
    record_tool_call(ctx, tool_name, f"path={relpath} bytes={len(content)}")
    ctx.event_log.log_artifact_written(
        mission_id=ctx.mission_id,
        actor=_actor(ctx),
        path=relpath,
        task_id=ctx.task.task_id,
    )
    return relpath


def make_save_research_note(ctx: TaskContext) -> Any:
    @function_tool
    async def save_research_note(topic: str, content_markdown: str) -> str:
        """Save research_notes/<topic>.md.

        `topic` MUST be kebab-case (e.g. 'axum-routing', 'tokio-vs-async-std').
        Content MUST be Research Worker's own synthesis — never raw HTML/JSON
        (soul.md §7.3); code snippets from external sources must be rewritten
        and attributed with "based on <url>".
        """
        check_tool_allowed(ctx.task.permission, "save_research_note")
        _validate_slug(topic, field_name="topic")
        relpath = f"research_notes/{topic}.md"
        return _save_markdown(
            ctx, relpath=relpath, content=content_markdown, tool_name="save_research_note"
        )

    return save_research_note


def make_save_code_map(ctx: TaskContext) -> Any:
    @function_tool
    async def save_code_map(module: str, content_markdown: str) -> str:
        """Save code_map/<module>.md.

        `module` MUST be kebab-case and match the module being mapped.
        Content lists modules/functions/types with one-line summaries.
        """
        check_tool_allowed(ctx.task.permission, "save_code_map")
        _validate_slug(module, field_name="module")
        relpath = f"code_map/{module}.md"
        return _save_markdown(
            ctx, relpath=relpath, content=content_markdown, tool_name="save_code_map"
        )

    return save_code_map


def make_save_dependency_brief(ctx: TaskContext) -> Any:
    @function_tool
    async def save_dependency_brief(content_markdown: str) -> str:
        """Save dependency_brief.md — top-level dependency snapshot."""
        check_tool_allowed(ctx.task.permission, "save_dependency_brief")
        return _save_markdown(
            ctx,
            relpath="dependency_brief.md",
            content=content_markdown,
            tool_name="save_dependency_brief",
        )

    return save_dependency_brief


def make_save_workspace_overview(ctx: TaskContext) -> Any:
    @function_tool
    async def save_workspace_overview(content_markdown: str) -> str:
        """Save workspace_overview.md — top-level workspace layout summary."""
        check_tool_allowed(ctx.task.permission, "save_workspace_overview")
        return _save_markdown(
            ctx,
            relpath="workspace_overview.md",
            content=content_markdown,
            tool_name="save_workspace_overview",
        )

    return save_workspace_overview


# ---------------------------------------------------------------------------
# Cargo / inspection tools (read-only — Research Worker doesn't mutate)
# ---------------------------------------------------------------------------


def make_cargo_metadata(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_metadata() -> dict[str, Any]:
        """Run `cargo metadata --format-version 1` and return parsed JSON."""
        check_tool_allowed(ctx.task.permission, "cargo_metadata")
        t0 = time_block()
        res = await ctx.sandbox.exec(
            "cargo metadata --format-version 1 --no-deps",
            cwd="/workspace",
            timeout_sec=120,
        )
        record_tool_call(
            ctx,
            "cargo_metadata",
            "",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        if res.exit_code != 0:
            raise ToolError(f"cargo metadata failed: {res.stderr[:500]}")
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as e:
            raise ToolError(f"cargo metadata output not JSON: {e}") from e
        if not isinstance(data, dict):
            raise ToolError("cargo metadata output is not a JSON object")
        return data

    return cargo_metadata


def make_cargo_tree(ctx: TaskContext) -> Any:
    @function_tool
    async def cargo_tree(args: list[str] | None = None) -> CommandResult:
        """Run `cargo tree` with optional args (e.g. ['--edges', 'normal'])."""
        check_tool_allowed(ctx.task.permission, "cargo_tree")
        rest = " ".join(args or [])
        cmd = f"cargo tree {rest}".strip()
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=120)
        record_tool_call(
            ctx,
            "cargo_tree",
            f"args={rest}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return res

    return cargo_tree


# ---------------------------------------------------------------------------
# grep / glob — these are read tools, so they go through check_path_access
# for the base directory (relative paths only).
# ---------------------------------------------------------------------------


def make_grep(ctx: TaskContext) -> Any:
    @function_tool
    async def grep(
        pattern: str,
        paths: list[str] | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
    ) -> list[GrepMatch]:
        """Run ripgrep over the worktree. Returns parsed match records.

        Falls back to `grep -rn` if ripgrep is not available. Paths default
        to ["."].
        """
        check_tool_allowed(ctx.task.permission, "grep")
        for p in paths or ["."]:
            check_path_access(ctx.task.permission, p, mode="read")
        flags = ["-n", "--json"]
        if case_insensitive:
            flags.append("-i")
        if context_lines > 0:
            flags.extend(["-C", str(context_lines)])
        path_args = " ".join(paths or ["."])
        rg_cmd = f"rg {' '.join(flags)} -- {_shell_quote(pattern)} {path_args}"

        t0 = time_block()
        res = await ctx.sandbox.exec(rg_cmd, cwd="/workspace", timeout_sec=60)
        record_tool_call(
            ctx,
            "grep",
            f"pattern={pattern!r} paths={paths}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        # ripgrep exit codes: 0 = matches, 1 = no matches, 2 = error
        if res.exit_code == 1:
            return []
        if res.exit_code == 2:
            raise ToolError(f"grep failed: {res.stderr[:500]}")
        return _parse_rg_json(res.stdout)

    return grep


def _shell_quote(s: str) -> str:
    """POSIX-safe single-quote escape for a single argv."""
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_rg_json(stdout: str) -> list[GrepMatch]:
    """Parse `rg --json` stream into GrepMatch records. Tolerant of partial output."""
    matches: list[GrepMatch] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "match":
            continue
        data = obj.get("data") or {}
        path_obj = data.get("path") or {}
        line_obj = data.get("lines") or {}
        line_no = data.get("line_number")
        path = path_obj.get("text", "")
        text = line_obj.get("text", "").rstrip("\n")
        if not path or line_no is None:
            continue
        matches.append(GrepMatch(path=path, line_number=int(line_no), line=text))
    return matches


def make_glob(ctx: TaskContext) -> Any:
    @function_tool
    async def glob(pattern: str, cwd: str = ".") -> list[str]:
        """Return paths matching the glob pattern, relative to the worktree.

        Uses `git ls-files` + fnmatch — respects .gitignore so generated
        files (target/, .venv/) don't pollute results.
        """
        check_tool_allowed(ctx.task.permission, "glob")
        check_path_access(ctx.task.permission, cwd, mode="read")
        cmd = f"git ls-files -- {cwd}"
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=60)
        record_tool_call(
            ctx,
            "glob",
            f"pattern={pattern} cwd={cwd}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        if res.exit_code != 0:
            raise ToolError(f"git ls-files failed: {res.stderr[:200]}")
        return [p for p in res.stdout.splitlines() if fnmatch.fnmatch(p, pattern)]

    return glob


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_research_tools(
    ctx: TaskContext,
    *,
    fetcher: _FetchFn | None = None,
    domain_whitelist: list[str] | None = None,
) -> list[Any]:
    """Build the full set of Research Worker tools bound to `ctx`.

    `fetcher` and `domain_whitelist` are optional injection points used by
    tests; production callers should leave them unset.
    """
    return [
        make_fetch_url(ctx, fetcher=fetcher, domain_whitelist=domain_whitelist),
        make_save_research_note(ctx),
        make_save_code_map(ctx),
        make_save_dependency_brief(ctx),
        make_save_workspace_overview(ctx),
        make_cargo_metadata(ctx),
        make_cargo_tree(ctx),
        make_grep(ctx),
        make_glob(ctx),
    ]


__all__ = [
    "build_research_tools",
    "make_cargo_metadata",
    "make_cargo_tree",
    "make_fetch_url",
    "make_glob",
    "make_grep",
    "make_save_code_map",
    "make_save_dependency_brief",
    "make_save_research_note",
    "make_save_workspace_overview",
]
