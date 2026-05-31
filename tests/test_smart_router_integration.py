"""Smart Router PR-SR2 — ModelRouter.resolve_model tier integration.

These tests verify the *application* of a tier over a role's primary, and — most
importantly — that the application NEVER bypasses the forbidden-providers /
validator-≠-coder enforcement (execution plan §1.5). The Judge is always a
mocked stub callable, so no test hits a live API.

Invariants encoded here (the WHY, per testing rule 9):
  - Disabled smart_router (globally or per-role) must keep routing byte-for-byte
    identical to get_primary_model — otherwise SR-2 would silently change the
    355 existing routing decisions.
  - A tier override that targets a forbidden provider is a security hazard: it
    could route a validator onto the Coder's provider, defeating the
    different-provider rule. It MUST be discarded, not honoured.
  - `complex` is an Orchestrator re-planning signal, not a model selection — it
    must return the primary unchanged and never error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from maf_coder.models import ModelRouter
from maf_coder.models.router import ModelConfig
from maf_coder.schemas.common import RiskLevel, Role
from maf_coder.schemas.task import Permission, Task

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_roles() -> dict:
    """Role configs shared by every smart_router fixture below."""
    return {
        "coder_worker": {
            "primary": {
                "model": "anthropic/claude-sonnet-4-6",
                "temperature": 0.3,
                "max_tokens": 32000,
            },
            "fallback": [
                {"model": "openai/gpt-5", "temperature": 0.3, "max_tokens": 32000},
            ],
        },
        "review_validator": {
            "primary": {"model": "openai/gpt-5", "temperature": 0.0, "max_tokens": 8000},
            "fallback": [
                {"model": "google/gemini-2.5-pro", "temperature": 0.0, "max_tokens": 8000},
            ],
            "constraints": {"forbidden_providers": ["anthropic"]},
        },
    }


def _smart_router_block(*, enabled: bool = True) -> dict:
    return {
        "enabled": enabled,
        "judge": {"model": "google/gemini-2.5-flash", "temperature": 0.0, "max_tokens": 256},
        "default_tier": "medium",
        "tiers": {
            "simple": {"model": "anthropic/claude-sonnet-4-6", "max_tokens": 8000},
            "medium": {"model": "anthropic/claude-sonnet-4-6", "max_tokens": 32000},
            "reasoning": {"model": "anthropic/claude-opus-4-7", "max_tokens": 32000},
            # complex intentionally absent → no model override.
        },
        "rules": ["Cross-crate refactor → reasoning"],
        "per_role": {
            "coder_worker": {"enabled": True},
            "review_validator": {"enabled": False},
        },
    }


def _write_config(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "droid.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


@pytest.fixture
def sr_config(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path, {"version": 1, "roles": _base_roles(), "smart_router": _smart_router_block()}
    )


@pytest.fixture
def sr_disabled_config(tmp_path: Path) -> Path:
    return _write_config(
        tmp_path,
        {"version": 1, "roles": _base_roles(), "smart_router": _smart_router_block(enabled=False)},
    )


@pytest.fixture
def no_sr_config(tmp_path: Path) -> Path:
    """No smart_router block at all — the pre-SR-1 world."""
    return _write_config(tmp_path, {"version": 1, "roles": _base_roles()})


def _task(goal: str = "Add a CLI flag") -> Task:
    return Task(
        task_id="t1",
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        risk_level=RiskLevel.LOW,
        goal=goal,
        background="bg",
        acceptance_criteria=["f1.a1"],
        required_outputs=["patch.diff"],
        permission=Permission(),
    )


def _judge(tier: str):
    """Stub judge emitting a fixed <tier> tag. Keeps tests off the live API."""

    async def _fn(_prompt: str) -> str:
        return f"<tier>{tier}</tier>"

    return _fn


# ---------------------------------------------------------------------------
# Disabled path — MUST equal get_primary_model (the 355-tests-stay-green guard)
# ---------------------------------------------------------------------------


class TestDisabledPathIsIdentical:
    async def test_global_disabled_returns_primary(self, sr_disabled_config: Path) -> None:
        router = ModelRouter(sr_disabled_config)
        expected = router.get_primary_model("coder_worker")
        got = await router.resolve_model("coder_worker", task=_task(), judge=_judge("reasoning"))
        # Even though the judge would pick `reasoning`, smart_router is off → primary.
        assert got == expected

    async def test_no_smart_router_block_returns_primary(self, no_sr_config: Path) -> None:
        router = ModelRouter(no_sr_config)
        expected = router.get_primary_model("coder_worker")
        got = await router.resolve_model("coder_worker", task=_task(), judge=_judge("reasoning"))
        assert got == expected

    async def test_per_role_disabled_returns_primary(self, sr_config: Path) -> None:
        # review_validator has per_role.enabled=False even though SR is globally on.
        router = ModelRouter(sr_config)
        expected = router.get_primary_model("review_validator")
        got = await router.resolve_model(
            "review_validator", task=_task(), judge=_judge("reasoning")
        )
        assert got == expected

    async def test_per_role_disabled_still_honours_coder_constraint(
        self, sr_config: Path
    ) -> None:
        # Disabled path must STILL apply the dynamic coder-provider constraint.
        router = ModelRouter(sr_config)
        expected = router.get_primary_model(
            "review_validator", coder_provider_in_use="openai"
        )
        got = await router.resolve_model(
            "review_validator",
            task=_task(),
            coder_provider_in_use="openai",
            judge=_judge("reasoning"),
        )
        assert got == expected
        assert "openai" not in got.model
        assert "anthropic" not in got.model


# ---------------------------------------------------------------------------
# Enabled path — tier override is applied over the primary
# ---------------------------------------------------------------------------


class TestEnabledPathAppliesOverride:
    async def test_reasoning_tier_upgrades_coder_to_opus(self, sr_config: Path) -> None:
        router = ModelRouter(sr_config)
        got = await router.resolve_model(
            "coder_worker", task=_task(), judge=_judge("reasoning")
        )
        # reasoning tier model overrides the sonnet primary.
        assert got.model == "anthropic/claude-opus-4-7"
        assert got.max_tokens == 32000

    async def test_simple_tier_applies_its_max_tokens(self, sr_config: Path) -> None:
        router = ModelRouter(sr_config)
        got = await router.resolve_model("coder_worker", task=_task(), judge=_judge("simple"))
        assert got.model == "anthropic/claude-sonnet-4-6"
        assert got.max_tokens == 8000  # simple tier's max_tokens, not the primary's 32000

    async def test_returns_modelconfig_instance(self, sr_config: Path) -> None:
        router = ModelRouter(sr_config)
        got = await router.resolve_model(
            "coder_worker", task=_task(), judge=_judge("reasoning")
        )
        assert isinstance(got, ModelConfig)


# ---------------------------------------------------------------------------
# complex tier — Orchestrator re-planning signal, NOT a model selection
# ---------------------------------------------------------------------------


class TestComplexTierReturnsPrimary:
    async def test_complex_returns_primary_unchanged_no_error(self, sr_config: Path) -> None:
        router = ModelRouter(sr_config)
        expected = router.get_primary_model("coder_worker")
        got = await router.resolve_model("coder_worker", task=_task(), judge=_judge("complex"))
        # complex carries no model override → primary, never an error.
        assert got == expected


# ---------------------------------------------------------------------------
# §1.5 CRITICAL — a hostile tier override targeting a forbidden provider is
# rejected; the validator never lands on the Coder's provider.
# ---------------------------------------------------------------------------


class TestForbiddenProviderTierOverrideIsRejected:
    @pytest.fixture
    def hostile_config(self, tmp_path: Path) -> Path:
        """smart_router enabled for review_validator, with a `reasoning` tier
        that points at anthropic — the very provider review_validator forbids.
        A correct resolve_model must DISCARD this override.
        """
        cfg = {"version": 1, "roles": _base_roles(), "smart_router": _smart_router_block()}
        cfg["smart_router"]["per_role"]["review_validator"] = {"enabled": True}
        cfg["smart_router"]["tiers"]["reasoning"] = {
            "model": "anthropic/claude-opus-4-7",  # forbidden for review_validator
            "max_tokens": 8000,
        }
        return _write_config(tmp_path, cfg)

    async def test_static_forbidden_override_discarded(self, hostile_config: Path) -> None:
        router = ModelRouter(hostile_config)
        got = await router.resolve_model(
            "review_validator", task=_task(), judge=_judge("reasoning")
        )
        # anthropic is statically forbidden → override rejected → compliant primary.
        assert "anthropic" not in got.model
        assert got == router.get_primary_model("review_validator")

    async def test_dynamic_coder_provider_override_discarded(self, hostile_config: Path) -> None:
        """The deeper hazard: tier picks the Coder's provider for a validator.

        Make the hostile tier target openai and run with the Coder on openai.
        resolve_model MUST refuse to route the validator onto openai (the Coder's
        provider) and fall back to the only compliant option (google).
        """
        cfg = {"version": 1, "roles": _base_roles(), "smart_router": _smart_router_block()}
        cfg["smart_router"]["per_role"]["review_validator"] = {"enabled": True}
        cfg["smart_router"]["tiers"]["reasoning"] = {
            "model": "openai/gpt-5",  # == coder provider below
            "max_tokens": 8000,
        }
        p = Path(hostile_config).parent / "hostile2.yaml"
        p.write_text(yaml.safe_dump(cfg))
        router = ModelRouter(p)

        got = await router.resolve_model(
            "review_validator",
            task=_task(),
            coder_provider_in_use="openai",
            judge=_judge("reasoning"),
        )
        assert "openai" not in got.model  # never the Coder's provider
        assert "anthropic" not in got.model  # static constraint still holds
        assert "google" in got.model
        assert got == router.get_primary_model(
            "review_validator", coder_provider_in_use="openai"
        )


# ---------------------------------------------------------------------------
# default_tier fallback path (judge unparseable) still applies a compliant model
# ---------------------------------------------------------------------------


class TestDefaultTierFallback:
    async def test_unparseable_judge_uses_default_tier(self, sr_config: Path) -> None:
        async def _bad_judge(_prompt: str) -> str:
            return "I have no idea"

        router = ModelRouter(sr_config)
        got = await router.resolve_model("coder_worker", task=_task(), judge=_bad_judge)
        # default_tier=medium → medium tier model (sonnet, 32000).
        assert got.model == "anthropic/claude-sonnet-4-6"
        assert got.max_tokens == 32000
