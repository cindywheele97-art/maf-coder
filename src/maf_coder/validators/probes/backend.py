"""backend_service_health_probe — behavior probe for backend services (PR-D1).

Lifecycle:

1. Launch `spec.start_command` in the sandbox, backgrounded, with stdout+stderr
   redirected to a log file under `/workspace`.
2. Poll `spec.ready_check` (a command that returns 0 when the service is up)
   until it succeeds or `spec.timeout_sec` elapses.
3. Probe each entry in `spec.endpoints_to_probe` (a check command that returns
   0 on success, e.g. `curl -sf localhost:8080/health`) — one per assertion.
4. Always tear the service down and capture the service log as evidence.

If the service never becomes ready, every observation is marked unmatched and
`failure_reason` is set; the runner persists the captured log on the fail path.

All process execution goes through `ctx.sandbox.exec` — never the host shell.
"""

from __future__ import annotations

import time
from typing import Any

from ...schemas.contract import Assertion
from ...schemas.profile import BehaviorProbeSpec
from ...schemas.verdict import BehaviorObservation
from .base import ProbeResult, ProbeStrategy

# Pidfile + logfile live under /workspace so the sandbox maps them consistently
# and so cleanup happens inside the sandbox, not on the host.
_LOG_REL = ".maf_behavior_service.log"
_PID_REL = ".maf_behavior_service.pid"
_POLL_INTERVAL_SEC = 0.5


class BackendServiceHealthProbe(ProbeStrategy):
    name = "backend_service_health_probe"

    async def run(
        self,
        ctx: Any,
        spec: BehaviorProbeSpec,
        assertions: list[Assertion],
    ) -> ProbeResult:
        if not spec.start_command:
            return self._all_unmatched(
                spec, assertions, reason="backend probe requires profile.behavior_probe.start_command"
            )

        evidence: dict[str, bytes] = {}
        try:
            await self._start_service(ctx, spec.start_command)
            ready = await self._wait_ready(ctx, spec.ready_check, spec.timeout_sec)
            if not ready:
                log = await self._read_log(ctx)
                evidence["service.log"] = log.encode("utf-8")
                return ProbeResult(
                    strategy=self.name,
                    observations=self._unmatched_observations(
                        assertions, observed="service did not become ready", expected="ready_check exits 0"
                    ),
                    evidence=evidence,
                    failure_reason=f"service ready_check did not pass within {spec.timeout_sec}s",
                )

            observations, ev = await self._probe_endpoints(ctx, spec, assertions)
            evidence.update(ev)
            if any(not o.matched for o in observations):
                log = await self._read_log(ctx)
                evidence["service.log"] = log.encode("utf-8")
            failure_reason = (
                None
                if all(o.matched for o in observations)
                else "one or more endpoint probes failed"
            )
            return ProbeResult(
                strategy=self.name,
                observations=observations,
                evidence=evidence,
                failure_reason=failure_reason,
            )
        finally:
            await self._stop_service(ctx)

    # -- lifecycle helpers -------------------------------------------------

    async def _start_service(self, ctx: Any, start_command: str) -> None:
        # Background the service, record its PID, redirect output to the log.
        launch = (
            f"nohup {start_command} > {_LOG_REL} 2>&1 & echo $! > {_PID_REL}"
        )
        await ctx.sandbox.exec(launch, cwd="/workspace", timeout_sec=30)

    async def _wait_ready(self, ctx: Any, ready_check: str | None, timeout_sec: int) -> bool:
        if not ready_check:
            # No ready check configured — give the service a brief grace then
            # assume up (the endpoint probes are the real gate).
            return True
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            res = await ctx.sandbox.exec(ready_check, cwd="/workspace", timeout_sec=30)
            if res.exit_code == 0:
                return True
            await _async_sleep(_POLL_INTERVAL_SEC)
        return False

    async def _probe_endpoints(
        self, ctx: Any, spec: BehaviorProbeSpec, assertions: list[Assertion]
    ) -> tuple[list[BehaviorObservation], dict[str, bytes]]:
        observations: list[BehaviorObservation] = []
        evidence: dict[str, bytes] = {}
        endpoints = spec.endpoints_to_probe
        for i, assertion in enumerate(assertions):
            if endpoints:
                cmd = endpoints[min(i, len(endpoints) - 1)]
            elif spec.ready_check:
                cmd = spec.ready_check
            else:
                cmd = "true"
            res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=spec.timeout_sec)
            matched = res.exit_code == 0
            observations.append(
                BehaviorObservation(
                    assertion_id=assertion.id,
                    observed=f"probe exit_code={res.exit_code}",
                    expected="probe exit_code=0",
                    matched=matched,
                )
            )
            if not matched:
                evidence[f"{assertion.id}.stderr.txt"] = res.stderr.encode("utf-8")
        return observations, evidence

    async def _read_log(self, ctx: Any) -> str:
        res = await ctx.sandbox.exec(
            f"cat {_LOG_REL} 2>/dev/null || true", cwd="/workspace", timeout_sec=15
        )
        return str(res.stdout)

    async def _stop_service(self, ctx: Any) -> None:
        # Best-effort: kill the recorded PID and remove the pidfile.
        cmd = (
            f"if [ -f {_PID_REL} ]; then kill \"$(cat {_PID_REL})\" 2>/dev/null || true; "
            f"rm -f {_PID_REL}; fi"
        )
        await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=15)

    # -- failure shaping ---------------------------------------------------

    def _unmatched_observations(
        self, assertions: list[Assertion], *, observed: str, expected: str
    ) -> list[BehaviorObservation]:
        return [
            BehaviorObservation(
                assertion_id=a.id, observed=observed, expected=expected, matched=False
            )
            for a in assertions
        ]

    def _all_unmatched(
        self, spec: BehaviorProbeSpec, assertions: list[Assertion], *, reason: str
    ) -> ProbeResult:
        return ProbeResult(
            strategy=self.name,
            observations=self._unmatched_observations(
                assertions, observed=reason, expected="service probe succeeds"
            ),
            evidence={"error.txt": reason.encode("utf-8")},
            failure_reason=reason,
        )


async def _async_sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch the poll delay away."""
    import asyncio

    await asyncio.sleep(seconds)


__all__ = ["BackendServiceHealthProbe"]
