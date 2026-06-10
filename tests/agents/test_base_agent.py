"""BaseAgent tests (AGENT_TOOLS_SPEC §2).

Strategy: subclass `BaseAgent` and override `_execute_sdk` with a synthetic
result so we don't need the actual OpenAI Agents SDK installed during unit
tests. This is the documented testing pattern.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from maf_coder.agents import AgentResult, BaseAgent, TaskContext
from maf_coder.agents.base import _RawResult
from maf_coder.blackboard import ArtifactStore, EventLog
from maf_coder.models.router import ModelRouter
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_router(tmp_path: Path) -> ModelRouter:
    """Build a ModelRouter from an in-memory config (no YAML file needed)."""
    cfg_path = tmp_path / "droid.yaml"
    cfg_path.write_text(
        "version: 1\n"
        "roles:\n"
        "  coder_worker:\n"
        "    primary:\n"
        "      model: anthropic/claude-test\n"
        "      temperature: 0.1\n"
        "      max_tokens: 4000\n"
        "    fallback: []\n"
        "  review_validator:\n"
        "    primary:\n"
        "      model: openai/gpt-test\n"
        "      temperature: 0.0\n"
        "      max_tokens: 4000\n"
        "    fallback: []\n"
        "  orchestrator:\n"
        "    primary:\n"
        "      model: anthropic/claude-test\n"
        "      temperature: 0.2\n"
        "      max_tokens: 8000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg_path)


@pytest.fixture
def prompt_file(tmp_path: Path) -> Path:
    p = tmp_path / "fake_prompt.md"
    p.write_text("You are a test agent. Do test things.")
    return p


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m-test-001")


@pytest.fixture
def event_log(store: ArtifactStore) -> EventLog:
    return store.event_log()


class _DummySandbox:
    """Minimal SandboxClient stand-in — BaseAgent only stores a reference."""

    async def health_check(self) -> bool:
        return True


def _make_task(
    *,
    task_id: str = "t1",
    timeout: int = 60,
) -> Task:
    return Task(
        task_id=task_id,
        parent_milestone="m1",
        owner=Role.CODER_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="test",
        background="test",
        acceptance_criteria=["f1.a1"],
        required_outputs=["handoff.md"],
        permission=Permission(network_policy=NetworkPolicy.NONE),
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=timeout),
    )


# ---------------------------------------------------------------------------
# Stub agent: synthetic SDK execution
# ---------------------------------------------------------------------------


class _StubAgent(BaseAgent[str]):
    role = Role.CODER_WORKER

    def __init__(self, *, prompt_path: Path, raw_output: str, **kw):  # type: ignore[no-untyped-def]
        self.prompt_path = prompt_path  # instance attr satisfies the class attr check
        self._raw_output = raw_output
        super().__init__(**kw)

    def build_tools(self, ctx: TaskContext) -> list:
        return []

    def build_first_user_message(self, ctx: TaskContext) -> str:
        return f"goal: {ctx.task.goal}"

    def parse_output(self, raw_output: str, ctx: TaskContext) -> str:
        return raw_output.strip()

    async def _execute_sdk(self, **kw) -> _RawResult:  # type: ignore[override]
        # Record this run by appending a fake tool invocation
        kw["ctx"].tools_invoked.append("stub_tool")
        return _RawResult(
            final_output=self._raw_output,
            tokens_in=12,
            tokens_out=34,
            cost_usd=0.0001,
            model_used=kw["model_id"],
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_loads_instructions_from_prompt(
        self, store: ArtifactStore, event_log: EventLog, stub_router: ModelRouter, prompt_file: Path
    ) -> None:
        agent = _StubAgent(
            prompt_path=prompt_file,
            raw_output="hi",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        assert "test agent" in agent._instructions

    def test_missing_prompt_raises(
        self, store: ArtifactStore, event_log: EventLog, stub_router: ModelRouter, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope.md"
        with pytest.raises(FileNotFoundError):
            _StubAgent(
                prompt_path=missing,
                raw_output="x",
                store=store,
                event_log=event_log,
                router=stub_router,
                sandbox=_DummySandbox(),
            )

    def test_missing_role_attr_raises(
        self, store: ArtifactStore, event_log: EventLog, stub_router: ModelRouter, prompt_file: Path
    ) -> None:
        class NoRole(BaseAgent[str]):
            prompt_path = prompt_file

            def build_tools(self, ctx):  # type: ignore[no-untyped-def]
                return []

            def build_first_user_message(self, ctx):  # type: ignore[no-untyped-def]
                return ""

            def parse_output(self, raw, ctx):  # type: ignore[no-untyped-def]
                return raw

        # role attribute missing
        with pytest.raises(TypeError):
            NoRole(store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox())


class TestRun:
    @pytest.mark.asyncio
    async def test_returns_agent_result_with_parsed_output(
        self, store: ArtifactStore, event_log: EventLog, stub_router: ModelRouter, prompt_file: Path
    ) -> None:
        agent = _StubAgent(
            prompt_path=prompt_file,
            raw_output="hello world\n",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        result = await agent.run(_make_task(), mission_id="m-test-001")
        assert isinstance(result, AgentResult)
        assert result.parsed_output == "hello world"
        assert result.role == Role.CODER_WORKER
        assert result.task_id == "t1"
        assert result.tokens_in == 12
        assert result.tokens_out == 34
        assert result.errored is False
        assert "stub_tool" in result.tools_invoked

    @pytest.mark.asyncio
    async def test_logs_llm_call_event(
        self, store: ArtifactStore, event_log: EventLog, stub_router: ModelRouter, prompt_file: Path
    ) -> None:
        agent = _StubAgent(
            prompt_path=prompt_file,
            raw_output="x",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        await agent.run(_make_task(), mission_id="m-test-001")
        kinds = [e.kind for e in event_log.iter_events()]
        assert "llm_call" in kinds

    @pytest.mark.asyncio
    async def test_timeout_returns_errored_result(
        self, store: ArtifactStore, event_log: EventLog, stub_router: ModelRouter, prompt_file: Path
    ) -> None:
        class SlowAgent(_StubAgent):
            async def _execute_sdk(self, **kw):  # type: ignore[override]
                await asyncio.sleep(10)
                return _RawResult(final_output="never")

        agent = SlowAgent(
            prompt_path=prompt_file,
            raw_output="x",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        # 1-second budget
        result = await agent.run(_make_task(task_id="slow", timeout=1), mission_id="m-test-001")
        assert result.errored is True
        assert "timeout" in (result.error_reason or "")

    @pytest.mark.asyncio
    async def test_validator_provider_constraint_applied(
        self,
        store: ArtifactStore,
        event_log: EventLog,
        stub_router: ModelRouter,
        prompt_file: Path,
    ) -> None:
        """When Coder used anthropic, ReviewValidator must NOT pick anthropic."""

        class ReviewAgent(_StubAgent):
            role = Role.REVIEW_VALIDATOR

        # Add forbidden_providers constraint to review_validator
        stub_router.config.roles["review_validator"].constraints = {"forbidden_providers": []}

        agent = ReviewAgent(
            prompt_path=prompt_file,
            raw_output="ok",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        # If coder_provider_in_use=anthropic and the only review model is openai,
        # the router must pick openai (which it does — no forbidden tweaking needed).
        task = _make_task()
        result = await agent.run(task, mission_id="m-test-001", coder_provider_in_use="anthropic")
        assert result.errored is False
        assert "openai" in result.model_used

    @pytest.mark.asyncio
    async def test_smart_router_hook_applies_tier_when_enabled(
        self,
        store: ArtifactStore,
        event_log: EventLog,
        prompt_file: Path,
        tmp_path: Path,
    ) -> None:
        """The single base.py hook must route the run through resolve_model: when
        smart_router is enabled for coder_worker and the (mocked) judge picks
        `reasoning`, the SDK call must receive the tier model, not the primary.
        """
        cfg_path = tmp_path / "sr_droid.yaml"
        cfg_path.write_text(
            "version: 1\n"
            "roles:\n"
            "  coder_worker:\n"
            "    primary: { model: anthropic/claude-primary, temperature: 0.1, max_tokens: 4000 }\n"
            "    fallback: []\n"
            "smart_router:\n"
            "  enabled: true\n"
            "  judge: { model: google/gemini-2.5-flash, temperature: 0.0, max_tokens: 256 }\n"
            "  default_tier: medium\n"
            "  tiers:\n"
            "    reasoning: { model: anthropic/claude-tier-reasoning, max_tokens: 32000 }\n"
            "  per_role:\n"
            "    coder_worker: { enabled: true }\n"
        )
        router = ModelRouter(cfg_path)

        async def _judge(_prompt: str) -> str:
            return "<tier>reasoning</tier>"

        # Inject the stub judge so no live API is hit.
        router.config.smart_router.judge = None  # force resolve_model to use override judge
        original = router.resolve_model

        async def _resolve(role, **kw):  # type: ignore[no-untyped-def]
            return await original(role, judge=_judge, **kw)

        router.resolve_model = _resolve  # type: ignore[method-assign]

        agent = _StubAgent(
            prompt_path=prompt_file,
            raw_output="done",
            store=store,
            event_log=event_log,
            router=router,
            sandbox=_DummySandbox(),
        )
        result = await agent.run(_make_task(), mission_id="m-test-001")
        assert result.errored is False
        # The hook routed through resolve_model → SDK saw the tier model.
        assert result.model_used == "anthropic/claude-tier-reasoning"

    @pytest.mark.asyncio
    async def test_unhandled_exception_in_sdk_yields_errored(
        self,
        store: ArtifactStore,
        event_log: EventLog,
        stub_router: ModelRouter,
        prompt_file: Path,
    ) -> None:
        class CrashAgent(_StubAgent):
            async def _execute_sdk(self, **kw):  # type: ignore[override]
                raise RuntimeError("kaboom")

        agent = CrashAgent(
            prompt_path=prompt_file,
            raw_output="x",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        result = await agent.run(_make_task(), mission_id="m-test-001")
        assert result.errored is True
        assert "kaboom" in (result.error_reason or "")

    @pytest.mark.asyncio
    async def test_parse_output_failure_marks_errored_but_still_returns(
        self,
        store: ArtifactStore,
        event_log: EventLog,
        stub_router: ModelRouter,
        prompt_file: Path,
    ) -> None:
        class BadParse(_StubAgent):
            def parse_output(self, raw_output, ctx):  # type: ignore[no-untyped-def]
                raise ValueError("bad json")

        agent = BadParse(
            prompt_path=prompt_file,
            raw_output="x",
            store=store,
            event_log=event_log,
            router=stub_router,
            sandbox=_DummySandbox(),
        )
        result = await agent.run(_make_task(), mission_id="m-test-001")
        assert result.errored is True
        assert "parse_failed" in (result.error_reason or "")
        assert result.raw_output == "x"  # raw output is preserved


# ---------------------------------------------------------------------------
# Cost-conscious model selection (soul.md §5.5)
# ---------------------------------------------------------------------------


class _ReviewStub(_StubAgent):
    role = Role.REVIEW_VALIDATOR


def _cc_router(tmp_path: Path) -> ModelRouter:
    """review_validator with a distinct fallback so we can tell which was used."""
    cfg = tmp_path / "cc_droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  review_validator:\n"
        "    primary: {model: openai/gpt-primary, temperature: 0.0, max_tokens: 1000}\n"
        "    fallback:\n"
        "      - {model: google/gemini-fallback, temperature: 0.0, max_tokens: 1000}\n"
    )
    return ModelRouter(cfg)


async def _review_model_used(tmp_path, store, event_log, prompt_file, *, budget_mode: str) -> str:
    from datetime import UTC, datetime

    from maf_coder.schemas import MissionState

    store.save_mission_state(
        MissionState(
            mission_id=store.mission_id,
            started_at=datetime.now(UTC),
            budget_mode=budget_mode,
        )
    )
    agent = _ReviewStub(
        prompt_path=prompt_file,
        raw_output="ok",
        store=store,
        event_log=event_log,
        router=_cc_router(tmp_path),
        sandbox=_DummySandbox(),
    )
    res = await agent.run(_make_task(), mission_id=store.mission_id)
    return res.model_used


@pytest.mark.asyncio
async def test_cost_conscious_validator_uses_fallback(
    tmp_path, store, event_log, prompt_file
) -> None:
    """soul.md §5.5: a validator uses its cheaper fallback model in cost_conscious
    mode; normal mode uses the primary."""
    assert (
        await _review_model_used(tmp_path, store, event_log, prompt_file, budget_mode="normal")
        == "openai/gpt-primary"
    )
    assert (
        await _review_model_used(
            tmp_path, store, event_log, prompt_file, budget_mode="cost_conscious"
        )
        == "google/gemini-fallback"
    )


@pytest.mark.asyncio
async def test_cost_conscious_non_validator_unaffected(
    tmp_path, store, event_log, prompt_file, stub_router
) -> None:
    """A non-validator (coder) keeps its primary even in cost_conscious mode."""
    from datetime import UTC, datetime

    from maf_coder.schemas import MissionState

    store.save_mission_state(
        MissionState(
            mission_id=store.mission_id,
            started_at=datetime.now(UTC),
            budget_mode="cost_conscious",
        )
    )
    agent = _StubAgent(  # role = CODER_WORKER
        prompt_path=prompt_file,
        raw_output="ok",
        store=store,
        event_log=event_log,
        router=stub_router,
        sandbox=_DummySandbox(),
    )
    res = await agent.run(_make_task(), mission_id=store.mission_id)
    assert res.model_used == "anthropic/claude-test"


# ---------------------------------------------------------------------------
# F3: budget guard must see cost for models LiteLLM can't price (mission path)
# ---------------------------------------------------------------------------


class _RealSDKAgent(BaseAgent[str]):
    """A BaseAgent that does NOT override _execute_sdk, so the real cost path runs."""

    role = Role.CODER_WORKER

    def __init__(self, *, prompt_path: Path, **kw):  # type: ignore[no-untyped-def]
        self.prompt_path = prompt_path
        super().__init__(**kw)

    def build_tools(self, ctx: TaskContext) -> list:
        return []

    def build_first_user_message(self, ctx: TaskContext) -> str:
        return "go"

    def parse_output(self, raw_output: str, ctx: TaskContext) -> str:
        return raw_output.strip()

    def _null_output(self) -> str:
        return ""


@pytest.mark.asyncio
async def test_execute_sdk_estimates_cost_for_unpriced_model(
    tmp_path, store, event_log, prompt_file, monkeypatch
) -> None:
    """A custom model the SDK can't price still yields a non-zero cost_usd, so the
    budget guard sees real spend on the mission path (F3)."""
    from types import SimpleNamespace

    import maf_coder.agents._sdk as sdk

    async def fake_run(agent, msg):  # type: ignore[no-untyped-def]
        # No cost_usd attribute → getattr(..., None) → token estimate kicks in.
        return SimpleNamespace(
            final_output="ok",
            usage=SimpleNamespace(input_tokens=600_000, output_tokens=400_000),
        )

    monkeypatch.setattr(sdk, "SDK_AVAILABLE", True)
    monkeypatch.setattr(sdk, "wrap_for_sdk", lambda t: t)
    monkeypatch.setattr(sdk, "Agent", lambda **kw: object())
    # LitellmModel must be present (it's now required); a stub is enough here —
    # this test exercises cost estimation, not the model wiring.
    monkeypatch.setattr(sdk, "LitellmModel", lambda *a, **k: object())
    monkeypatch.setattr(sdk, "ModelSettings", None)
    monkeypatch.setattr(sdk, "Runner", SimpleNamespace(run=fake_run))

    cfg = tmp_path / "custom.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  coder_worker:\n"
        "    primary: {model: mimo/custom-v1, temperature: 0.1, max_tokens: 1000}\n"
        "    fallback: []\n"
    )
    agent = _RealSDKAgent(
        prompt_path=prompt_file,
        store=store,
        event_log=event_log,
        router=ModelRouter(cfg),
        sandbox=_DummySandbox(),
    )
    res = await agent.run(_make_task(), mission_id=store.mission_id)
    # 1M tokens at the $1/Mtok default = $1.0 — crucially NOT $0.
    assert res.cost_usd == 1.0


# ---------------------------------------------------------------------------
# Custom per-model endpoints (MiMo / DeepSeek) — mission path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_sdk_passes_custom_endpoint_to_litellm(
    tmp_path, store, event_log, prompt_file, monkeypatch
) -> None:
    """A coder role configured with api_base + api_key_env routes the SDK's
    LitellmModel to that custom endpoint with the env-resolved key."""
    from types import SimpleNamespace

    import maf_coder.agents._sdk as sdk

    monkeypatch.setenv("MIMO_TEST_KEY", "sk-fake-mimo")
    captured: dict[str, object] = {}

    def fake_litellm_model(model, base_url=None, api_key=None, **kw):  # type: ignore[no-untyped-def]
        captured["model"] = model
        captured["base_url"] = base_url
        captured["api_key"] = api_key
        return object()

    async def fake_run(agent, msg):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            final_output="ok",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(sdk, "SDK_AVAILABLE", True)
    monkeypatch.setattr(sdk, "wrap_for_sdk", lambda t: t)
    monkeypatch.setattr(sdk, "Agent", lambda **kw: object())
    monkeypatch.setattr(sdk, "LitellmModel", fake_litellm_model)
    monkeypatch.setattr(sdk, "ModelSettings", None)
    monkeypatch.setattr(sdk, "Runner", SimpleNamespace(run=fake_run))

    cfg = tmp_path / "mimo.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  coder_worker:\n"
        "    primary:\n"
        "      model: anthropic/mimo-v2.5-pro\n"
        "      temperature: 0.2\n"
        "      max_tokens: 100\n"
        "      api_base: https://mimo.example/anthropic\n"
        "      api_key_env: MIMO_TEST_KEY\n"
        "    fallback: []\n"
    )
    agent = _RealSDKAgent(
        prompt_path=prompt_file,
        store=store,
        event_log=event_log,
        router=ModelRouter(cfg),
        sandbox=_DummySandbox(),
    )
    await agent.run(_make_task(), mission_id=store.mission_id)
    assert captured["model"] == "anthropic/mimo-v2.5-pro"
    assert captured["base_url"] == "https://mimo.example/anthropic"
    assert captured["api_key"] == "sk-fake-mimo"


class _ExtraForbidArg(BaseModel):
    """Mirrors the project convention: every model forbids extra fields, which
    emits additionalProperties:false — the schema feature that broke the SDK."""

    model_config = ConfigDict(extra="forbid")
    x: int


def _extra_forbid_tool(arg: _ExtraForbidArg, opt: _ExtraForbidArg | None = None) -> str:
    """Do a thing.

    Args:
        arg: the required argument.
        opt: an optional argument — the union/anyOf that triggered the crash.
    """
    return "ok"


def test_wrap_for_sdk_tolerates_extra_forbid_model_in_union() -> None:
    """Regression — caught by the first live shakedown.

    Orchestrator tools crashed in wrap_for_sdk with 'additionalProperties should
    not be set for object types': our tool params are Pydantic models with
    ConfigDict(extra="forbid") (a hard project convention), which emits
    additionalProperties:false, and the SDK's strict-schema enforcement rejects
    that inside a union/anyOf. wrap_for_sdk must wrap such a tool WITHOUT raising
    (it passes strict_mode=False). The unit suite never hit this because it
    monkeypatches wrap_for_sdk to a passthrough; this test uses the real one.

    The tool + model are module-level on purpose: the SDK resolves annotations
    via get_type_hints against module globals (this file uses
    `from __future__ import annotations`), exactly as the real tools do.
    """
    import maf_coder.agents._sdk as sdk

    if not sdk.SDK_AVAILABLE:
        pytest.skip("OpenAI Agents SDK not installed")

    # Before the strict_mode=False fix this raised agents.exceptions.UserError.
    wrapped = sdk.wrap_for_sdk(_extra_forbid_tool)
    assert wrapped is not None


def test_real_litellm_model_is_importable() -> None:
    """Regression — caught by the second live shakedown hop.

    Every role's model is a LiteLLM-style string (openai/…, deepseek/…,
    anthropic/…) that must be wrapped in the SDK's LitellmModel. The class moved
    to `agents.extensions.models.litellm_model`; _sdk previously looked only in
    `agents.models`, so _sdk.LitellmModel silently resolved to None — and
    _execute_sdk then built an agent with NO model, making the SDK fall back to
    its native OpenAI provider and crash with 'missing OPENAI_API_KEY'. Lock the
    real class in so the import path can't regress.
    """
    import maf_coder.agents._sdk as sdk

    if not sdk.SDK_AVAILABLE:
        pytest.skip("OpenAI Agents SDK not installed")

    assert sdk.LitellmModel is not None
    assert sdk.LitellmModel.__name__ == "LitellmModel"


@pytest.mark.asyncio
async def test_execute_sdk_fails_loud_when_litellm_model_missing(
    tmp_path, store, event_log, prompt_file, monkeypatch
) -> None:
    """If LitellmModel can't be imported, _execute_sdk must fail loud naming the
    litellm extra — not silently build a model-less agent that falls back to the
    SDK's native OpenAI provider (the misleading failure mode of the live run)."""
    import maf_coder.agents._sdk as sdk

    monkeypatch.setattr(sdk, "SDK_AVAILABLE", True)
    monkeypatch.setattr(sdk, "LitellmModel", None)
    monkeypatch.setattr(sdk, "wrap_for_sdk", lambda t: t)
    monkeypatch.setattr(sdk, "ModelSettings", None)

    cfg = tmp_path / "r.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  coder_worker:\n"
        "    primary:\n"
        "      model: openai/some-model\n"
        "      temperature: 0.2\n"
        "      max_tokens: 100\n"
        "    fallback: []\n"
    )
    agent = _RealSDKAgent(
        prompt_path=prompt_file,
        store=store,
        event_log=event_log,
        router=ModelRouter(cfg),
        sandbox=_DummySandbox(),
    )
    result = await agent.run(_make_task(), mission_id=store.mission_id)
    assert result.errored is True
    assert "litellm" in (result.error_reason or "").lower()
