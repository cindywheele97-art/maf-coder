"""ProbeStrategy ABC + ProbeResult (Phase D PR-D1).

A probe strategy is a headless behavior check dispatched by project type.
Given the mission's `BehaviorProbeSpec` and the `behavior_probe` assertions
from the locked validation contract, it runs the relevant commands *through
the sandbox* and returns a `ProbeResult`.

The result is an in-process dataclass (mirroring `CommandResult` / `_RawResult`
in this codebase, not a persisted Pydantic model). The probe *runner* in
`behavior_tools.py` turns the result into a `BehaviorVerdict` and persists any
evidence — strategies never touch the ArtifactStore directly. This keeps
strategies pure (sandbox in, observations out) and testable in isolation.

Contract between runner and strategy:

- One `BehaviorObservation` per assertion, 1:1 (the runner verifies this).
- `evidence` maps a filename to raw bytes (stdout / stderr / service log). The
  runner persists it via `save_behavior_evidence` on the fail path before
  returning. Strategies SHOULD always populate evidence on failure.
- `matched` per observation drives `result`: PASS iff all observations matched.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of running one probe strategy over a set of assertions.

    `observations` is 1:1 with the assertions handed to the strategy.
    `evidence` is a name -> raw-bytes map the runner persists under
    `behavior_evidence/<task_id>/<name>`. `failure_reason` is set iff the
    strategy could not even run the probe (e.g. service never came up) — this
    is distinct from an assertion simply not matching.
    """

    strategy: str
    observations: list[BehaviorObservation]
    evidence: dict[str, bytes] = field(default_factory=dict)
    failure_reason: str | None = None

    @property
    def matched_all(self) -> bool:
        """True iff every observation matched. Empty observations => vacuously True."""
        return all(o.matched for o in self.observations)

    @property
    def passed(self) -> bool:
        """A probe passes only when it ran AND every observation matched."""
        return self.failure_reason is None and self.matched_all


class ProbeStrategy(ABC):
    """Base class for behavior probe strategies.

    Subclasses set `name` (the registry key, matching
    `BehaviorProbeSpec.strategy`) and implement `run`.

    `run` MUST go through `ctx.sandbox` for every process execution and MUST
    return exactly one `BehaviorObservation` per assertion in `assertions`.
    On failure it SHOULD populate `evidence` so the runner can persist it.
    """

    #: Registry key; must equal a `BehaviorProbeSpec.strategy` value.
    name: str = "abstract"

    @abstractmethod
    async def run(
        self,
        ctx: object,
        spec: BehaviorProbeSpec,
        assertions: list[Assertion],
    ) -> ProbeResult:
        """Run the probe and return a ProbeResult.

        `ctx` is the `TaskContext` (typed as object here to avoid an import
        cycle with `agents.base`). Implementations use `ctx.sandbox` only.
        """


__all__ = ["ProbeResult", "ProbeStrategy"]
