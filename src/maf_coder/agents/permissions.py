"""Permission enforcement layer (AGENT_TOOLS_SPEC §5).

The single security choke point between LLM-driven tool calls and system
effects. Every tool function calls one or more of these helpers BEFORE doing
anything that touches the filesystem, the sandbox, or the network.

Design rules:
- This module never reads `task.permission` directly from a Task — callers
  pass the `Permission` object. That makes the helpers trivially unit-
  testable without constructing a full Task.
- All helpers raise `PermissionDeniedError` with `what` + `why` so the agent
  sees a structured denial reason it can reason about.
- Path normalization is POSIX-style (Rust sandbox is Linux-only). Windows
  callers normalize before calling.
- Wildcards in `allowed_paths` / `allowed_tools` use `fnmatch` glob syntax
  (i.e. `cargo_*` matches `cargo_test` and `cargo_check`).
"""
from __future__ import annotations

import fnmatch
import re
from typing import Literal
from urllib.parse import urlparse

from ..schemas import NetworkPolicy, Permission
from .errors import PermissionDeniedError

PathMode = Literal["read", "write"]


# ---------------------------------------------------------------------------
# Path traversal: reject anything that escapes the sandbox root via .. parts
# ---------------------------------------------------------------------------


def _normalize_relpath(path: str) -> str:
    """Collapse "./" and reject ".." escapes.

    Returns a clean POSIX-style relative path. Raises PermissionDeniedError
    if the path contains any traversal segment (".."), is empty, or uses an
    absolute Windows-style prefix.
    """
    if not path:
        raise PermissionDeniedError(path, "empty path")
    if re.match(r"^[A-Za-z]:[\\/]", path):
        raise PermissionDeniedError(path, "Windows-style absolute path not allowed")

    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PermissionDeniedError(
            path, "path traversal (..) not allowed"
        )
    if path.startswith("/"):
        return "/" + "/".join(parts)
    return "/".join(parts) or "."


