"""Tests for TierRouter (Smart Router PR-SR1).

The Judge LLM is injected as a stub callable, so these tests NEVER hit a live
API. They cover: each tier parses from a mocked <tier> response; sticky
continuation holds previous_tier on ambiguous output; defaultTier fallback on
unparseable output; RouteDecision round-trips with extra="forbid".
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from maf_coder.models.tier_router import (
    DEFAULT_TIER,
    classify_task,
    generate_judge_prompt,
    parse_tier,
)
from maf_coder.schemas import RouteDecision, TierModelOverride, TierName
from maf_coder.schemas.common import RiskLevel, Role
from maf_coder.schemas.task import Permission, Task


def _make_task(*, goal: str = "Add a flag to the CLI", risk: RiskLevel = RiskLevel.LOW) -> Task:
    """Minimal valid Task for routing tests."""
    return Task(
        task_id="t1",
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        risk_level=risk,
        goal=goal,
        background="background",
        acceptance_criteria=["f1.a1", "f1.a2"],
        required_outputs=["patch.diff"],
        permission=Permission(),
    )


def _judge_returning(text: str):
    """Build a stub judge callable that always returns ``text``."""

    async def _judge(_prompt: str) -> str:
        return text

    return _judge


# ---------------------------------------------------------------------------
# parse_tier
# ---------------------------------------------------------------------------


class TestParseTier:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("<tier>simple</tier>", TierName.SIMPLE),
            ("<tier>medium</tier>", TierName.MEDIUM),
            ("<tier>reasoning</tier>", TierName.REASONING),
            ("<tier>complex</tier>", TierName.COMPLEX),
            ("noise <TIER> Complex </TIER> trailing", TierName.COMPLEX),
        ],
    )
    def test_parses_each_tier(self, raw: str, expected: TierName) -> None:
        assert parse_tier(raw) == expected

    def test_unparseable_returns_none(self) -> None:
        assert parse_tier("I think this is medium difficulty") is None

    def test_unknown_tier_name_returns_none(self) -> None:
        assert parse_tier("<tier>gigantic</tier>") is None


# ---------------------------------------------------------------------------
# classify_task — tier parsing (the cost decision must follow the Judge)
# ---------------------------------------------------------------------------


class TestClassifyTierParsing:
    @pytest.mark.parametrize(
        "tier",
        [TierName.SIMPLE, TierName.MEDIUM, TierName.REASONING, TierName.COMPLEX],
    )
    async def test_each_tier_parses_from_mocked_response(self, tier: TierName) -> None:
        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_judge_returning(f"<tier>{tier.value}</tier>"),
        )
        # use_enum_values=True ⇒ stored as the string value.
        assert decision.tier == tier.value
        assert decision.sticky_hit is False
        assert decision.judge_raw == f"<tier>{tier.value}</tier>"


# ---------------------------------------------------------------------------
# Sticky continuation — WHY: a clean parse must NOT be silently overridden, but
# an ambiguous/continuation task must inherit previous_tier instead of collapsing
# to a wrong (usually cheaper) tier. That is the cost-correctness guarantee.
# ---------------------------------------------------------------------------


class TestStickyContinuation:
    async def test_ambiguous_output_holds_previous_tier(self) -> None:
        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_judge_returning("hmm, not sure"),  # unparseable
            previous_tier="reasoning",
        )
        assert decision.tier == TierName.REASONING.value
        assert decision.sticky_hit is True

    async def test_short_continuation_goal_holds_previous_tier(self) -> None:
        # Judge even names a cheap tier, but the goal is a bare continuation cue,
        # so we must stay on the previous (expensive) tier — not downgrade.
        decision = await classify_task(
            task=_make_task(goal="继续"),
            profile=None,
            rules=[],
            judge=_judge_returning("<tier>simple</tier>"),
            previous_tier="complex",
        )
        assert decision.tier == TierName.COMPLEX.value
        assert decision.sticky_hit is True

    async def test_clean_parse_not_overridden_by_previous_tier(self) -> None:
        # A real, non-continuation task with a clean tier must win over sticky.
        decision = await classify_task(
            task=_make_task(goal="Refactor the storage layer across three crates"),
            profile=None,
            rules=[],
            judge=_judge_returning("<tier>reasoning</tier>"),
            previous_tier="simple",
        )
        assert decision.tier == TierName.REASONING.value
        assert decision.sticky_hit is False


# ---------------------------------------------------------------------------
# defaultTier fallback
# ---------------------------------------------------------------------------


class TestDefaultTierFallback:
    async def test_unparseable_no_previous_falls_back_to_default(self) -> None:
        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_judge_returning("garbage with no tag"),
        )
        assert decision.tier == DEFAULT_TIER.value == TierName.MEDIUM.value
        assert decision.sticky_hit is False

    async def test_custom_default_tier_used(self) -> None:
        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_judge_returning("no tag here"),
            default_tier=TierName.SIMPLE,
        )
        assert decision.tier == TierName.SIMPLE.value

    async def test_invalid_previous_tier_falls_back_to_default(self) -> None:
        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_judge_returning("unparseable"),
            previous_tier="bogus-tier",
        )
        assert decision.tier == TierName.MEDIUM.value
        assert decision.sticky_hit is False

    async def test_judge_exception_falls_back(self) -> None:
        async def _boom(_prompt: str) -> str:
            raise RuntimeError("judge timeout")

        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_boom,
        )
        # No previous tier ⇒ default; raw is empty (call failed).
        assert decision.tier == TierName.MEDIUM.value
        assert decision.judge_raw == ""


# ---------------------------------------------------------------------------
# model_override attachment (ModelConfig-compatible) — SR-2 will apply it.
# ---------------------------------------------------------------------------


class TestModelOverride:
    async def test_override_attached_for_resolved_tier(self) -> None:
        tier_models = {
            "reasoning": TierModelOverride(
                model="anthropic/claude-opus-4-7", temperature=0.2, max_tokens=32000
            )
        }
        decision = await classify_task(
            task=_make_task(goal="Design a new public trait API"),
            profile=None,
            rules=["Cross-crate refactor or new public API → reasoning"],
            judge=_judge_returning("<tier>reasoning</tier>"),
            tier_models=tier_models,
        )
        assert decision.model_override is not None
        assert decision.model_override.model == "anthropic/claude-opus-4-7"

    async def test_no_override_when_tier_absent_from_map(self) -> None:
        decision = await classify_task(
            task=_make_task(),
            profile=None,
            rules=[],
            judge=_judge_returning("<tier>complex</tier>"),
            tier_models={"reasoning": TierModelOverride(model="x/y")},
        )
        assert decision.model_override is None


# ---------------------------------------------------------------------------
# Judge prompt — ports PilotDeck generateJudgePrompt: tiers + rules + previous.
# ---------------------------------------------------------------------------


class TestJudgePrompt:
    def test_prompt_contains_tiers_rules_and_previous(self) -> None:
        prompt = generate_judge_prompt(
            task_summary="role: coder_worker\ngoal: do thing",
            rules=["my-special-rule"],
            previous_tier="reasoning",
        )
        for tier in TierName:
            assert tier.value in prompt
        assert "my-special-rule" in prompt
        assert "reasoning" in prompt
        assert "<tier>NAME</tier>" in prompt

    def test_prompt_includes_structured_task_summary_not_raw_message(self) -> None:
        # Judge must see a structured summary (fusion doc §4.1), so the task's
        # role/goal/criteria_count appear in the rendered prompt.
        async def _capture_judge(prompt: str) -> str:
            _captured["prompt"] = prompt
            return "<tier>medium</tier>"

        _captured: dict[str, str] = {}
        import asyncio

        asyncio.run(
            classify_task(
                task=_make_task(goal="Add CLI flag"),
                profile=None,
                rules=[],
                judge=_capture_judge,
            )
        )
        assert "role: coder_worker" in _captured["prompt"]
        assert "goal: Add CLI flag" in _captured["prompt"]
        assert "criteria_count: 2" in _captured["prompt"]


# ---------------------------------------------------------------------------
# RouteDecision schema — extra="forbid" round-trip.
# ---------------------------------------------------------------------------


class TestRouteDecisionSchema:
    def test_round_trip(self) -> None:
        decision = RouteDecision(
            tier=TierName.REASONING,
            model_override=TierModelOverride(model="anthropic/claude-opus-4-7"),
            judge_raw="<tier>reasoning</tier>",
            sticky_hit=False,
        )
        restored = RouteDecision.model_validate(decision.model_dump())
        assert restored == decision
        assert restored.tier == TierName.REASONING.value

    def test_minimal_valid(self) -> None:
        decision = RouteDecision(tier=TierName.SIMPLE)
        assert decision.model_override is None
        assert decision.sticky_hit is False
        assert decision.judge_raw == ""

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            RouteDecision(tier=TierName.SIMPLE, bogus="x")  # type: ignore[call-arg]

    def test_model_override_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            TierModelOverride(model="x/y", bogus="x")  # type: ignore[call-arg]
