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
import ipaddress
import re
import socket
from collections.abc import Callable
from typing import Literal
from urllib.parse import urlparse

from ..schemas import NetworkPolicy, Permission
from .errors import PermissionDeniedError

Resolver = Callable[[str], list[str]]
"""DNS resolver seam: host -> list of resolved IP strings. Injectable for tests."""

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
        raise PermissionDeniedError(path, "path traversal (..) not allowed")
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
        if pat.endswith("/") and (path == pat[:-1] or path.startswith(pat)):
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
    # NetworkPolicy is an Enum(str, Enum); comparing enum members against
    # other enum members works, and against raw strings works because the
    # mixin makes them str subclasses. We compare via the enum to keep mypy
    # happy without losing the "str input" tolerance.
    policy = NetworkPolicy(permission.network_policy)

    parsed = urlparse(url)
    # Scheme allowlist: only plain HTTP(S). Blocks file:// (LFI), ftp://, gopher://
    # and other urllib-supported schemes that could be abused for SSRF/LFI even
    # against an otherwise-allowed host.
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise PermissionDeniedError(url, f"URL scheme {scheme!r} not allowed (only http/https)")
    host = (parsed.hostname or "").lower()
    if not host:
        raise PermissionDeniedError(url, "URL has no host component")

    # Hard global denylist (apply for ALL policies, even OPEN). "0.0.0.0" here is
    # BLOCKED, not bound — bandit's B104 (bind-all-interfaces) is a false positive.
    if host in {"localhost", "0.0.0.0"} or host.endswith(".local") or _is_private_ip(host):  # nosec B104
        raise PermissionDeniedError(url, f"host {host} blocked by global SSRF denylist")

    if policy is NetworkPolicy.NONE:
        raise PermissionDeniedError(url, "task network_policy=none")

    if policy is NetworkPolicy.CRATES_ONLY:
        if host in _CRATES_ONLY_HOSTS or host.endswith(".github.io"):
            return
        raise PermissionDeniedError(
            url, f"host {host} not in crates-only allowlist {sorted(_CRATES_ONLY_HOSTS)}"
        )

    if policy is NetworkPolicy.WHITELIST:
        wl = domain_whitelist or []
        if any(host == d or host.endswith("." + d) for d in wl):
            return
        raise PermissionDeniedError(url, f"host {host} not in domain_whitelist={wl}")

    # OPEN — passed all checks above
    return


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True iff `ip` is an SSRF target: private (RFC-1918 + doc/CGNAT ranges),
    loopback, link-local (incl. cloud metadata 169.254.169.254 / fd00:ec2::254),
    multicast, reserved, or unspecified. IPv4-mapped IPv6 is unwrapped first so
    ``::ffff:10.0.0.1`` can't smuggle a private v4 address past the check.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_private_ip(host: str) -> bool:
    """True iff `host` is a literal IP (v4 or v6) in a blocked range.

    Hostnames (non-literal) return False here — those are resolved and checked
    separately by :func:`check_resolved_host_safe` (M2), since a literal-only
    check let an allowlisted hostname resolve to a private/metadata IP.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return _ip_is_blocked(ip)


def assert_addr_allowed(ip_str: str) -> None:
    """Raise PermissionDeniedError if a *resolved* IP literal is an SSRF target.

    The pin-and-connect transport (M2 TOCTOU) resolves a host once, validates
    each candidate address here, then connects to that exact address — so a DNS
    rebind between check and connect can't steer the connection to an internal
    IP. Non-IP input is a no-op (callers only pass resolved sockaddr literals).
    """
    candidate = ip_str.split("%", 1)[0]  # strip IPv6 zone id (fe80::1%eth0)
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return
    if _ip_is_blocked(ip):
        raise PermissionDeniedError(
            candidate, f"resolved address {candidate} blocked by SSRF denylist"
        )


def _default_resolver(host: str) -> list[str]:
    """Resolve `host` to all its IPs via the system resolver (A + AAAA)."""
    # info[4] is the sockaddr; element 0 is the address string (v4 and v6).
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def check_resolved_host_safe(
    host: str,
    *,
    resolver: Resolver = _default_resolver,
) -> None:
    """Reject `host` if it (or anything it resolves to) is an SSRF target (M2).

    ``check_network_allowed`` only blocks *literal* private IPs in the URL, so
    an allowlisted hostname — or any host under OPEN policy — that resolves to a
    private / link-local / cloud-metadata address slipped through. This resolves
    the host and rejects if ANY resolved address is blocked.

    A literal-IP host is validated directly (no DNS). Resolution failure is
    non-fatal: a host that can't resolve can't be connected to, so we let the
    subsequent fetch surface the error rather than converting it into a denial.

    This is a fast pre-check that produces a clean structured denial + egress
    log before any socket work. The TOCTOU window it used to leave (resolve here,
    re-resolve at connect) is now closed by the pin-and-connect transport
    (:func:`~maf_coder.agents.tools.research_tools._safe_create_connection`),
    which resolves once and connects to that exact validated address via
    :func:`assert_addr_allowed`.
    """
    if not host:
        return
    # Literal IP host: validate directly, skip DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _ip_is_blocked(literal):
            raise PermissionDeniedError(host, f"host IP {host} blocked by SSRF denylist")
        return

    try:
        addrs = resolver(host)
    except OSError:
        return
    for addr in addrs:
        candidate = addr.split("%", 1)[0]  # strip IPv6 zone id (e.g. fe80::1%eth0)
        try:
            rip = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if _ip_is_blocked(rip):
            raise PermissionDeniedError(
                host, f"host {host} resolves to blocked address {candidate}"
            )


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
    "PathMode",
    "Resolver",
    "assert_addr_allowed",
    "check_command_pattern",
    "check_network_allowed",
    "check_path_access",
    "check_resolved_host_safe",
    "check_tool_allowed",
]
