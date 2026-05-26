"""ModelRouter — reads droid_whispering.yaml and routes role-based model calls
through LiteLLM with fallback chain and provider-constraint enforcement.

Key responsibilities (soul.md §4 + droid_whispering.yaml):

1. Map role → primary/fallback model configs from YAML
2. Enforce static `forbidden_providers` (e.g. review_validator forbids anthropic)
3. Enforce DYNAMIC provider constraint: validators must use different provider
   than Coder *for this mission* (tracked in MissionState.coder_provider_in_use)
4. Provide unified async `complete()` interface — hides LiteLLM details
5. Track cost + tokens per call → feeds budget guard + event log
6. Auto-fallback on primary model errors

Important: LiteLLM is imported lazily inside `complete()` so unit tests can
exercise the routing logic without needing a real API key or network.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config schemas (mirrors droid_whispering.yaml structure)
# ---------------------------------------------------------------------------


class ModelConfig(BaseModel):
    """One model entry in the yaml — primary or a fallback."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="LiteLLM model string, e.g. 'anthropic/claude-opus-4-7'")
    temperature: float = 0.2
    max_tokens: int = 8000


class RoleConfig(BaseModel):
    """A single role's full routing config."""

    model_config = ConfigDict(extra="allow")  # tolerate `notes` and future keys

    primary: ModelConfig
    fallback: list[ModelConfig] = Field(default_factory=list)
    constraints: dict[str, list[str]] = Field(default_factory=dict)
    notes: str = ""


class RouterConfig(BaseModel):
    """Top-level droid_whispering.yaml structure."""

    model_config = ConfigDict(extra="allow")  # tolerate budgets/network/tracing sections

    version: int = 1
    roles: dict[str, RoleConfig]


# ---------------------------------------------------------------------------
# Call result type
# ---------------------------------------------------------------------------


@dataclass
class CallResult:
    """Outcome of one model call. Drives cost tracking + event log."""

    role: str
    model_used: str
    content: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_sec: float
    fallback_used: bool = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RoleNotConfiguredError(KeyError):
    """Raised when a requested role has no entry in droid_whispering.yaml."""


