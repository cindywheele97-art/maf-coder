#!/usr/bin/env python3
"""Live smoke test — exercise the AGENT stack end to end with one cheap real call.

Why this exists (and how it differs from `scripts/smoke_test.py`):
    `smoke_test.py` validates the *model layer* — every configured model through
    LiteLLM (completion / tool-calling / JSON). This script validates the *agent
    layer* on top of it: `BaseAgent.run()` → router model resolution →
    `_execute_sdk` (OpenAI Agents SDK `Runner` + `LitellmModel`) → `parse_output`
    → `AgentResult` + EventLog. It answers "is the agent machinery actually wired
    to a real model, with my keys?" before you spend money on a full mission.

    It runs a minimal in-script agent with NO tools and a trivial instruction, so
    a failure points at the wiring, not at a role's prompt/tools/contract.

Usage:
    python scripts/live_smoke.py                      # research_worker's model, one call
    python scripts/live_smoke.py --role coder_worker
    python scripts/live_smoke.py --keys-only          # just check env keys, no call
    python scripts/live_smoke.py --max-tokens 8 --config config/droid_whispering.yaml

Needs the relevant provider keys in the environment (ANTHROPIC_API_KEY,
OPENAI_API_KEY, GEMINI_API_KEY). Reads config/droid_whispering.yaml by default.
Exit code 0 iff the attempted checks pass.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

from maf_coder.agents._sdk import SDK_AVAILABLE
from maf_coder.agents.base import BaseAgent, TaskContext
from maf_coder.blackboard import ArtifactStore
from maf_coder.cli import _default_router_config
from maf_coder.models import ModelRouter
from maf_coder.models.router import _provider_of
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    Role,
    Task,
    TaskBudget,
)

# provider prefix (LiteLLM-style) -> the env var that carries its key.
_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_SMOKE_TOKEN = "SMOKE-OK"


class SmokeAgent(BaseAgent[str]):
    """Minimal agent: no tools, a trivial prompt. `role`/`prompt_path` are set on
    the class at runtime before construction (see `build_smoke_agent`)."""

    def build_tools(self, ctx: TaskContext) -> list[Any]:
        return []

    def build_first_user_message(self, ctx: TaskContext) -> str:
        return f"Reply with exactly: {_SMOKE_TOKEN}"

    def parse_output(self, raw_output: str, ctx: TaskContext) -> str:
        return raw_output.strip()

    def _null_output(self) -> str:
        return ""


def provider_env_var(provider: str) -> str | None:
    """Env var that carries the key for a LiteLLM provider prefix, or None."""
    return _PROVIDER_ENV.get(provider)


def providers_for_role(router: ModelRouter, role: str) -> list[str]:
    """Distinct providers across a role's primary + fallback chain (config order)."""
    cfg = router.get_role_config(role)
    seen: list[str] = []
    for entry in (cfg.primary, *cfg.fallback):
        p = _provider_of(entry.model)
        if p not in seen:
            seen.append(p)
    return seen


def build_smoke_agent(
    *, role: str, router: ModelRouter, store: ArtifactStore, prompt_path: Path
) -> SmokeAgent:
    """Construct the minimal smoke agent for `role` (sets the class attrs)."""
    SmokeAgent.role = Role(role)
    SmokeAgent.prompt_path = prompt_path
    return SmokeAgent(
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=LocalShellSandbox(),
    )


def _smoke_task(role: str) -> Task:
    return Task(
        task_id="live-smoke",
        parent_milestone="m0",
        owner=Role(role),
        goal="live smoke: confirm the agent stack reaches a real model",
        background="Minimal no-tool round-trip to validate keys + routing + SDK.",
        acceptance_criteria=[],
        required_outputs=[],
        permission=Permission(
            allowed_paths=[], allowed_tools=[], network_policy=NetworkPolicy.NONE
        ),
        budget=TaskBudget(max_tokens=2000, max_runtime_sec=120),
    )