def _path_matches_any(path: str, patterns: list[str]) -> bool:
    """Match `path` against any glob in `patterns`.

    A pattern is treated as a directory prefix if it ends with `/` or `/**`;
    otherwise as an `fnmatch` glob. Both `path` and `patterns` are POSIX-style
    relative paths (the canonical form coming out of _normalize_relpath).
    """
    for pat in patterns:
        if pat in ("**", "*", ".", "./"):
            return True
        if pat.endswith("/**"):
            prefix = pat[:-3].rstrip("/")
            if path == prefix or path.startswith(prefix + "/"):
                return True
        if pat.endswith("/"):
            if path == pat[:-1] or path.startswith(pat):
                return True
        if fnmatch.fnmatch(path, pat):
            return True
        # Also try "prefix" match for patterns without explicit glob suffix
        if "/" not in pat and "*" not in pat and path.startswith(pat + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Public checks
# ---------------------------------------------------------------------------


def check_path_access(
    permission: Permission,
    path: str,
    mode: PathMode,
) -> None:
    """Raise PermissionDeniedError if `path` is not allowed under `permission`.

    Rules:
      1. Path must normalize cleanly (no `..` traversal).
      2. If `allowed_paths` is empty: sandbox-default applies. For read this
         means anything under the sandbox worktree is allowed; for write it
         means *nothing* is allowed (caller must explicitly grant write paths).
      3. If `allowed_paths` is non-empty: the normalized path must match one
         of the patterns.
    """
    norm = _normalize_relpath(path)
    allowed = list(permission.allowed_paths)

    if not allowed:
        if mode == "write":
            raise PermissionDeniedError(
                norm,
                "no write paths declared on task; permission.allowed_paths is empty",
            )
        return  # sandbox-default read OK

    if not _path_matches_any(norm, allowed):
        raise PermissionDeniedError(
            norm,
            f"path not in allowed_paths={allowed} (mode={mode})",
        )


def check_tool_allowed(permission: Permission, tool_name: str) -> None:
    """Raise PermissionDeniedError if `tool_name` is not in `permission.allowed_tools`.

    Empty `allowed_tools` means "no per-task restriction" and the call passes
    (the SDK tool registry already constrains which tools the agent can see).
    Wildcards are honored: 'cargo_*' allows 'cargo_test', 'cargo_check', etc.
    """
    if not permission.allowed_tools:
        return
    if any(fnmatch.fnmatch(tool_name, pat) for pat in permission.allowed_tools):
        return
    raise PermissionDeniedError(
        tool_name,
        f"tool not in allowed_tools={list(permission.allowed_tools)}",
    )


_CRATES_ONLY_HOSTS = {
    "crates.io",
    "static.crates.io",
    "docs.rs",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
}


def check_network_allowed(
    permission: Permission,
    url: str,
    domain_whitelist: list[str] | None = None,
) -> None:
    """Raise PermissionDeniedError if outbound HTTP not allowed for this task.

    NetworkPolicy values:
      - NONE: deny everything
      - CRATES_ONLY: allow crates.io / docs.rs / github.com (+ *.github.io)
      - WHITELIST: allow only the domains in `domain_whitelist`
      - OPEN: allow everything (still subject to the global denylist of
        link-local / private RFC-1918 hosts to prevent SSRF)
    """
    policy = permission.network_policy
    if hasattr(policy, "value"):
        policy = policy.value  # Enum unwrap

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise PermissionDeniedError(url, "URL has no host component")

    # Hard global denylist (apply for ALL policies, even OPEN)
    if host in {"localhost", "0.0.0.0"} or host.endswith(".local") or _is_private_ip(host):
        raise PermissionDeniedError(url, f"host {host} blocked by global SSRF denylist")

    if policy == NetworkPolicy.NONE.value or policy == NetworkPolicy.NONE:
        raise PermissionDeniedError(url, "task network_policy=none")

    if policy == NetworkPolicy.CRATES_ONLY.value or policy == NetworkPolicy.CRATES_ONLY:
        if host in _CRATES_ONLY_HOSTS or host.endswith(".github.io"):
            return
        raise PermissionDeniedError(
            url, f"host {host} not in crates-only allowlist {sorted(_CRATES_ONLY_HOSTS)}"
        )

    if policy == NetworkPolicy.WHITELIST.value or policy == NetworkPolicy.WHITELIST:
        wl = domain_whitelist or []
        if any(host == d or host.endswith("." + d) for d in wl):
            return
        raise PermissionDeniedError(url, f"host {host} not in domain_whitelist={wl}")

    # OPEN — passed all checks above
    return


def _is_private_ip(host: str) -> bool:
    """Cheap RFC-1918 / link-local detection without importing ipaddress for
    obvious literal cases. Hostnames that aren't IPs return False here; the
    deeper firewall layer (real sandbox egress policy) handles DNS-level
    resolution.
    """
    if re.match(r"^10\.\d+\.\d+\.\d+$", host):
        return True
    if re.match(r"^192\.168\.\d+\.\d+$", host):
        return True
    if re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+$", host):
        return True
    if re.match(r"^169\.254\.\d+\.\d+$", host):
        return True
    if re.match(r"^127\.\d+\.\d+\.\d+$", host):
        return True
    return False


# ---------------------------------------------------------------------------
# Command-pattern denylist (soul.md §13)
# ---------------------------------------------------------------------------

# Compiled once; order doesn't matter — first match wins.
_COMMAND_DENYLIST: list[tuple[str, re.Pattern[str]]] = [
    ("git_push", re.compile(r"\bgit\s+push\b")),
    ("cargo_publish", re.compile(r"\bcargo\s+publish\b")),
    ("npm_publish", re.compile(r"\bnpm\s+publish\b")),
    ("pip_upload", re.compile(r"\bpython\s+-m\s+twine\s+upload\b|\btwine\s+upload\b")),
    ("rm_rf_root", re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-rf|-fr)\b\s*/+(\s|$)")),
    ("sudo", re.compile(r"(^|[\s;&|`])sudo\b")),
    ("curl_pipe_sh", re.compile(r"curl[^|;]*\|\s*(sh|bash|zsh)\b")),
    ("wget_pipe_sh", re.compile(r"wget[^|;]*\|\s*(sh|bash|zsh)\b")),
    ("backtick_curl", re.compile(r"\$\(\s*curl\s|\$\(\s*wget\s|`\s*curl\s|`\s*wget\s")),
    ("ssh", re.compile(r"(^|[\s;&|])ssh\b")),
    ("scp", re.compile(r"(^|[\s;&|])scp\b")),
    ("rsync_remote", re.compile(r"\brsync\s+[^\s]*::?[^\s]*")),
    ("nc_listen", re.compile(r"\bnc\s+-l\b|\bncat\s+-l\b")),
]


def check_command_pattern(permission: Permission, command: str) -> None:
    """Raise PermissionDeniedError if `command` matches a global denylist.

    This denylist is hardcoded and NOT overridable per-task. Listed patterns
    have no legitimate use inside a Worker's task scope:
      - external publishing (cargo publish, npm publish, twine upload)
      - external side-effects (git push, ssh, scp, rsync ::, nc -l)
      - shell escape (curl | sh, $(curl ...), `wget ...`)
      - filesystem destruction (rm -rf /)
      - privilege escalation (sudo)
    """
    # permission is currently unused here — we keep it in the signature so
    # future per-task allowlists can be plumbed without changing all callers.
    del permission

    for name, pat in _COMMAND_DENYLIST:
        if pat.search(command):
            raise PermissionDeniedError(
                command,
                f"command matches global denylist rule '{name}'",
            )


__all__ = [
    "check_path_access",
    "check_tool_allowed",
    "check_network_allowed",
    "check_command_pattern",
    "PathMode",
]
