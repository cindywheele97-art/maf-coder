"""Preflight readiness gate — "can this go straight to a real run?" in one pass.

Real production entry has several operator prerequisites scattered across the
smoke test and the runbook: provider API keys, a valid router config, a
profilable target repo, and (for real runs) a reachable Docker daemon with the
sandbox image built. This consolidates them into a single go/no-go check so the
operator can answer the readiness question with one command
(``maf-coder preflight``) instead of discovering each gap mid-mission.

The check is pure and dependency-injected: ``env``, ``docker_available`` and
``image_present`` are seams so the logic is testable without real keys/Docker.
It never makes a network/LLM call and never spends money — it only inspects.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ..models.router import ModelRouter, provider_of
from ..sandbox.client import DockerSandbox
from .project_profiler import ProjectProfileError, profile_project

# LiteLLM provider prefix -> acceptable env var names (any present satisfies it).
_PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}

DEFAULT_SANDBOX_IMAGE = "maf-coder:rust-sandbox"

# Check statuses.
PASS = "pass"
FAIL = "fail"
WARN = "warn"


@dataclass(frozen=True)
class PreflightCheck:
    """One readiness check. `status` is pass/fail/warn; `remediation` is the
    exact next action when it isn't passing."""

    name: str
    status: str
    detail: str
    remediation: str = ""


@dataclass(frozen=True)
class PreflightReport:
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Go iff nothing failed (warnings don't block)."""
        return all(c.status != FAIL for c in self.checks)


@dataclass(frozen=True)
class _KeyReq:
    label: str
    env_vars: tuple[str, ...]
    reason: str


def _docker_image_present(image: str) -> bool:
    """Best-effort: True iff the local Docker daemon has `image`."""
    try:
        import docker

        client = docker.from_env()
        client.images.get(image)
        return True
    except Exception:
        return False


def _required_keys(router: ModelRouter) -> list[_KeyReq]:
    """The distinct API-key requirements across every role's primary + fallback.

    A model's explicit ``api_key_env`` (custom endpoints) wins; otherwise the
    provider prefix maps to its standard env var(s).
    """
    seen: dict[tuple[str, ...], _KeyReq] = {}
    for role_name, role in router.config.roles.items():
        for model in [role.primary, *role.fallback]:
            if model.api_key_env:
                env_vars: tuple[str, ...] = (model.api_key_env,)
            else:
                env_vars = _PROVIDER_ENV.get(provider_of(model.model), ())
            if not env_vars or env_vars in seen:
                continue
            seen[env_vars] = _KeyReq(
                label=" or ".join(env_vars),
                env_vars=env_vars,
                reason=f"{role_name} ({model.model})",
            )
    return list(seen.values())


def run_preflight(
    router_config: str | Path,
    *,
    repo_path: str | Path | None = None,
    sandbox: str = "docker",
    image: str = DEFAULT_SANDBOX_IMAGE,
    env: Mapping[str, str] | None = None,
    docker_available: Callable[[], bool] = DockerSandbox.is_available,
    image_present: Callable[[str], bool] = _docker_image_present,
) -> PreflightReport:
    """Run all readiness checks and return a report. Inspect-only (no spend)."""
    env = os.environ if env is None else env
    checks: list[PreflightCheck] = []

    # 1. Router config loads + validates (this also enforces the 异-provider rule).
    router: ModelRouter | None = None
    try:
        router = ModelRouter(router_config)
        checks.append(
            PreflightCheck(
                "router config",
                PASS,
                f"loaded {router_config} — {len(router.config.roles)} roles, "
                "异-provider rule intact",
            )
        )
    except Exception as e:
        checks.append(
            PreflightCheck(
                "router config",
                FAIL,
                f"failed to load {router_config}: {e}",
                "fix config/droid_whispering.yaml",
            )
        )

    # 2. Provider API keys for every model in the routing chains.
    if router is not None:
        for req in _required_keys(router):
            present = any(env.get(v) for v in req.env_vars)
            checks.append(
                PreflightCheck(
                    f"api key {req.label}",
                    PASS if present else FAIL,
                    f"required by {req.reason}" + ("" if present else " — not set"),
                    "" if present else f"export {req.env_vars[0]}=...",
                )
            )
    else:
        checks.append(
            PreflightCheck(
                "api keys",
                WARN,
                "skipped — router config did not load",
                "fix the router config, then re-run preflight",
            )
        )

    # 3. Target repo profilable (only when a repo is given).
    if repo_path is not None:
        try:
            profile = profile_project(repo_path, strict=True)
            ptype = getattr(profile.project_type, "value", profile.project_type)
            checks.append(
                PreflightCheck(
                    "target repo",
                    PASS,
                    f"profiled {repo_path} as {ptype}",
                )
            )
        except ProjectProfileError as e:
            checks.append(
                PreflightCheck(
                    "target repo",
                    FAIL,
                    str(e),
                    "point --repo at a Cargo project (or omit for a config-only check)",
                )
            )

    # 4 + 5. Docker daemon + sandbox image (only for the docker backend).
    if sandbox == "docker":
        dok = docker_available()
        checks.append(
            PreflightCheck(
                "docker daemon",
                PASS if dok else FAIL,
                "reachable" if dok else "not reachable",
                "" if dok else "start Docker (real --no-dry-run runs default to docker)",
            )
        )
        if dok:
            present = image_present(image)
            checks.append(
                PreflightCheck(
                    "sandbox image",
                    PASS if present else FAIL,
                    f"{image} present" if present else f"{image} not built",
                    "" if present else "bash scripts/build_sandbox.sh",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "sandbox image",
                    WARN,
                    "skipped — docker not reachable",
                    "start Docker, then bash scripts/build_sandbox.sh",
                )
            )

    return PreflightReport(checks=checks)


__all__ = [
    "DEFAULT_SANDBOX_IMAGE",
    "PreflightCheck",
    "PreflightReport",
    "run_preflight",
]