class ProviderForbiddenError(RuntimeError):
    """Raised when every model in a role's chain is forbidden by current constraints."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider_of(model_id: str) -> str:
    """Extract LiteLLM provider prefix.

    'anthropic/claude-opus-4-7' -> 'anthropic'
    'openai/gpt-5'              -> 'openai'
    'gpt-4o'                    -> 'openai' (LiteLLM default)
    """
    if "/" in model_id:
        return model_id.split("/", 1)[0]
    # LiteLLM convention: bare names default to openai
    return "openai"


# Roles that MUST run on a different provider than Coder. This is the dynamic
# half of the异-provider rule from soul.md §3.5 — the static half lives in
# `forbidden_providers` inside droid_whispering.yaml.
_VALIDATOR_ROLES = frozenset({"review_validator", "adversarial_subagent"})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class ModelRouter:
    """Read droid_whispering.yaml once at startup.

    For every model call:
    - Resolve role -> primary
    - Apply static forbidden_providers constraints
    - Apply dynamic constraint: validators ≠ coder_provider_in_use
    - Try primary; on error, try fallbacks in order
    - Return CallResult with cost/tokens for downstream budget tracking
    """

    def __init__(self, config_path: str | Path):
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"droid_whispering config not found: {path}")
        raw = yaml.safe_load(path.read_text())
        self.config = RouterConfig.model_validate(raw)
        logger.info(
            "ModelRouter loaded %d role configs from %s",
            len(self.config.roles),
            path,
        )

    # -- Role lookup -------------------------------------------------------

    def get_role_config(self, role: str) -> RoleConfig:
        if role not in self.config.roles:
            raise RoleNotConfiguredError(
                f"Role '{role}' not found in droid_whispering.yaml. "
                f"Available: {sorted(self.config.roles)}"
            )
        return self.config.roles[role]

    # -- Forbidden-provider computation -----------------------------------

    def _forbidden_providers_for(self, role: str, *, coder_provider_in_use: str | None) -> set[str]:
        """Combine static (yaml) + dynamic (vs Coder) forbidden providers."""
        cfg = self.get_role_config(role)
        forbidden: set[str] = set(cfg.constraints.get("forbidden_providers", []))
        if coder_provider_in_use and role in _VALIDATOR_ROLES:
            forbidden.add(coder_provider_in_use)
        return forbidden

    # -- Primary / fallback resolution ------------------------------------

    def get_primary_model(
        self, role: str, *, coder_provider_in_use: str | None = None
    ) -> ModelConfig:
        """Resolve primary model for a role, applying all constraints.

        Logic:
        1. If primary's provider is not forbidden → return primary.
        2. Otherwise → first fallback whose provider is not forbidden.
        3. If everything is forbidden → ProviderForbiddenError.
        """
        cfg = self.get_role_config(role)
        forbidden = self._forbidden_providers_for(role, coder_provider_in_use=coder_provider_in_use)

        if _provider_of(cfg.primary.model) not in forbidden:
            return cfg.primary

        # Primary blocked — search fallbacks
        for fb in cfg.fallback:
            if _provider_of(fb.model) not in forbidden:
                logger.warning(
                    "Role %s primary %s forbidden (providers blocked: %s); using fallback %s",
                    role,
                    cfg.primary.model,
                    sorted(forbidden),
                    fb.model,
                )
                return fb

        raise ProviderForbiddenError(
            f"Role '{role}': primary {cfg.primary.model} and all fallbacks "
            f"are blocked by forbidden providers {sorted(forbidden)}. "
            f"Check droid_whispering.yaml fallback chain coverage."
        )

    def get_fallback_chain(
        self, role: str, *, coder_provider_in_use: str | None = None
    ) -> list[ModelConfig]:
        """Get the ordered list of acceptable fallback models (excluding primary)."""
        cfg = self.get_role_config(role)
        forbidden = self._forbidden_providers_for(role, coder_provider_in_use=coder_provider_in_use)
        return [fb for fb in cfg.fallback if _provider_of(fb.model) not in forbidden]

    # -- Sync resolution (no model call) for smoke tests -------------------

    def resolve_chain(
        self, role: str, *, coder_provider_in_use: str | None = None
    ) -> list[ModelConfig]:
        """Return the full chain that `complete()` would try, in order.

        Useful for smoke tests + dry-runs without burning tokens.
        """
        primary = self.get_primary_model(role, coder_provider_in_use=coder_provider_in_use)
        chain = [primary]
        for fb in self.get_fallback_chain(role, coder_provider_in_use=coder_provider_in_use):
            # Don't duplicate primary if primary was selected from fallback list
            if fb.model != primary.model:
                chain.append(fb)
        return chain

    # -- Async completion --------------------------------------------------

    async def complete(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        coder_provider_in_use: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CallResult:
        """Run a completion for a role.

        Tries primary; on any exception, walks fallback chain until one
        succeeds or all are exhausted (RuntimeError).
        """
        try:
            from litellm import acompletion  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("LiteLLM not installed. Run: pip install 'litellm>=1.50.0'") from e

        chain = self.resolve_chain(role, coder_provider_in_use=coder_provider_in_use)
        last_error: Exception | None = None

        for idx, model_cfg in enumerate(chain):
            t0 = time.monotonic()
            try:
                resp = await acompletion(
                    model=model_cfg.model,
                    messages=messages,
                    temperature=temperature if temperature is not None else model_cfg.temperature,
                    max_tokens=max_tokens if max_tokens is not None else model_cfg.max_tokens,
                    tools=tools,
                )
                latency = time.monotonic() - t0
                # LiteLLM returns an OpenAI-compatible response object
                content = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                tokens_in = getattr(usage, "prompt_tokens", 0) if usage else 0
                tokens_out = getattr(usage, "completion_tokens", 0) if usage else 0
                # LiteLLM populates _response_cost when its pricing table knows the model
                cost = float(getattr(resp, "_response_cost", 0.0) or 0.0)
                return CallResult(
                    role=role,
                    model_used=model_cfg.model,
                    content=content,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                    latency_sec=latency,
                    fallback_used=(idx > 0),
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "Model call failed: role=%s model=%s error=%r; trying next.",
                    role,
                    model_cfg.model,
                    e,
                )
                continue

        raise RuntimeError(
            f"All models exhausted for role '{role}'. Last error: {last_error!r}"
        ) from last_error
