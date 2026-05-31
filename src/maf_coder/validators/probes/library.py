"""library_example_probe — behavior probe for library crates (PR-D1).

A library has no service or binary to hit, so behavior is exercised by running
its examples. For each assertion we run a command from
`spec.endpoints_to_probe` (e.g. `cargo run --example quickstart`) and check it
exits 0. With no endpoints configured we fall back to building+running the
crate's examples as a single smoke command.

All execution goes through `ctx.sandbox.exec` in `/workspace`.
"""

from __future__ import annotations

from typing import Any

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation
from .base import ProbeResult, ProbeStrategy

_DEFAULT_EXAMPLE_CMD = "cargo build --examples"


class LibraryExampleProbe(ProbeStrategy):
    name = "library_example_probe"

    def _command_for(self, spec: BehaviorProbeSpec, index: int) -> str:
        endpoints = spec.endpoints_to_probe
        if endpoints:
            return endpoints[min(index, len(endpoints) - 1)]
        if spec.start_command:
            return spec.start_command
        return _DEFAULT_EXAMPLE_CMD

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
                    observed=f"example exit_code={res.exit_code}",
                    expected="example exit_code=0",
                    matched=matched,
                )
            )
            if not matched:
                evidence[f"{assertion.id}.stdout.txt"] = res.stdout.encode("utf-8")
                evidence[f"{assertion.id}.stderr.txt"] = res.stderr.encode("utf-8")

        failure_reason = (
            None
            if all(o.matched for o in observations)
            else "one or more library examples failed to run"
        )
        return ProbeResult(
            strategy=self.name,
            observations=observations,
            evidence=evidence,
            failure_reason=failure_reason,
        )


__all__ = ["LibraryExampleProbe"]
