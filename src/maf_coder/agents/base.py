"""BaseAgent — shared shell for every role agent (AGENT_TOOLS_SPEC §2).

Every role-specific agent (Orchestrator, CoderWorker, ReviewValidator, ...)
subclasses `BaseAgent`. The shell handles:

1. Constructing the per-task `TaskContext` that closes over store/sandbox/router
2. Resolving the right model via `ModelRouter` (with异-provider enforcement)
3. Instantiating the OpenAI Agents SDK Agent with tools + instructions + model
4. Running the SDK Runner with the per-task budget timeout
5. Parsing structured output and aggregating it into an `AgentResult`

What BaseAgent deliberately does NOT do:
- Decide which tools to register: subclass `build_tools` does
- Decide which model to use: `ModelRouter` does
- Enforce per-tool permissions: each tool calls `permissions.check_*`
- Retry on failure: that's `Scheduler.run`'s responsibility
- Persist artifacts: tools that write artifacts call `ctx.store` directly

Subclass contract:

    class MyAgent(BaseAgent[MyOutput]):
        role = Role.CODER_WORKER
        prompt_path = Path("prompts/coder_worker.md")

        def build_tools(self, ctx): ...
        def build_first_user_message(self, ctx): ...
        def parse_output(self, raw, ctx) -> MyOutput: ...

Testing without the SDK installed: override `_execute_sdk` in a subclass to
return a synthetic `_RawResult`. See `tests/agents/test_base_agent.py` for the
canonical stub pattern.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ..blackboard import ArtifactStore
from ..blackboard.event_log import EventLog
from ..models import ModelRouter
from ..schemas import Role, Task
from . import _sdk
from .errors import ToolError

if TYPE_CHECKING:
    from ..sandbox import SandboxClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskContext:
    """Per-task execution context. Closed over by tool factories.

    Constructed once per `BaseAgent.run` invocation. All tool closures share
    this object so they have a consistent view of the task, the artifact
    store, the sandbox, and the event log.
    """

    task: Task
    mission_id: str
    store: ArtifactStore
    event_log: EventLog
    router: ModelRouter
    sandbox: SandboxClient
    coder_provider_in_use: str | None = None
    # Mutable scratch-pad used by the agent shell to record observed
    # tool invocations during a single run. Tools append to this list AFTER
    # their permission check passes; BaseAgent.run reads it post-execution.
    tools_invoked: list[str] = field(default_factory=list, compare=False)


@dataclass(frozen=True)
class AgentResult(Generic[T]):
    """Outcome of one `BaseAgent.run` invocation."""

    role: Role
    task_id: str
    parsed_output: T
    raw_output: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_sec: float
    model_used: str
    fallback_used: bool
    tools_invoked: list[str]
    errored: bool = False
    error_reason: str | None = None


@dataclass(frozen=True)
class _RawResult:
    """Output of the SDK Runner — internal handoff to `parse_output`.

    Decoupling this from `AgentResult` lets tests fake the SDK while
    preserving the BaseAgent post-processing (timing, token aggregation).
    """

    final_output: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    model_used: str = ""
    fallback_used: bool = False


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class BaseAgent(ABC, Generic[T]):
    """Shared agent shell. All role agents subclass this.

    Subclasses MUST set the class attributes `role` and `prompt_path`, and
    implement `build_tools`, `build_first_user_message`, and `parse_output`.
    """

    role: Role
    prompt_path: Path

    def __init__(
        self,
        *,
        store: ArtifactStore,
        event_log: EventLog,
        router: ModelRouter,
        sandbox: SandboxClient,
    ) -> None:
        if not hasattr(self, "role"):
            raise TypeError(f"{type(self).__name__} must set class attribute `role`")
        if not hasattr(self, "prompt_path"):
            raise TypeError(f"{type(self).__name__} must set class attribute `prompt_path`")
        self.store = store
        self.event_log = event_log
        self.router = router
        self.sandbox = sandbox
        prompt_file = Path(self.prompt_path)
        if not prompt_file.is_absolute():
            prompt_file = self._resolve_prompt_path(prompt_file)
        if not prompt_file.exists():
            raise FileNotFoundError(
                f"{type(self).__name__}: prompt file not found at {prompt_file}"
            )
        self._instructions = prompt_file.read_text(encoding="utf-8")
        self._prompt_file = prompt_file

    @staticmethod
    def _resolve_prompt_path(rel: Path) -> Path:
        """Resolve `prompts/<role>.md` against likely candidates.

        Tries (in order):
          1. cwd / rel
          2. repo root sibling of `src/` (walks up from this file)
          3. rel as-is
        """
        cand1 = Path.cwd() / rel
        if cand1.exists():
            return cand1
        # walk up from this file looking for prompts/
        here = Path(__file__).resolve()
        for parent in here.parents:
            cand = parent / rel
            if cand.exists():
                return cand
        return rel

    # -- Subclass extension points ----------------------------------------

    @abstractmethod
    def build_tools(self, ctx: TaskContext) -> list[Any]:
        """Return the list of tool factories scoped to this task.

        Each tool MUST close over `ctx`. The list shape is what OpenAI Agents
        SDK expects (callables decorated with `@function_tool`).
        """

    @abstractmethod
    def build_first_user_message(self, ctx: TaskContext) -> str:
        """Construct the first user message handed to the SDK Agent."""

    @abstractmethod
    def parse_output(self, raw_output: str, ctx: TaskContext) -> T:
        """Extract the structured output from the raw final_output text."""

    # -- Lifecycle --------------------------------------------------------

    async def run(
        self,
        task: Task,
        *,
        mission_id: str,
        coder_provider_in_use: str | None = None,
    ) -> AgentResult[T]:
        """Execute the agent loop for one task.

        Honors `task.budget.max_runtime_sec` via `asyncio.wait_for`.
        Catches `ToolError` raised inside tools — the agent saw them as tool
        results — and any unexpected exception, returning an `errored=True`
        result rather than propagating (the Scheduler decides what to do).
        """
        ctx = TaskContext(
            task=task,
            mission_id=mission_id,
            store=self.store,
            event_log=self.event_log,
            router=self.router,
            sandbox=self.sandbox,
            coder_provider_in_use=coder_provider_in_use,
        )

        # Resolve model for this role + dynamic provider constraint.
        # Smart Router hook (SR-2): when smart_router is enabled for this role,
        # resolve_model classifies the task into a tier and applies the tier's
        # model over the primary — STILL passing forbidden_providers / validator
        # -≠-coder enforcement. When disabled (the default, and always for
        # review_validator) resolve_model returns get_primary_model unchanged, so
        # routing is identical. coder_provider_in_use flows from the Scheduler;
        # when None, forbidden_providers from config still applies.
        role_name = self.role.value if hasattr(self.role, "value") else self.role
        try:
            if hasattr(self.router, "resolve_model"):
                model_cfg = await self.router.resolve_model(
                    role_name,
                    task=task,
                    coder_provider_in_use=coder_provider_in_use,
                )
            else:
                model_cfg = self.router.get_primary_model(
                    role_name,
                    coder_provider_in_use=coder_provider_in_use,
                )
        except Exception as e:
            logger.error("Model resolution failed for role=%s: %r", self.role, e)
            return AgentResult(
                role=self.role,
                task_id=task.task_id,
                parsed_output=self._null_output(),
                raw_output="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_sec=0.0,
                model_used="",
                fallback_used=False,
                tools_invoked=[],
                errored=True,
                error_reason=f"model_resolution_failed: {e!r}",
            )

        tools = self.build_tools(ctx)
        first_msg = self.build_first_user_message(ctx)

        t0 = time.monotonic()
        try:
            raw = await asyncio.wait_for(
                self._execute_sdk(
                    instructions=self._instructions,
                    tools=tools,
                    first_user_message=first_msg,
                    model_id=model_cfg.model,
                    temperature=model_cfg.temperature,
                    max_tokens=model_cfg.max_tokens,
                    ctx=ctx,
                ),
                timeout=float(task.budget.max_runtime_sec),
            )
        except TimeoutError:
            duration = time.monotonic() - t0
            return AgentResult(
                role=self.role,
                task_id=task.task_id,
                parsed_output=self._null_output(),
                raw_output="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_sec=duration,
                model_used=model_cfg.model,
                fallback_used=False,
                tools_invoked=list(ctx.tools_invoked),
                errored=True,
                error_reason=f"timeout after {task.budget.max_runtime_sec}s",
            )
        except ToolError as e:
            duration = time.monotonic() - t0
            return AgentResult(
                role=self.role,
                task_id=task.task_id,
                parsed_output=self._null_output(),
                raw_output="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_sec=duration,
                model_used=model_cfg.model,
                fallback_used=False,
                tools_invoked=list(ctx.tools_invoked),
                errored=True,
                error_reason=f"{type(e).__name__}: {e}",
            )
        except Exception as e:
            duration = time.monotonic() - t0
            logger.exception("Agent run crashed for task=%s", task.task_id)
            return AgentResult(
                role=self.role,
                task_id=task.task_id,
                parsed_output=self._null_output(),
                raw_output="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_sec=duration,
                model_used=model_cfg.model,
                fallback_used=False,
                tools_invoked=list(ctx.tools_invoked),
                errored=True,
                error_reason=f"unhandled: {type(e).__name__}: {e}",
            )

        duration = time.monotonic() - t0

        try:
            parsed = self.parse_output(raw.final_output, ctx)
            parse_error: str | None = None
        except Exception as e:
            logger.exception("parse_output failed for task=%s", task.task_id)
            parsed = self._null_output()
            parse_error = f"parse_failed: {type(e).__name__}: {e}"

        # Emit LLM_CALL event for accounting (covers the common case where the
        # SDK doesn't already hook into our event log).
        try:
            self.event_log.log_llm_call(
                mission_id=mission_id,
                actor=self.role.value if hasattr(self.role, "value") else str(self.role),
                model=raw.model_used or model_cfg.model,
                tokens_in=raw.tokens_in,
                tokens_out=raw.tokens_out,
                cost_usd=raw.cost_usd,
                latency_sec=duration,
                task_id=task.task_id,
                fallback_used=raw.fallback_used,
            )
        except Exception:
            logger.exception("event_log.log_llm_call failed; continuing")

        return AgentResult(
            role=self.role,
            task_id=task.task_id,
            parsed_output=parsed,
            raw_output=raw.final_output,
            tokens_in=raw.tokens_in,
            tokens_out=raw.tokens_out,
            cost_usd=raw.cost_usd,
            latency_sec=duration,
            model_used=raw.model_used or model_cfg.model,
            fallback_used=raw.fallback_used,
            tools_invoked=list(ctx.tools_invoked),
            errored=parse_error is not None,
            error_reason=parse_error,
        )

    # -- SDK isolation point ----------------------------------------------

    async def _execute_sdk(
        self,
        *,
        instructions: str,
        tools: list[Any],
        first_user_message: str,
        model_id: str,
        temperature: float,
        max_tokens: int,
        ctx: TaskContext,
    ) -> _RawResult:
        """Invoke the OpenAI Agents SDK Runner.

        Isolated as its own method so tests can override it with a stub.
        Real SDK call:

            agent = Agent(name=..., instructions=..., tools=tools,
                          model=LitellmModel(model_id),
                          model_settings=ModelSettings(...))
            result = await Runner.run(agent, first_user_message)
            return _RawResult(final_output=result.final_output, ...)
        """
        if not _sdk.SDK_AVAILABLE:
            raise RuntimeError(
                "OpenAI Agents SDK not installed and `_execute_sdk` not overridden. "
                "Install `openai-agents` or stub this method in tests."
            )

        wrapped_tools = [_sdk.wrap_for_sdk(t) for t in tools]
        agent_kwargs: dict[str, Any] = {
            "name": self.role.value if hasattr(self.role, "value") else str(self.role),
            "instructions": instructions,
            "tools": wrapped_tools,
        }
        if _sdk.LitellmModel is not None:
            agent_kwargs["model"] = _sdk.LitellmModel(model_id)
        if _sdk.ModelSettings is not None:
            agent_kwargs["model_settings"] = _sdk.ModelSettings(
                temperature=temperature, max_tokens=max_tokens
            )

        sdk_agent = _sdk.Agent(**agent_kwargs)
        sdk_result = await _sdk.Runner.run(sdk_agent, first_user_message)
        final_output: str = getattr(sdk_result, "final_output", "") or ""
        usage = getattr(sdk_result, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        cost = float(getattr(sdk_result, "cost_usd", 0.0) or 0.0)
        return _RawResult(
            final_output=final_output,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            model_used=model_id,
            fallback_used=False,
        )

    # -- Subclass override hook for "what to return on failure" -----------

    def _null_output(self) -> T:
        """Return a placeholder output when run fails.

        Default behavior: a string. Subclasses with structured outputs (Handoff,
        ReviewVerdict) should override this to return a typed sentinel.
        """
        return ""  # type: ignore[return-value]


__all__ = ["AgentResult", "BaseAgent", "TaskContext"]
