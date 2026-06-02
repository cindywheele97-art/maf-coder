"""Tests for scripts/live_smoke.py.

The script lives outside the package, so we load it by path. The real agent.run
path is stubbed (override `_execute_sdk`) so these tests need no network/SDK —
they verify the script's wiring and helpers, not a live model.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from maf_coder.agents.base import _RawResult

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_smoke.py"
_CONFIG = Path(__file__).resolve().parents[1] / "config" / "droid_whispering.yaml"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("live_smoke", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


live_smoke = _load()


def test_provider_env_var_mapping() -> None:
    assert live_smoke.provider_env_var("anthropic") == "ANTHROPIC_API_KEY"
    assert live_smoke.provider_env_var("openai") == "OPENAI_API_KEY"
    assert live_smoke.provider_env_var("google") == "GEMINI_API_KEY"
    assert live_smoke.provider_env_var("nope") is None


def test_providers_for_role_reads_chain() -> None:
    from maf_coder.models import ModelRouter

    router = ModelRouter(_CONFIG)
    providers = live_smoke.providers_for_role(router, "review_validator")
    assert "openai" in providers  # primary gpt-5
    assert all(isinstance(p, str) for p in providers)


def test_keys_only_main_passes_with_all_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(env, "x")
    rc = live_smoke.main(["--keys-only", "--role", "coder_worker", "--config", str(_CONFIG)])
    assert rc == 0


def test_keys_only_main_fails_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    rc = live_smoke.main(["--keys-only", "--role", "coder_worker", "--config", str(_CONFIG)])
    assert rc == 1


@pytest.mark.asyncio
async def test_stubbed_agent_run_round_trips(tmp_path: Path) -> None:
    """The script's SmokeAgent, with a stubbed SDK, returns the model's reply —
    proving BaseAgent.run → parse_output wiring without a live call."""
    from maf_coder.blackboard import ArtifactStore
    from maf_coder.models import ModelRouter
    from maf_coder.sandbox import LocalShellSandbox
    from maf_coder.schemas import Role

    class _StubSmoke(live_smoke.SmokeAgent):  # type: ignore[name-defined,misc]
        async def _execute_sdk(self, **kw: Any) -> _RawResult:  # type: ignore[override]
            return _RawResult(final_output=f"{live_smoke._SMOKE_TOKEN}\n", model_used="stub/x")

    prompt = tmp_path / "p.md"
    prompt.write_text("smoke", encoding="utf-8")
    _StubSmoke.role = Role.RESEARCH_WORKER
    _StubSmoke.prompt_path = prompt

    store = ArtifactStore(tmp_path / "missions", "live-smoke")
    agent = _StubSmoke(
        store=store, event_log=store.event_log(),
        router=ModelRouter(_CONFIG), sandbox=LocalShellSandbox(),
    )
    result = await agent.run(
        live_smoke._smoke_task("research_worker"),
        mission_id="live-smoke",
        coder_provider_in_use="anthropic",
    )
    assert result.errored is False
    assert result.parsed_output == live_smoke._SMOKE_TOKEN
