#!/usr/bin/env python3
"""Phase A smoke test — validate provider × model × test_case stability.

Why this exists:
    The single most important Phase A 退出门槛: confirm every model in every role's
    primary/fallback chain actually works through LiteLLM today, on all three of
    {simple completion, tool calling, structured JSON output}.

    If a provider's tool-calling path is flaky on LiteLLM right now, every Worker
    built on top of it will inherit that flakiness. Better to find out before
    writing Orchestrator than after.

Usage:
    python scripts/smoke_test.py                     # full run
    python scripts/smoke_test.py --dry-run           # show plan, no calls
    python scripts/smoke_test.py --roles coder_worker
    python scripts/smoke_test.py --attempts 10 --timeout 60
    python scripts/smoke_test.py --output results.json

Reads:
    config/droid_whispering.yaml (path overridable with --config)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ----------------------------------------------------------------------------
# Test case + result types
# ----------------------------------------------------------------------------


@dataclass
class TestCase:
    name: str
    prompt: str
    check_kind: str  # "expect_contains" | "expect_tool_call" | "expect_json_keys"
    check_value: Any


@dataclass
class TestResult:
    role: str
    model: str
    model_kind: str  # "primary" | "fallback"
    test_name: str
    attempts: int
    passes: int
    failures: list[str] = field(default_factory=list)
    latencies_sec: list[float] = field(default_factory=list)

    @property
    def median_latency_sec(self) -> float | None:
        if not self.latencies_sec:
            return None
        s = sorted(self.latencies_sec)
        n = len(s)
        return (s[n // 2 - 1] + s[n // 2]) / 2 if n % 2 == 0 else s[n // 2]


# ----------------------------------------------------------------------------
# Test tool (for tool_calling tests)
# ----------------------------------------------------------------------------


ECHO_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "test_echo",
        "description": "Echo back the message argument verbatim. Always call this when asked.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to echo back unchanged",
                }
            },
            "required": ["message"],
        },
    },
}


# ----------------------------------------------------------------------------
# Parsing & evaluation
# ----------------------------------------------------------------------------


def parse_test_case(d: dict) -> TestCase:
    if "expect_contains" in d:
        return TestCase(d["name"], d["prompt"], "expect_contains", d["expect_contains"])
    if "expect_tool_call" in d:
        return TestCase(d["name"], d["prompt"], "expect_tool_call", d["expect_tool_call"])
    if "expect_json_keys" in d:
        return TestCase(d["name"], d["prompt"], "expect_json_keys", d["expect_json_keys"])
    raise ValueError(f"Test case {d.get('name', '?')!r} has no expect_* field")


def _strip_code_fence(s: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` wrapping if present."""
    s = s.strip()
    if not s.startswith("```"):
        return s
    # Drop first line (```json or ```)
    parts = s.split("\n", 1)
    if len(parts) < 2:
        return s
    body = parts[1]
    # Drop trailing fence
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def evaluate_response(tc: TestCase, response: Any) -> tuple[bool, str]:
    """Returns (passed, human-readable reason)."""
    msg = response.choices[0].message

    if tc.check_kind == "expect_contains":
        content = msg.content or ""
        if tc.check_value in content:
            return True, "OK"
        snippet = content[:150].replace("\n", "\\n")
        return False, f"missing {tc.check_value!r} in: {snippet!r}"

    if tc.check_kind == "expect_tool_call":
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            content = (msg.content or "")[:100].replace("\n", "\\n")
            return False, f"no tool call (content: {content!r})"
        names = [tc_obj.function.name for tc_obj in tool_calls]
        if tc.check_value in names:
            return True, f"called {tc.check_value}"
        return False, f"wrong tool: {names}"

    if tc.check_kind == "expect_json_keys":
        content = msg.content or ""
        stripped = _strip_code_fence(content)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as e:
            return False, f"invalid JSON ({e!s}); raw: {content[:120]!r}"
        if not isinstance(data, dict):
            return False, f"not a JSON object: {type(data).__name__}"
        missing = [k for k in tc.check_value if k not in data]
        if missing:
            return False, f"missing keys {missing}; got keys {list(data)}"
        return True, "OK"

    return False, f"unknown check_kind: {tc.check_kind}"


# ----------------------------------------------------------------------------
# Test runner
# ----------------------------------------------------------------------------


