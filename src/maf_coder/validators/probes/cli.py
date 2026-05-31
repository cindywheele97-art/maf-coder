"""cli_assert_cmd_probe — behavior probe for CLI projects (Phase D PR-D1).

Runs the project's CLI binary (built artifact) once per assertion and checks
that it exits 0. The command line for each assertion comes from
`spec.endpoints_to_probe` (one entry per assertion, by index). When there are
fewer entries than assertions the last entry is reused; when there are none we
fall back to `spec.start_command` (treated as the invocation) or a bare
`--help` smoke run so the probe still produces a 1:1 observation set.

Everything runs through `ctx.sandbox.exec` in `/workspace`; never the host
shell.
"""

from __future__ import annotations

from typing import Any

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation
from .base import ProbeResult, ProbeStrategy


class CliAssertCmdProbe(ProbeStrategy):
    name = "cli_assert_cmd_probe"

    def _command_for(self, spec: BehaviorProbeSpec, index: int) -> str:
        endpoints = spec.endpoints_to_probe
        if endpoints:
            return endpoints[min(index, len(endpoints) - 1)]
        if spec.start_command:
            return spec.start_command
        return "--help"

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
                    observed=f"exit_code={res.exit_code}",
                    expected="exit_code=0",
                    matched=matched,
                )
            )
            if not matched:
                evidence[f"{assertion.id}.stdout.txt"] = res.stdout.encode("utf-8")
                evidence[f"{assertion.id}.stderr.txt"] = res.stderr.encode("utf-8")

        failure_reason = (
            None
            if all(o.matched for o in observations)
            else "one or more CLI invocations exited non-zero"
        )
        return ProbeResult(
            strategy=self.name,
            observations=observations,
            evidence=evidence,
            failure_reason=failure_reason,
        )


__all__ = ["CliAssertCmdProbe"]
