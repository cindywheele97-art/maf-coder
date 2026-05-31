"""embedded_host_test_probe — minimal behavior probe for embedded crates (PR-D1).

Embedded targets can't be run on the host, so behavior is exercised by the
crate's host-side test suite (the portable logic compiled for the host). This
minimal strategy runs `cargo test` (or an override from
`spec.start_command` / `endpoints_to_probe`) per assertion and checks exit 0.

A fuller probe (flashing to hardware / QEMU) is out of scope for PR-D1.
All execution goes through `ctx.sandbox.exec`.
"""

from __future__ import annotations

from typing import Any

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation
from .base import ProbeResult, ProbeStrategy

_DEFAULT_HOST_TEST_CMD = "cargo test --workspace"


class EmbeddedHostTestProbe(ProbeStrategy):
    name = "embedded_host_test_probe"

    def _command_for(self, spec: BehaviorProbeSpec, index: int) -> str:
        endpoints = spec.endpoints_to_probe
        if endpoints:
            return endpoints[min(index, len(endpoints) - 1)]
        if spec.start_command:
            return spec.start_command
        return _DEFAULT_HOST_TEST_CMD

    async def run(
        self,
        ctx: Any,
        spec: BehaviorProbeSpec,
        assertions: list[Assertion],
    ) -> ProbeResult:
        observations: list[BehaviorObservation] = []
        evidence: dict[str, bytes] = {}

        for i, assertion in enumerate(assertions):
            cmd = self._command_for(spec, i)
            res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=spec.timeout_sec)
            matched = res.exit_code == 0
            observations.append(
                BehaviorObservation(
                    assertion_id=assertion.id,
                    observed=f"host_test exit_code={res.exit_code}",
                    expected="host_test exit_code=0",
                    matched=matched,
                )
            )
            if not matched:
                evidence[f"{assertion.id}.stderr.txt"] = res.stderr.encode("utf-8")

        failure_reason = (
            None if all(o.matched for o in observations) else "host-side embedded tests failed"
        )
        return ProbeResult(
            strategy=self.name,
            observations=observations,
            evidence=evidence,
            failure_reason=failure_reason,
        )


__all__ = ["EmbeddedHostTestProbe"]
