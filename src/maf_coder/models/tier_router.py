"""TierRouter — complexity/cost-aware tier classification (Smart Router PR-SR1).

Ports PilotDeck's ``tokenSaver`` classifier (``classifyAndRoute.ts`` +
``generateJudgePrompt.ts``) into MAF-Coder. A cheap "Judge" model reads a
**structured Task summary** (not a raw user message — see fusion doc §4.1) and
emits exactly ``<tier>NAME</tier>``. We parse that tag into a
:class:`~maf_coder.schemas.routing.RouteDecision`.

Three behaviours ported faithfully from PilotDeck:

1. **<tier> parse** — extract the tier name from ``<tier>…</tier>``.
2. **Continuation sticky** — when ``previous_tier`` is set and the judge output
   is ambiguous (unparseable OR a short continuation like "continue"/"继续"),
   *stay* on ``previous_tier`` rather than collapsing to ``simple``.
3. **defaultTier fallback** — when parsing fails and there is no previous tier
   to stick to, fall back to a configurable default (PilotDeck default:
   ``medium``).

Tier semantics (PinchBench): simple / medium / reasoning / complex.
``complex`` ⇒ Orchestrator splits a DAG task — NOT an SDK sub-agent spawn
(see RouteDecision docstring + execution plan §1/§4). SR-2 enforces that.

The Judge call is **injectable**: pass a ``judge`` callable. This keeps unit
tests off the live API — they pass a stub returning canned ``<tier>`` text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from ..schemas.routing import RouteDecision, TierModelOverride, TierName

logger = logging.getLogger(__name__)

# A Judge is anything that takes the rendered prompt and returns the model's raw
# text. Real impl wraps ModelRouter.complete (wired in SR-2); tests pass a stub.
JudgeFn = Callable[[str], Awaitable[str]]

# PilotDeck default when classification is unrecoverable.
DEFAULT_TIER: TierName = TierName.MEDIUM

# Short continuation messages that should inherit previous_tier rather than be
# re-classified (PilotDeck SessionRouterStore sticky / generateJudgePrompt).
# Lower-cased, stripped of surrounding punctuation before comparison.
STICKY_CONTINUATIONS: frozenset[str] = frozenset(
    {"continue", "ok", "okay", "yes", "go", "go on", "好的", "继续", "嗯"}
)

# PinchBench tier descriptions injected into the Judge prompt (fusion doc §2.2).
_TIER_DESCRIPTIONS: dict[TierName, str] = {
    TierName.SIMPLE: "Greeting, confirmation, or a single-step Q&A. Use the cheapest model.",
    TierName.MEDIUM: "A single tool call or a short, self-contained code change.",
    TierName.REASONING: (
        "Hard but a SINGLE agent can finish it (e.g. cross-crate refactor, new "
        "public API). Use a strong model. Do NOT spawn sub-agents."
    ),
    TierName.COMPLEX: (
        "Requires the Orchestrator to SPLIT this into a DAG of tasks "
        "(research ∥ security → coder → review → behavior). Does NOT mean spawn "
        "an in-turn sub-agent."
    ),
}

# Matches <tier>NAME</tier>, case-insensitive, tolerant of whitespace.
_TIER_TAG_RE = re.compile(r"<tier>\s*([a-zA-Z]+)\s*</tier>", re.IGNORECASE)


def generate_judge_prompt(
    *,
    task_summary: str,
    rules: list[str],
    previous_tier: str | None,
) -> str:
    """Render the Judge prompt (port of PilotDeck ``generateJudgePrompt.ts``).

    Inputs: a structured task summary, natural-language routing ``rules`` (from
    ``smart_router.rules`` / ``.maf/rules/routing.md``), and an optional
    ``previous_tier`` to bias continuations. Output instruction: emit ONLY
    ``<tier>NAME</tier>``.
    """
    tier_lines = "\n".join(
        f"- {tier.value}: {desc}" for tier, desc in _TIER_DESCRIPTIONS.items()
    )
    rule_lines = "\n".join(f"- {r}" for r in rules) if rules else "- (none)"
    prev_line = (
        f"\nThe previous task was classified as: {previous_tier}. "
        "If this task is a short continuation or you are unsure, keep that tier.\n"
        if previous_tier
        else ""
    )
    return (
        "You are a routing Judge. Classify the task below into exactly one tier.\n\n"
        "Tiers:\n"
        f"{tier_lines}\n\n"
        "Routing rules (heuristics, apply where relevant):\n"
        f"{rule_lines}\n"
        f"{prev_line}\n"
        "Task:\n"
        f"{task_summary}\n\n"
        "Respond with ONLY the tag, nothing else: <tier>NAME</tier>"
    )


def _summarize_task(task: object, profile: object | None) -> str:
    """Build the structured Task summary fed to the Judge (fusion doc §4.1).

    Uses ``getattr`` so this works for a real ``Task`` model without importing it
    (avoids a schema import cycle and keeps SR-1 decoupled). Missing fields are
    simply omitted.
    """
    fields = [
        ("role", getattr(getattr(task, "owner", None), "value", getattr(task, "owner", None))),
        ("goal", getattr(task, "goal", None)),
        ("risk", getattr(getattr(task, "risk_level", None), "value", getattr(task, "risk_level", None))),
        ("depends_on", getattr(task, "depends_on", None)),
    ]
    criteria = getattr(task, "acceptance_criteria", None)
    if criteria is not None:
        fields.append(("criteria_count", len(criteria)))
    if profile is not None:
        ptype = getattr(getattr(profile, "project_type", None), "value", None)
        if ptype is not None:
            fields.append(("project_type", ptype))
    lines = [f"{k}: {v}" for k, v in fields if v is not None]
    return "\n".join(lines)


def parse_tier(text: str) -> TierName | None:
    """Parse ``<tier>NAME</tier>`` from Judge output.

    Returns the matched :class:`TierName`, or ``None`` when the tag is absent or
    names an unknown tier (caller then applies sticky / defaultTier).
    """
    match = _TIER_TAG_RE.search(text or "")
    if not match:
        return None
    name = match.group(1).strip().lower()
    try:
        return TierName(name)
    except ValueError:
        logger.warning("Judge emitted unknown tier %r; treating as unparseable.", name)
        return None


def _is_short_continuation(task_summary: str) -> bool:
    """True when the task summary's goal reads as a bare continuation cue."""
    for line in task_summary.splitlines():
        if line.startswith("goal:"):
            goal = line[len("goal:") :].strip().strip(".!? 。！？").lower()  # noqa: RUF001
            return goal in STICKY_CONTINUATIONS
    return False