async def run_one_combo(
    role: str,
    model: str,
    model_kind: str,
    tc: TestCase,
    *,
    attempts: int,
    timeout_sec: float,
) -> TestResult:
    from litellm import acompletion  # type: ignore[import-not-found]

    result = TestResult(role=role, model=model, model_kind=model_kind,
                         test_name=tc.name, attempts=attempts, passes=0)

    for i in range(attempts):
        messages = [{"role": "user", "content": tc.prompt}]
        tools = [ECHO_TOOL_SCHEMA] if tc.check_kind == "expect_tool_call" else None

        t0 = time.monotonic()
        try:
            response = await asyncio.wait_for(
                acompletion(
                    model=model,
                    messages=messages,
                    tools=tools,
                    temperature=0.0,
                    max_tokens=500,
                ),
                timeout=timeout_sec,
            )
            result.latencies_sec.append(time.monotonic() - t0)
            passed, reason = evaluate_response(tc, response)
            if passed:
                result.passes += 1
            else:
                result.failures.append(f"#{i+1}: {reason}")
        except TimeoutError:
            result.failures.append(f"#{i+1}: timeout after {timeout_sec}s")
        except Exception as e:
            result.failures.append(f"#{i+1}: {type(e).__name__}: {e!s}")

    return result


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def check_env_keys() -> dict[str, bool]:
    """Detect which provider API keys are present in env."""
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai": bool(os.environ.get("OPENAI_API_KEY")),
        "google": bool(
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        ),
    }


def parse_pass_criterion(s: str) -> tuple[int, int]:
    """'5/5' → (5, 5)."""
    parts = s.split("/")
    if len(parts) != 2:
        raise ValueError(f"Invalid pass_criterion {s!r} (expected like '5/5')")
    return int(parts[0]), int(parts[1])


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def print_header(args: argparse.Namespace, config_path: Path, attempts: int,
                  required_passes: int, expected_attempts: int) -> None:
    print("=" * 72)
    print("MAF-Coder Phase A Smoke Test")
    print("=" * 72)
    print(f"Config:          {config_path}")
    print(f"Attempts/combo:  {attempts}")
    print(f"Pass criterion:  >= {required_passes}/{expected_attempts}")
    print(f"Timeout:         {args.timeout}s per call")
    print(f"Concurrency:     {args.concurrency}")
    print()
    print("Provider keys in environment:")
    for p, ok in check_env_keys().items():
        sigil = "✓" if ok else "✗"
        print(f"  {sigil} {p}")
    missing = [p for p, ok in check_env_keys().items() if not ok]
    if missing:
        print(f"  → Tests against {', '.join(missing)} models will likely fail with auth errors.")
    print()


def print_results(results: list[TestResult], required_passes: int) -> None:
    by_role: dict[str, list[TestResult]] = {}
    for r in results:
        by_role.setdefault(r.role, []).append(r)

    print()
    print("=" * 72)
    print("Results")
    print("=" * 72)

    for role in sorted(by_role):
        print(f"\n  {role}:")
        by_model: dict[str, list[TestResult]] = {}
        for r in by_role[role]:
            by_model.setdefault(r.model, []).append(r)
        for model in by_model:
            kind = by_model[model][0].model_kind
            print(f"    {model}  ({kind}):")
            for r in by_model[model]:
                ok = r.passes >= required_passes
                sigil = "✓" if ok else "✗"
                med = r.median_latency_sec
                latency_str = f"{med:.1f}s med" if med is not None else "no calls"
                print(f"      {sigil} {r.test_name:22s} {r.passes}/{r.attempts}  ({latency_str})")
                if not ok:
                    for f in r.failures[:3]:
                        print(f"          {f}")
                    if len(r.failures) > 3:
                        print(f"          ... ({len(r.failures) - 3} more)")


