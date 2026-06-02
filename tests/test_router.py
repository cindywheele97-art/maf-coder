"""Sanity tests for ModelRouter.

Phase A 退出门槛: `pytest tests/test_router.py` 全过.
These tests do NOT call any real model — they verify routing logic only.
The smoke test for actual API calls lives in tests/smoke/ (run manually).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from maf_coder.models import (
    ModelRouter,
    ProviderForbiddenError,
    RoleNotConfiguredError,
)
from maf_coder.models.router import _provider_of


@pytest.fixture
def minimal_config(tmp_path: Path) -> Path:
    """A minimal but representative droid_whispering.yaml.

    Mirrors the structure of config/droid_whispering.yaml in the real project,
    but with only the roles needed for routing tests.
    """
    cfg = {
        "version": 1,
        "roles": {
            "orchestrator": {
                "primary": {
                    "model": "anthropic/claude-opus-4-7",
                    "temperature": 0.2,
                    "max_tokens": 16000,
                },
                "fallback": [
                    {"model": "openai/gpt-5", "temperature": 0.2, "max_tokens": 16000},
                ],
            },
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
                "primary": {
                    "model": "openai/gpt-5",
                    "temperature": 0.0,
                    "max_tokens": 8000,
                },
                "fallback": [
                    {
                        "model": "google/gemini-2.5-pro",
                        "temperature": 0.0,
                        "max_tokens": 8000,
                    },
                ],
                "constraints": {"forbidden_providers": ["anthropic"]},
            },
            "adversarial_subagent": {
                "primary": {
                    "model": "google/gemini-2.5-pro",
                    "temperature": 0.0,
                    "max_tokens": 6000,
                },
                "fallback": [
                    {"model": "openai/gpt-5", "temperature": 0.0, "max_tokens": 6000},
                ],
                "constraints": {"forbidden_providers": ["anthropic"]},
            },
            # Edge case: a role with only one option whose provider could be blocked
            "fragile_role": {
                "primary": {
                    "model": "openai/gpt-5",
                    "temperature": 0.0,
                    "max_tokens": 4000,
                },
                "fallback": [],
            },
        },
    }
    p = tmp_path / "droid.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


# ============================================================================
# Helpers
# ============================================================================


class TestProviderHelper:
    def test_namespaced(self) -> None:
        assert _provider_of("anthropic/claude-opus-4-7") == "anthropic"
        assert _provider_of("google/gemini-2.5-pro") == "google"

    def test_bare_name_defaults_to_openai(self) -> None:
        assert _provider_of("gpt-4o") == "openai"


# ============================================================================
# Config loading
# ============================================================================


class TestModelRouterLoading:
    def test_load_valid_config(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        assert "orchestrator" in router.config.roles
        assert router.config.version == 1

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ModelRouter(tmp_path / "does_not_exist.yaml")

    def test_unknown_role_raises(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        with pytest.raises(RoleNotConfiguredError):
            router.get_role_config("nonexistent_role")


# ============================================================================
# Primary resolution — no constraints
# ============================================================================


class TestPrimaryResolutionNoConstraints:
    def test_orchestrator_uses_anthropic_primary(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        m = router.get_primary_model("orchestrator")
        assert m.model == "anthropic/claude-opus-4-7"

    def test_coder_uses_anthropic_primary(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        m = router.get_primary_model("coder_worker")
        assert m.model == "anthropic/claude-sonnet-4-6"

    def test_provider_for_role_reads_raw_primary(self, minimal_config: Path) -> None:
        # provider_for_role identifies the role's OWN provider (no forbidden
        # resolution) — this is how the driver derives coder_provider_in_use.
        router = ModelRouter(minimal_config)
        assert router.provider_for_role("coder_worker") == "anthropic"
        assert router.provider_for_role("orchestrator") == "anthropic"


# ============================================================================
# Static forbidden_providers (yaml-level constraint)
# ============================================================================


class TestStaticForbiddenProviders:
    """review_validator and adversarial_subagent forbid anthropic in the yaml."""

    def test_review_validator_skips_anthropic_when_in_yaml(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        m = router.get_primary_model("review_validator")
        assert "anthropic" not in m.model
        # Primary is openai/gpt-5 which is allowed by the static constraint
        assert m.model == "openai/gpt-5"

    def test_adversarial_subagent_primary_is_google_not_anthropic(
        self, minimal_config: Path
    ) -> None:
        router = ModelRouter(minimal_config)
        m = router.get_primary_model("adversarial_subagent")
        assert "anthropic" not in m.model


# ============================================================================
# Dynamic异-provider constraint vs Coder (the v3 §4 invariant)
# ============================================================================


class TestDynamicCoderConstraint:
    """When Coder is on provider X, validators must NOT use X — even if X is
    listed as their primary. This protects against shared-training-data blind
    spots between Coder and its reviewers (soul.md §3.5).
    """

    def test_coder_on_openai_pushes_review_validator_to_google(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        # Coder using openai → review_validator can't use openai → falls back to google
        m = router.get_primary_model("review_validator", coder_provider_in_use="openai")
        assert "openai" not in m.model
        assert "anthropic" not in m.model  # static constraint still applies
        assert "google" in m.model

    def test_coder_on_anthropic_review_validator_uses_openai_normally(
        self, minimal_config: Path
    ) -> None:
        router = ModelRouter(minimal_config)
        # Coder using anthropic → review_validator's primary openai is fine (anthropic already forbidden statically)
        m = router.get_primary_model("review_validator", coder_provider_in_use="anthropic")
        assert m.model == "openai/gpt-5"

    def test_coder_on_google_pushes_subagent_to_openai(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        # Coder using google → adversarial_subagent primary (google) blocked → falls back to openai
        m = router.get_primary_model("adversarial_subagent", coder_provider_in_use="google")
        assert m.model == "openai/gpt-5"

    def test_orchestrator_not_affected_by_coder_constraint(self, minimal_config: Path) -> None:
        """Orchestrator is not a validator; coder_provider_in_use does NOT constrain it."""
        router = ModelRouter(minimal_config)
        m = router.get_primary_model("orchestrator", coder_provider_in_use="anthropic")
        # Orchestrator's primary is anthropic; even though Coder is also anthropic, that's fine
        assert m.model == "anthropic/claude-opus-4-7"


# ============================================================================
# Exhaustion: when every option is forbidden
# ============================================================================


class TestExhaustion:
    def test_all_forbidden_raises(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        # fragile_role only has openai/gpt-5. If we forbid openai (via Coder constraint),
        # fragile_role becomes a normal non-validator role though — so Coder doesn't apply.
        # We simulate by manually patching the constraint structure on adversarial_subagent.
        cfg = router.config.roles["adversarial_subagent"]
        # Force a constraint that blocks every model in the chain
        cfg.constraints = {"forbidden_providers": ["anthropic", "openai", "google"]}
        with pytest.raises(ProviderForbiddenError):
            router.get_primary_model("adversarial_subagent")


# ============================================================================
# Chain resolution (for smoke tests / dry runs)
# ============================================================================


class TestChainResolution:
    def test_chain_contains_primary_then_fallbacks(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        chain = router.resolve_chain("coder_worker")
        assert chain[0].model == "anthropic/claude-sonnet-4-6"
        assert chain[1].model == "openai/gpt-5"

    def test_chain_excludes_forbidden_models(self, minimal_config: Path) -> None:
        router = ModelRouter(minimal_config)
        chain = router.resolve_chain("review_validator", coder_provider_in_use="openai")
        # openai/gpt-5 (primary) and anthropic (static) blocked → only google left
        assert len(chain) == 1
        assert "google" in chain[0].model


def test_shipped_config_loads_and_is_valid() -> None:
    """The shipped config/droid_whispering.yaml MUST validate against the schema.

    Regression: a stray `smart_router.judge.timeout_ms` key once made every real
    mission fail at ModelRouter construction (ModelConfig is extra='forbid'). This
    test loads the actual file the CLI ships so such drift fails loudly in CI.
    """
    cfg = Path(__file__).resolve().parents[1] / "config" / "droid_whispering.yaml"
    assert cfg.exists(), "shipped config/droid_whispering.yaml is missing"
    router = ModelRouter(cfg)
    # Usable: every role the MissionDriver constructs resolves a provider.
    for role in ("orchestrator", "coder_worker", "review_validator", "behavior_validator"):
        assert router.provider_for_role(role)