def _resolve_tier(
    parsed: TierName | None,
    *,
    previous_tier: str | None,
    sticky_continuation: bool,
    default_tier: TierName,
) -> tuple[TierName, bool]:
    """Apply sticky + defaultTier fallback. Returns ``(tier, sticky_hit)``.

    Precedence (PilotDeck):
    1. A cleanly parsed tier wins outright.
    2. Otherwise, if a valid ``previous_tier`` exists (ambiguous output or a
       short continuation cue), stick to it → ``sticky_hit=True``.
    3. Otherwise fall back to ``default_tier``.
    """
    if parsed is not None and not (sticky_continuation and previous_tier):
        return parsed, False

    if previous_tier is not None:
        try:
            return TierName(previous_tier), True
        except ValueError:
            logger.warning("Invalid previous_tier %r; ignoring for sticky.", previous_tier)

    if parsed is not None:
        return parsed, False
    return default_tier, False


async def classify_task(
    *,
    task: object,
    profile: object | None,
    rules: list[str],
    judge: JudgeFn,
    previous_tier: str | None = None,
    default_tier: TierName = DEFAULT_TIER,
    tier_models: dict[str, TierModelOverride] | None = None,
) -> RouteDecision:
    """Classify a Task into a tier via the (injectable) Judge.

    Args:
        task: a ``Task`` (duck-typed via getattr — owner/goal/risk_level/…).
        profile: optional ``ProjectProfile`` for extra Judge context.
        rules: natural-language routing heuristics injected into the prompt.
        judge: async callable ``(prompt) -> raw_text``. Wraps the cheap Judge
            model in production; a stub in tests. Keeps this off the live API.
        previous_tier: last task's tier, for continuation sticky. ``None`` first.
        default_tier: fallback when parsing fails and no previous tier (medium).
        tier_models: optional ``tier_name -> TierModelOverride`` map (from yaml).
            When the resolved tier has an entry, it is attached as
            ``model_override`` — still subject to SR-2 provider enforcement.

    Returns:
        A :class:`RouteDecision` (tier, optional model_override, judge_raw,
        sticky_hit). Never raises on bad Judge output — falls back instead.
    """
    task_summary = _summarize_task(task, profile)
    prompt = generate_judge_prompt(
        task_summary=task_summary, rules=rules, previous_tier=previous_tier
    )

    try:
        raw = await judge(prompt)
    except Exception as e:  # judge failures must NOT crash routing — fall back.
        logger.warning("Judge call failed (%r); falling back to sticky/default tier.", e)
        raw = ""

    parsed = parse_tier(raw)
    sticky_continuation = parsed is None or _is_short_continuation(task_summary)
    tier, sticky_hit = _resolve_tier(
        parsed,
        previous_tier=previous_tier,
        sticky_continuation=sticky_continuation,
        default_tier=default_tier,
    )

    override = (tier_models or {}).get(tier.value)
    return RouteDecision(
        tier=tier,
        model_override=override,
        judge_raw=raw,
        sticky_hit=sticky_hit,
    )