def _key_report(providers: list[str]) -> tuple[bool, list[str]]:
    """(all_present, lines). A missing key for a used provider is a hard fail."""
    ok = True
    lines: list[str] = []
    import os

    for p in providers:
        env = provider_env_var(p)
        if env is None:
            lines.append(f"  ?  provider {p!r}: unknown — can't map to an env var")
            continue
        present = bool(os.environ.get(env))
        lines.append(f"  {'✓' if present else '✗'}  {p:<10} → {env}{'' if present else '  (MISSING)'}")
        ok = ok and present
    return ok, lines


async def _run_smoke(*, role: str, config: Path, max_tokens: int) -> int:
    router = ModelRouter(config)
    primary = router.get_role_config(role).primary
    coder_provider = router.provider_for_role("coder_worker")
    print(f"config:          {config}")
    print(f"role:            {role}")
    print(f"resolved model:  {primary.model}  (the call uses this — mind its cost tier)")
    print(f"coder provider:  {coder_provider}  (异-provider rule active)")

    providers = providers_for_role(router, role)
    keys_ok, lines = _key_report(providers)
    print("API keys for this role's providers:")
    print("\n".join(lines))
    if not keys_ok:
        print("\nFAIL: a required provider key is missing. Export it and retry.")
        return 1

    if not SDK_AVAILABLE:
        print(
            "\nFAIL: OpenAI Agents SDK not importable — install it "
            "(the agent path can't run without it). `smoke_test.py` still works."
        )
        return 1

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prompt = tmp / "smoke_prompt.md"
        prompt.write_text(
            "You are a connectivity smoke test. Do nothing but reply with the "
            f"exact token you are asked for ({_SMOKE_TOKEN}). No tools, no extra words.",
            encoding="utf-8",
        )
        store = ArtifactStore(tmp / "missions", "live-smoke")
        agent = build_smoke_agent(role=role, router=router, store=store, prompt_path=prompt)
        # Trim the per-call ceiling so the smoke stays cheap.
        task = _smoke_task(role)
        task = task.model_copy(update={"budget": task.budget.model_copy(update={"max_tokens": max_tokens})})

        print(f"\ncalling {primary.model} (max_tokens={max_tokens}) …")
        result = await agent.run(task, mission_id="live-smoke", coder_provider_in_use=coder_provider)

    if result.errored:
        print(f"\nFAIL: agent.run errored → {result.error_reason}")
        return 1
    snippet = (result.raw_output or "").strip().replace("\n", " ")[:120]
    print("\nPASS ✅  the agent stack reached a real model.")
    print(f"  model_used:  {result.model_used}  (fallback={result.fallback_used})")
    print(f"  tokens:      in={result.tokens_in} out={result.tokens_out}")
    print(f"  cost_usd:    {result.cost_usd:.5f}")
    print(f"  latency_sec: {result.latency_sec:.2f}")
    print(f"  reply:       {snippet!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent-stack live smoke test.")
    parser.add_argument("--config", type=Path, default=None, help="droid_whispering.yaml (default: auto-locate).")
    parser.add_argument("--role", default="research_worker", help="Role whose model to call.")
    parser.add_argument("--max-tokens", type=int, default=16, help="Cap the reply (keep it cheap).")
    parser.add_argument("--keys-only", action="store_true", help="Only check env keys; make no call.")
    args = parser.parse_args(argv)

    try:
        config = args.config or _default_router_config()
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    if args.keys_only:
        router = ModelRouter(config)
        providers = providers_for_role(router, args.role)
        ok, lines = _key_report(providers)
        print(f"role {args.role!r} providers:")
        print("\n".join(lines))
        return 0 if ok else 1

    try:
        return asyncio.run(_run_smoke(role=args.role, config=config, max_tokens=args.max_tokens))
    except KeyboardInterrupt:  # pragma: no cover
        print("\ninterrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
