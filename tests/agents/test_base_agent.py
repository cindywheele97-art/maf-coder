"""BaseAgent tests (AGENT_TOOLS_SPEC §2).

Strategy: subclass `BaseAgent` and override `_execute_sdk` with a synthetic
result so we don't need the actual OpenAI Agents SDK installed during unit
tests. This is the documented testing pattern.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maf_coder.agents import AgentResult, BaseAgent, TaskContext
from maf_coder.agents.base import _RawResult
from maf_coder.blackboard import ArtifactStore, EventLog
from maf_coder.models.router import ModelConfig, ModelRouter, RoleConfig, RouterConfig
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
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
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
                store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
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
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
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
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
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
            prompt_path=prompt_file, raw_output="x",
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
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
        stub_router.config.roles["review_validator"].constraints = {
            "forbidden_providers": []
        }

        agent = ReviewAgent(
            prompt_path=prompt_file, raw_output="ok",
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
        )
        # If coder_provider_in_use=anthropic and the only review model is openai,
        # the router must pick openai (which it does — no forbidden tweaking needed).
        task = _make_task()
        result = await agent.run(task, mission_id="m-test-001", coder_provider_in_use="anthropic")
        assert result.errored is False
        assert "openai" in result.model_used

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
            prompt_path=prompt_file, raw_output="x",
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
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
            prompt_path=prompt_file, raw_output="x",
            store=store, event_log=event_log, router=stub_router, sandbox=_DummySandbox(),
        )
        result = await agent.run(_make_task(), mission_id="m-test-001")
        assert result.errored is True
        assert "parse_failed" in (result.error_reason or "")
        assert result.raw_output == "x"  # raw output is preserved
