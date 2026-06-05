"""wasm_node_probe — full Node behavior probe for wasm crates (Phase G).

Strategy (mirrors the lifecycle richness of ``backend_service_health_probe``):

1. ``cargo build --target wasm32-unknown-unknown`` — fail-closed on error.
2. ``wasm-pack build --target nodejs`` — produces the importable ``pkg/``.
3. ``wasm-pack test --node`` — runs the crate's own ``wasm-bindgen-test`` suite
   under Node. A crate with **no** wasm tests degrades gracefully (we detect the
   "no tests" signal and don't false-fail); a real test *failure* is fail-closed
   with the test log captured as evidence.
4. Per assertion: run the configured Node harness command
   (``spec.endpoints_to_probe``) or — when none is configured — a default
   **import smoke** that loads the generated package in Node and fails loud if it
   can't be required/instantiated. (The PR-D1 minimal version treated build+pack
   success as a vacuous pass for every assertion; that gap is what this closes.)

Every observation is 1:1 with an assertion. All execution goes through
``ctx.sandbox.exec`` — never the host shell. Evidence is populated on every fail
path so the runner can persist it (build/pack stderr, the wasm-test log, and
per-assertion node stderr).
"""

from __future__ import annotations

from typing import Any

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation
from .base import ProbeResult, ProbeStrategy

# Module-level so tests can monkeypatch them with fake shell commands.
_BUILD_CMD = "cargo build --target wasm32-unknown-unknown"
_PACK_CMD = "wasm-pack build --target nodejs"
_TEST_CMD = "wasm-pack test --node"
# Default per-assertion check when no node harness is configured: load the
# generated package. `require` resolves ./pkg/package.json's main (wasm-pack
# nodejs output), so a broken/uninstantiable module fails loud here.
_IMPORT_SMOKE_CMD = "node -e \"require('./pkg')\""

# A non-zero `wasm-pack test --node` carrying any of these markers means the
# crate simply has no wasm-bindgen tests / no testable target — a graceful skip,
# NOT a behavior failure. Real test failures lack these and stay fail-closed.
_NO_TESTS_MARKERS = (
    "running 0 tests",
    "no library targets",
    "no test target",
    "no tests to run",
    "0 tests",
)


class WasmNodeProbe(ProbeStrategy):
    name = "wasm_node_probe"

    async def run(
        self,
        ctx: Any,
        spec: BehaviorProbeSpec,
        assertions: list[Assertion],
    ) -> ProbeResult:
        evidence: dict[str, bytes] = {}

        # 1 + 2: build for wasm32, then package with wasm-pack.
        build = await ctx.sandbox.exec(_BUILD_CMD, cwd="/workspace", timeout_sec=spec.timeout_sec)
        if build.exit_code != 0:
            evidence["build.stderr.txt"] = build.stderr.encode("utf-8")
            return self._all_unmatched(
                assertions,
                evidence=evidence,
                observed="wasm32 build failed",
                expected="wasm32 build succeeds",
                failure_reason="wasm32 build failed",
            )

        pack = await ctx.sandbox.exec(_PACK_CMD, cwd="/workspace", timeout_sec=spec.timeout_sec)
        if pack.exit_code != 0:
            evidence["build.stderr.txt"] = build.stderr.encode("utf-8")
            evidence["wasm_pack.stderr.txt"] = pack.stderr.encode("utf-8")
            return self._all_unmatched(
                assertions,
                evidence=evidence,
                observed="wasm-pack build failed",
                expected="wasm-pack build succeeds",
                failure_reason="wasm-pack packaging failed",
            )

        # 3: run the crate's wasm-bindgen test suite under Node. Skip gracefully
        # when the crate has no wasm tests; fail-closed on a real test failure.
        test = await ctx.sandbox.exec(_TEST_CMD, cwd="/workspace", timeout_sec=spec.timeout_sec)
        if test.exit_code != 0 and not _looks_like_no_tests(test):
            evidence["wasm_pack_test.log"] = (test.stdout + "\n" + test.stderr).encode("utf-8")
            return self._all_unmatched(
                assertions,
                evidence=evidence,
                observed=f"wasm-pack test --node exit_code={test.exit_code}",
                expected="wasm-pack test --node passes",
                failure_reason="wasm-pack test --node reported failing tests",
            )

        # 4: per-assertion node checks (configured harness or default import smoke).
        observations: list[BehaviorObservation] = []
        endpoints = spec.endpoints_to_probe
        for i, assertion in enumerate(assertions):
            if endpoints:
                cmd = endpoints[min(i, len(endpoints) - 1)]
                expected = "node check exit_code=0"
            else:
                cmd = _IMPORT_SMOKE_CMD
                expected = "generated package imports in node"
            res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=spec.timeout_sec)
            matched = res.exit_code == 0
            if not matched:
                evidence[f"{assertion.id}.stderr.txt"] = res.stderr.encode("utf-8")
            observations.append(
                BehaviorObservation(
                    assertion_id=assertion.id,
                    observed=f"node check exit_code={res.exit_code}",
                    expected=expected,
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

    def _all_unmatched(
        self,
        assertions: list[Assertion],
        *,
        evidence: dict[str, bytes],
        observed: str,
        expected: str,
        failure_reason: str,
    ) -> ProbeResult:
        return ProbeResult(
            strategy=self.name,
            observations=[
                BehaviorObservation(
                    assertion_id=a.id, observed=observed, expected=expected, matched=False
                )
                for a in assertions
            ],
            evidence=evidence,
            failure_reason=failure_reason,
        )


def _looks_like_no_tests(result: Any) -> bool:
    """True iff a non-zero `wasm-pack test --node` just means 'no wasm tests'."""
    combined = (str(result.stdout) + " " + str(result.stderr)).lower()
    return any(marker in combined for marker in _NO_TESTS_MARKERS)


__all__ = ["WasmNodeProbe"]
