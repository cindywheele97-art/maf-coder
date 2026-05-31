"""BehaviorValidator tool factories (AGENT_TOOLS_SPEC §11, Phase D PR-D1).

The BehaviorValidator answers "is the behavior correct?" via headless probes
dispatched by project type. This module exposes the six tools from §11 plus a
probe *runner* that wires the `validators/probes/` strategies to the mission's
profile and contract.

Tool surface (signatures verbatim from §11):
    start_service / stop_service          : long-running service lifecycle
    probe_http / probe_cli                : single behavior probes
    save_behavior_verdict                 : write verdicts/<task>.behavior.json
    save_behavior_evidence                : write behavior_evidence/<task>/<name>

Probe runner (`run_behavior_probes`):
    Reads `profile.behavior_probe` + the `behavior_probe` assertions from the
    locked validation contract, dispatches to the registered strategy, emits
    one `BehaviorObservation` per assertion (1:1), and — on failure — writes
    evidence BEFORE returning. Evidence-on-fail is a hard exit-gate rule.

Every tool routes through `permissions.check_tool_allowed`; every process
execution goes through `ctx.sandbox` (never the host shell).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ...schemas import VerdictResult
from ...schemas.contract import Assertion
from ...schemas.verdict import BehaviorObservation, BehaviorVerdict
from ...validators.probes import ProbeResult, get_probe_strategy
from .._sdk import function_tool
from ..base import TaskContext
from ..errors import ArtifactError, SandboxError, ToolError
from ..permissions import check_tool_allowed
from . import record_tool_call, time_block

# In-process registry of services started by start_service, keyed by the
# service_id we hand back. Scoped per Python process; the sandbox owns the
# real OS processes. Stored as service_id -> (pid_relpath, log_relpath).
_SERVICE_REGISTRY: dict[str, tuple[str, str]] = {}

_BEHAVIOR_VERIFICATION_METHOD = "behavior_probe"


def _actor(ctx: TaskContext) -> str:
    owner = ctx.task.owner
    return owner.value if hasattr(owner, "value") else str(owner)


# ---------------------------------------------------------------------------
# start_service / stop_service
# ---------------------------------------------------------------------------


def make_start_service(ctx: TaskContext) -> Any:
    @function_tool
    async def start_service(
        command: str,
        ready_check: str,
        timeout_sec: int = 300,
    ) -> dict[str, Any]:
        """Start a long-running service command in the sandbox.

        Waits for ready_check to return 0 (or until timeout). Returns:
          {service_id, started_at, log_path}
        """
        check_tool_allowed(ctx.task.permission, "start_service")
        service_id = f"svc-{uuid.uuid4().hex[:8]}"
        log_rel = f".maf_service_{service_id}.log"
        pid_rel = f".maf_service_{service_id}.pid"
        t0 = time_block()

        launch = f"nohup {command} > {log_rel} 2>&1 & echo $! > {pid_rel}"
        await ctx.sandbox.exec(launch, cwd="/workspace", timeout_sec=30)

        ready = await _wait_for_ready(ctx, ready_check, timeout_sec)
        if not ready:
            # Tear down the half-started service before surfacing the error.
            await _kill_service(ctx, pid_rel)
            raise SandboxError(
                f"start_service: ready_check did not pass within {timeout_sec}s "
                f"(command={command!r})"
            )

        _SERVICE_REGISTRY[service_id] = (pid_rel, log_rel)
        record_tool_call(
            ctx,
            "start_service",
            f"service_id={service_id} cmd={command[:80]}",
            duration_sec=time_block() - t0,
        )
        return {
            "service_id": service_id,
            "started_at": time.time(),
            "log_path": log_rel,
        }

    return start_service


def make_stop_service(ctx: TaskContext) -> Any:
    @function_tool
    async def stop_service(service_id: str) -> None:
        """Stop a service started by start_service."""
        check_tool_allowed(ctx.task.permission, "stop_service")
        entry = _SERVICE_REGISTRY.pop(service_id, None)
        if entry is None:
            raise ToolError(f"stop_service: unknown service_id {service_id!r}")
        pid_rel, _log_rel = entry
        await _kill_service(ctx, pid_rel)
        record_tool_call(ctx, "stop_service", f"service_id={service_id}")

    return stop_service


# ---------------------------------------------------------------------------
# probe_http / probe_cli
# ---------------------------------------------------------------------------


def make_probe_http(ctx: TaskContext) -> Any:
    @function_tool
    async def probe_http(
        url: str,
        method: str = "GET",
        body: str | None = None,
        expected_status: int | None = None,
    ) -> dict[str, Any]:
        """HTTP probe against a sandbox-internal URL (typically localhost:PORT)."""
        check_tool_allowed(ctx.task.permission, "probe_http")
        # Use curl inside the sandbox; print the status code on its own line so
        # we can parse it deterministically without a JSON dependency.
        data = f"--data {_shell_quote(body)} " if body is not None else ""
        cmd = (
            f"curl -s -o /dev/null -w '%{{http_code}}' "
            f"-X {_shell_quote(method)} {data}{_shell_quote(url)}"
        )
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=60)
        status = _parse_status(res.stdout)
        matched = (
            res.exit_code == 0
            if expected_status is None
            else (res.exit_code == 0 and status == expected_status)
        )
        record_tool_call(
            ctx,
            "probe_http",
            f"{method} {url} -> {status}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return {
            "status_code": status,
            "exit_code": res.exit_code,
            "expected_status": expected_status,
            "matched": matched,
            "stderr": res.stderr[:2000],
        }

    return probe_http


def make_probe_cli(ctx: TaskContext) -> Any:
    @function_tool
    async def probe_cli(
        binary: str,
        args: list[str] | None = None,
        stdin: str | None = None,
        expected_exit_code: int | None = None,
    ) -> dict[str, Any]:
        """CLI probe — runs a binary with given args, captures stdout/stderr/exit."""
        check_tool_allowed(ctx.task.permission, "probe_cli")
        argv = [binary, *(args or [])]
        cmd = " ".join(_shell_quote(a) for a in argv)
        t0 = time_block()
        res = await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=120, stdin=stdin)
        matched = (
            res.exit_code == 0
            if expected_exit_code is None
            else res.exit_code == expected_exit_code
        )
        record_tool_call(
            ctx,
            "probe_cli",
            f"{binary} args={len(args or [])} exit={res.exit_code}",
            exit_code=res.exit_code,
            duration_sec=time_block() - t0,
        )
        return {
            "exit_code": res.exit_code,
            "expected_exit_code": expected_exit_code,
            "matched": matched,
            "stdout": res.stdout[:8000],
            "stderr": res.stderr[:8000],
        }

    return probe_cli


# ---------------------------------------------------------------------------
# save_behavior_evidence / save_behavior_verdict
# ---------------------------------------------------------------------------


def make_save_behavior_evidence(ctx: TaskContext) -> Any:
    @function_tool
    async def save_behavior_evidence(task_id: str, name: str, content: bytes) -> str:
        """Save behavior_evidence/<task_id>/<name>.

        For traces, logs, recorded responses that BehaviorVerdict.evidence_path
        points to.
        """
        check_tool_allowed(ctx.task.permission, "save_behavior_evidence")
        return _write_evidence(ctx, task_id, name, content)

    return save_behavior_evidence


def make_save_behavior_verdict(ctx: TaskContext) -> Any:
    @function_tool
    async def save_behavior_verdict(
        task_id: str,
        result: str,
        probe_strategy: str,
        observations: list[dict[str, Any]] | None = None,
        evidence_path: str = "",
        failure_reason: str | None = None,
    ) -> str:
        """Save verdicts/<task_id>.behavior.json. Validates against schema."""
        check_tool_allowed(ctx.task.permission, "save_behavior_verdict")
        try:
            coerced = _coerce_observations(observations or [])
            verdict = BehaviorVerdict(
                task_id=task_id,
                result=result,  # type: ignore[arg-type]
                probe_strategy=probe_strategy,
                observations=coerced,
                evidence_path=evidence_path,
                failure_reason=failure_reason,
            )
        except ToolError:
            raise
        except Exception as e:
            raise ArtifactError(f"save_behavior_verdict: validation failed: {e}") from e

        try:
            path = ctx.store.save_behavior_verdict(task_id, verdict)
        except Exception as e:
            raise ArtifactError(f"save_behavior_verdict: store rejected: {e}") from e

        record_tool_call(
            ctx, "save_behavior_verdict", f"task_id={task_id} result={result}"
        )
        ctx.event_log.log_validator_verdict(
            mission_id=ctx.mission_id,
            task_id=task_id,
            validator="behavior_validator",
            result=result,
        )
        return str(path)

    return save_behavior_verdict


# ---------------------------------------------------------------------------
# Probe runner — the part not in §11
# ---------------------------------------------------------------------------


def make_run_behavior_probes(ctx: TaskContext) -> Any:
    @function_tool
    async def run_behavior_probes(task_id: str) -> dict[str, Any]:
        """Run the project-type probe strategy and persist the verdict.

        Reads `profile.behavior_probe` and the `behavior_probe` assertions from
        the locked validation contract, dispatches the registered strategy,
        emits one observation per assertion (1:1), writes evidence on the fail
        path BEFORE returning, then saves the BehaviorVerdict. Returns:
          {verdict_path, result, observations, evidence_path}
        """
        check_tool_allowed(ctx.task.permission, "run_behavior_probes")
        result = await run_behavior_probes_impl(ctx, task_id)
        return result

    return run_behavior_probes


async def run_behavior_probes_impl(ctx: TaskContext, task_id: str) -> dict[str, Any]:
    """Core probe-runner logic (separated so it is unit-testable without SDK).

    Steps:
      1. Load profile + behavior_probe spec.
      2. Load contract, filter assertions where verification_method == behavior_probe.
      3. Resolve the strategy from the registry and run it.
      4. On failure: persist evidence (stdout/stderr/log) BEFORE returning.
      5. Save the BehaviorVerdict (1:1 observations).
    """
    try:
        profile = ctx.store.load_project_profile()
    except Exception as e:
        raise ArtifactError(f"run_behavior_probes: cannot load project profile: {e}") from e
    spec = profile.behavior_probe

    try:
        contract = ctx.store.load_validation_contract()
    except Exception as e:
        raise ArtifactError(f"run_behavior_probes: cannot load validation contract: {e}") from e

    assertions = _behavior_assertions(contract)
    if not assertions:
        raise ToolError(
            "run_behavior_probes: validation contract has no behavior_probe assertions"
        )

    try:
        strategy = get_probe_strategy(spec.strategy)
    except KeyError as e:
        raise ToolError(str(e)) from e

    t0 = time_block()
    probe_result: ProbeResult = await strategy.run(ctx, spec, assertions)

    _assert_one_to_one(assertions, probe_result)

    # Evidence-on-fail (hard exit-gate). Persist BEFORE returning / saving.
    evidence_path = ""
    if not probe_result.passed and probe_result.evidence:
        for name, content in probe_result.evidence.items():
            _write_evidence(ctx, task_id, name, content)
        evidence_path = f"behavior_evidence/{task_id}"

    result_value = VerdictResult.PASS.value if probe_result.passed else VerdictResult.FAIL.value
    verdict = BehaviorVerdict(
        task_id=task_id,
        result=result_value,  # type: ignore[arg-type]
        probe_strategy=probe_result.strategy,
        observations=probe_result.observations,
        evidence_path=evidence_path,
        failure_reason=probe_result.failure_reason if not probe_result.passed else None,
    )
    try:
        path = ctx.store.save_behavior_verdict(task_id, verdict)
    except Exception as e:
        raise ArtifactError(f"run_behavior_probes: store rejected verdict: {e}") from e

    record_tool_call(
        ctx,
        "run_behavior_probes",
        f"task_id={task_id} strategy={probe_result.strategy} result={result_value}",
        duration_sec=time_block() - t0,
    )
    ctx.event_log.log_validator_verdict(
        mission_id=ctx.mission_id,
        task_id=task_id,
        validator="behavior_validator",
        result=result_value,
    )
    return {
        "verdict_path": str(path),
        "result": result_value,
        "observations": [o.model_dump(mode="json") for o in probe_result.observations],
        "evidence_path": evidence_path,
    }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_behavior_tools(ctx: TaskContext) -> list[Any]:
    """Build the BehaviorValidator tool set bound to `ctx` (§11 + probe runner)."""
    return [
        make_start_service(ctx),
        make_stop_service(ctx),
        make_probe_http(ctx),
        make_probe_cli(ctx),
        make_save_behavior_verdict(ctx),
        make_save_behavior_evidence(ctx),
        make_run_behavior_probes(ctx),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _behavior_assertions(contract: Any) -> list[Assertion]:
    """Flatten contract features and keep only behavior_probe assertions."""
    out: list[Assertion] = []
    for feature in contract.features:
        for assertion in feature.assertions:
            method = assertion.verification_method
            method_val = method.value if hasattr(method, "value") else str(method)
            if method_val == _BEHAVIOR_VERIFICATION_METHOD:
                out.append(assertion)
    return out


def _assert_one_to_one(assertions: list[Assertion], result: ProbeResult) -> None:
    """Enforce the 1:1 assertion<->observation contract (hard invariant)."""
    expected_ids = [a.id for a in assertions]
    observed_ids = [o.assertion_id for o in result.observations]
    if observed_ids != expected_ids:
        raise ToolError(
            "run_behavior_probes: strategy violated 1:1 observation rule "
            f"(assertions={expected_ids}, observations={observed_ids})"
        )


def _coerce_observations(raw: list[dict[str, Any]]) -> list[BehaviorObservation]:
    out: list[BehaviorObservation] = []
    for i, obs in enumerate(raw):
        try:
            out.append(BehaviorObservation(**obs))
        except Exception as e:
            raise ToolError(f"observation[{i}] invalid: {e}") from e
    return out


def _write_evidence(ctx: TaskContext, task_id: str, name: str, content: bytes) -> str:
    """Persist one evidence blob under behavior_evidence/<task_id>/<name>.

    Routes through ArtifactStore so the path-escape rejection holds. Bytes are
    decoded as UTF-8 (replacing undecodable bytes) since the store is text-based;
    behavior evidence is logs / traces / recorded responses (text).
    """
    relpath = f"behavior_evidence/{task_id}/{name}"
    try:
        text = content.decode("utf-8") if isinstance(content, bytes | bytearray) else str(content)
        ctx.store.write_text(relpath, text)
    except Exception as e:
        raise ArtifactError(f"save_behavior_evidence: {e}") from e
    record_tool_call(ctx, "save_behavior_evidence", f"task_id={task_id} name={name}")
    ctx.event_log.log_artifact_written(
        mission_id=ctx.mission_id,
        actor=_actor(ctx),
        path=relpath,
        task_id=task_id,
    )
    return relpath


async def _wait_for_ready(ctx: TaskContext, ready_check: str, timeout_sec: int) -> bool:
    import asyncio

    if not ready_check:
        return True
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        res = await ctx.sandbox.exec(ready_check, cwd="/workspace", timeout_sec=30)
        if res.exit_code == 0:
            return True
        await asyncio.sleep(0.5)
    return False


async def _kill_service(ctx: TaskContext, pid_rel: str) -> None:
    cmd = (
        f"if [ -f {pid_rel} ]; then kill \"$(cat {pid_rel})\" 2>/dev/null || true; "
        f"rm -f {pid_rel}; fi"
    )
    await ctx.sandbox.exec(cmd, cwd="/workspace", timeout_sec=15)


def _shell_quote(value: str) -> str:
    """Single-quote a value for safe inclusion in a shell command."""
    return "'" + value.replace("'", "'\\''") + "'"


def _parse_status(stdout: str) -> int | None:
    text = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
    try:
        return int(text)
    except ValueError:
        return None


__all__ = [
    "build_behavior_tools",
    "make_probe_cli",
    "make_probe_http",
    "make_run_behavior_probes",
    "make_save_behavior_evidence",
    "make_save_behavior_verdict",
    "make_start_service",
    "make_stop_service",
    "run_behavior_probes_impl",
]
