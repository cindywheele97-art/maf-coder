"""wasm_node_probe — minimal behavior probe for wasm crates (PR-D1).

Minimal strategy: build the crate for `wasm32-unknown-unknown` and package it
with `wasm-pack`, then (per assertion) run a check command from
`spec.endpoints_to_probe` (e.g. a node harness that imports the generated
package). With no endpoints we treat the build+pack itself as the single probe
for every assertion.

A fuller probe (driving the package through a node test runner with rich
assertions) is out of scope for PR-D1. All execution goes through
`ctx.sandbox.exec`.
"""

from __future__ import annotations

from typing import Any

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation
from .base import ProbeResult, ProbeStrategy

_BUILD_CMD = "cargo build --target wasm32-unknown-unknown"
_PACK_CMD = "wasm-pack build --target nodejs"


class WasmNodeProbe(ProbeStrategy):
    name = "wasm_node_probe"

    async def run(
        self,
        ctx: Any,
        spec: BehaviorProbeSpec,
        assertions: list[Assertion],
    ) -> ProbeResult:
        evidence: dict[str, bytes] = {}

        # Build for wasm32, then package with wasm-pack. A failure in either
        # step fails the whole probe (every assertion marked unmatched).
        build = await ctx.sandbox.exec(_BUILD_CMD, cwd="/workspace", timeout_sec=spec.timeout_sec)
        pack = (
            await ctx.sandbox.exec(_PACK_CMD, cwd="/workspace", timeout_sec=spec.timeout_sec)
            if build.exit_code == 0
            else None
        )
        build_ok = build.exit_code == 0 and (pack is None or pack.exit_code == 0)

        if not build_ok:
            evidence["build.stderr.txt"] = build.stderr.encode("utf-8")
            if pack is not None:
                evidence["wasm_pack.stderr.txt"] = pack.stderr.encode("utf-8")
            return ProbeResult(
                strategy=self.name,
                observations=[
                    BehaviorObservation(
                        assertion_id=a.id,
                        observed="wasm build/pack failed",
                        expected="wasm32 build + wasm-pack succeed",
                        matched=False,
                    )
                    for a in assertions
                ],
                evidence=evidence,
                failure_reason="wasm32 build or wasm-pack packaging failed",
            )

        # Build+pack succeeded. Per-assertion node checks (if configured).
        observations: list[BehaviorObservation] = []
        endpoints = spec.endpoints_to_probe
        for i, assertion in enumerate(assertions):
            if endpoints:
                cmd = endpoints[min(i, len(endpoints) - 1)]
                res = await ctx.sandbox.exec(
                    cmd, cwd="/workspace", timeout_sec=spec.timeout_sec
                )
                matched = res.exit_code == 0
                observed = f"node check exit_code={res.exit_code}"
                if not matched:
                    evidence[f"{assertion.id}.stderr.txt"] = res.stderr.encode("utf-8")
            else:
                matched = True
                observed = "wasm32 build + wasm-pack succeeded"
            observations.append(
                BehaviorObservation(
                    assertion_id=assertion.id,
                    observed=observed,
                    expected="node check exit_code=0" if endpoints else "build+pack succeed",
                    matched=matched,
                )
            )

        failure_reason = (
            None if all(o.matched for o in observations) else "one or more node checks failed"
        )
        return ProbeResult(
            strategy=self.name,
            observations=observations,
            evidence=evidence,
            failure_reason=failure_reason,
        )


__all__ = ["WasmNodeProbe"]
