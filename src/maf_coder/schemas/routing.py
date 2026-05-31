"""Smart Router routing decision schema (Smart Router PR-SR1).

Ports the *output shape* of PilotDeck's tokenSaver tier classification into the
MAF-Coder schema layer. A `RouteDecision` is the structured result of running
the Judge LLM over a Task (see `models.tier_router.classify_task`).

Design source: `docs/PILOTDECK_SMART_ROUTER_FUSION.md` §2.2, §4.

Four tiers, calibrated against PinchBench:

- ``simple``    — greetings / confirmations / single-step Q&A → cheapest model.
- ``medium``    — single tool call / short code → mid model.
- ``reasoning`` — hard but a *single agent* can finish it → strong model, NO spawn.
- ``complex``   — **the Orchestrator should split this into a DAG of tasks.**

SEMANTIC RULE (do not weaken): ``complex`` means "Orchestrator re-planning
signal — split a new Task into the mission DAG". It MUST NOT be interpreted as
"spawn an SDK sub-agent inside a turn". MAF-Coder's Scheduler + tasks.yaml owns
plan-level orchestration; turn-level auto-orchestration is explicitly out of
scope (execution plan §1, §4 SR invariants). SR-2 wires this signal correctly.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .common import RiskLevel  # noqa: F401  (kept for downstream tier-rule docs)


class TierName(str, Enum):
    """The four PinchBench tiers, cheapest → most capable / orchestration-heavy."""

    SIMPLE = "simple"
    MEDIUM = "medium"
    REASONING = "reasoning"
    COMPLEX = "complex"


class TierModelOverride(BaseModel):
    """A tier's model selection, shaped to be ``ModelConfig``-compatible.

    Fields mirror ``maf_coder.models.router.ModelConfig`` (model / temperature /
    max_tokens) so SR-2 can hand this straight to the RoleRouter when applying a
    tier over a role's primary.

    INVARIANT (execution plan §1.5): this override is NOT yet constrained by the
    different-provider rule. SR-2 MUST still pass it through
    ``ModelRouter`` forbidden-provider / validator-≠-coder enforcement when it
    applies the tier. Selecting a model here never bypasses that code invariant.
    """

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="LiteLLM model string, e.g. 'anthropic/claude-opus-4-7'")
    temperature: float = 0.2
    max_tokens: int = 8000


class RouteDecision(BaseModel):
    """Structured output of the tier Judge for one Task.

    Carries everything SR-2 needs to apply (or audit) a routing decision:
    the chosen tier, an optional model override, the raw judge text (for the
    audit trail / debugging unparseable output), and whether sticky continuation
    reused the previous tier.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    tier: TierName = Field(description="The classified tier for this task.")
    model_override: TierModelOverride | None = Field(
        default=None,
        description=(
            "Tier-selected model, ModelConfig-compatible. None ⇒ caller keeps the "
            "role's primary. SR-2 applies this still subject to forbidden_providers."
        ),
    )
    judge_raw: str = Field(
        default="",
        description="Raw Judge LLM output (the text the <tier> tag was parsed from).",
    )
    sticky_hit: bool = Field(
        default=False,
        description="True when sticky continuation reused previous_tier on ambiguous output.",
    )