def print_summary(results: list[TestResult], required_passes: int, elapsed: float) -> bool:
    """Print summary + mitigation guidance. Returns True if gate is OPEN."""
    pass_combos = [r for r in results if r.passes >= required_passes]
    fail_combos = [r for r in results if r.passes < required_passes]

    print()
    print("=" * 72)
    print("Summary")
    print("=" * 72)
    print(f"Total combos:       {len(results)}")
    print(f"Passing:            {len(pass_combos)}")
    print(f"Failing:            {len(fail_combos)}")
    print(f"Elapsed:            {elapsed:.1f}s")

    if not fail_combos:
        print()
        print("✓ Phase A smoke test gate is OPEN.")
        return True

    print()
    print("✗ Phase A smoke test gate is BLOCKED. Failing combos:")
    for r in fail_combos:
        print(f"   - {r.model} × {r.test_name}: {r.passes}/{r.attempts}")
    print()
    print("Mitigation paths (cheap → expensive):")
    print("  1. Bump a passing fallback to primary in droid_whispering.yaml")
    print("  2. Drop the unreliable model from droid_whispering.yaml entirely")
    print("     (be careful: review_validator + adversarial_subagent require")
    print("      a non-anthropic AND non-Coder-provider option)")
    print("  3. Try a different LiteLLM version: pip install -U 'litellm>=1.55.0'")
    print("     or older: pip install 'litellm<1.50.0' (tool-calling bugs are often")
    print("     version-specific)")
    print("  4. Check provider status pages (e.g. status.openai.com,")
    print("     status.anthropic.com)")
    return False


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase A smoke test for MAF-Coder model routing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default="config/droid_whispering.yaml",
                        help="Path to droid_whispering.yaml")
    parser.add_argument("--attempts", type=int, default=None,
                        help="Override attempts per combo (default: parsed from pass_criterion)")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="Per-call timeout in seconds (default 30)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Max concurrent calls (default 4)")
    parser.add_argument("--roles",
                        help="Comma-separated subset of roles to test (default: all in smoke_test.targets)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making any API calls")
    parser.add_argument("--output", help="Write JSON results to this path")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        return 2

    config = yaml.safe_load(config_path.read_text())
    smoke = config.get("smoke_test", {})
    if not smoke.get("enabled", True):
        print("smoke_test.enabled is False in config — exiting cleanly.")
        return 0

    test_cases = [parse_test_case(tc) for tc in smoke["test_cases"]]
    required_passes, expected_attempts = parse_pass_criterion(
        smoke.get("pass_criterion", "5/5")
    )
    attempts = args.attempts or expected_attempts

    targets_yaml = smoke["targets"]
    if args.roles:
        wanted = {r.strip() for r in args.roles.split(",")}
        targets_yaml = [t for t in targets_yaml if t["role"] in wanted]
        if not targets_yaml:
            print(f"ERROR: no smoke_test.targets matched --roles {args.roles}", file=sys.stderr)
            return 2

    # Build the test plan
    plan: list[tuple[str, str, str, TestCase]] = []
    for target in targets_yaml:
        role = target["role"]
        if role not in config["roles"]:
            print(f"WARN: role {role!r} in smoke_test.targets but missing from roles section. Skipping.")
            continue
        role_cfg = config["roles"][role]
        models = [(role_cfg["primary"]["model"], "primary")]
        if not target.get("primary_only", False):
            models.extend([(fb["model"], "fallback") for fb in role_cfg.get("fallback", [])])
        for model, kind in models:
            for tc in test_cases:
                plan.append((role, model, kind, tc))

    print_header(args, config_path, attempts, required_passes, expected_attempts)
    print(f"Plan: {len(plan)} combinations")
    if args.dry_run:
        print()
        for role, model, kind, tc in plan:
            print(f"  - {role:25s} {model:40s} {kind:10s} {tc.name}")
        return 0
    print()

    try:
        import litellm  # noqa: F401
    except ImportError:
        print("ERROR: litellm not installed. Run: pip install 'litellm>=1.50.0'", file=sys.stderr)
        return 2

    sem = asyncio.Semaphore(args.concurrency)

    async def run_one(role: str, model: str, kind: str, tc: TestCase) -> TestResult:
        async with sem:
            print(f"  testing {role:25s} {model:40s} {tc.name}")
            return await run_one_combo(
                role, model, kind, tc,
                attempts=attempts, timeout_sec=args.timeout,
            )

    print("Running:")
    t0 = time.monotonic()
    results = await asyncio.gather(*[run_one(*p) for p in plan])
    elapsed = time.monotonic() - t0

    print_results(results, required_passes)
    gate_open = print_summary(results, required_passes, elapsed)

    if args.output:
        out = {
            "config": str(config_path),
            "elapsed_sec": elapsed,
            "attempts_per_combo": attempts,
            "pass_criterion": f"{required_passes}/{expected_attempts}",
            "env_available": check_env_keys(),
            "gate_open": gate_open,
            "results": [asdict(r) for r in results],
        }
        Path(args.output).write_text(json.dumps(out, indent=2, default=str))
        print(f"\nResults persisted to {args.output}")

    return 0 if gate_open else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
