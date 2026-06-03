# AGENT_TOOLS_SPEC.md

> Formal interface specifications for MAF-Coder agents, tools, and execution machinery.
>
> Companion to:
> - `ARCHITECTURE.md` — system shape and design decisions (the "what")
> - `agent_team_soul_v3.1.md` — organizational rules (the "why")
> - `prompts/*.md` — agent behavior contracts (the "how each agent thinks")
> - `WORKED_EXAMPLE.md` — concrete walkthrough with sample artifacts (the "looks like this")
>
> This document defines Python signatures, JSON schemas, return types, error contracts, and integration patterns. It is **normative** for v1 — implementations must conform.
>
> All code blocks use Python 3.11+ type-hint syntax. `from __future__ import annotations` is assumed at file level.

---

## 0. How to read this document

| Reader | Suggested reading order |
|---|---|
| Cursor implementing Phase B | §1 → §2 → §3 → §4 → §5 → §6 → §7 → §8 → §13 → §14 |
| Cursor extending to Phase C–E | Read above + §9 / §10 / §11 / §12 |
| Auditor checking permission boundaries | §5 → §6 → §7 → §8 |
| Adding a new tool | §3 (the factory pattern) → mimic an existing tool spec |

### Conformance levels

When a section says **MUST**, the implementation has no flexibility — change the spec instead.
When it says **SHOULD**, the default is clear but implementations may deviate with a comment justifying it.
When it says **MAY**, it's optional / a hint.

### Out of scope for this doc

- Pydantic schema field-level definitions — those are in `src/maf_coder/schemas/` and have docstrings
- Per-prompt content — those are in `prompts/*.md`
- Build / test / deployment — those are in the Build Plan
- Mission lifecycle prose — that's ARCHITECTURE.md §4

---

## 1. Overview: how tools fit into the system

### The four-layer call stack

When an agent emits a tool call, the call traverses four layers before something happens in the world:

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: Agent emits tool_call (via LLM)                    │
│   {"name": "write_file", "args": {"path": "src/foo.rs",    │
│                                    "content": "..."}}       │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: OpenAI Agents SDK dispatches to registered tool    │
│   - Validates args against JSON schema                      │
│   - Calls the Python tool function                          │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: Tool function checks permissions + dispatches      │
│   - Reads TaskContext (closure)                             │
│   - Checks task.permission.allowed_paths, allowed_tools     │
│   - Raises PermissionDeniedError if denied                  │
│   - Otherwise calls the underlying executor                 │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ Layer 4: Executor performs the actual operation             │
│   - SandboxClient: docker exec for sandbox tools            │
│   - ArtifactStore: filesystem for blackboard tools          │
│   - HTTP client + Sanitizer: for external fetch tools       │
└─────────────────────────────────────────────────────────────┘
```

**Layer 3 is where the security boundary lives.** Layers 1 and 2 are inside the LLM trust zone (anything from the LLM is suspect). Layer 4 is the system surface that actually causes effects. Layer 3 is the choke point between them.

**No tool function MAY skip Layer 3.** Even if the tool is "obviously safe" (like `read_file`), the permission check runs. This is what makes the system survive prompt injection — see soul.md §13 and ARCHITECTURE.md §3.5.

### Tool naming convention

Tool function names use `snake_case`. Names must be:
- Verb-led: `read_file`, `write_file`, `dispatch_task` (not `file_reader`, `task_dispatcher`)
- Action-specific: `cargo_test` (not `test`, not `run_tests` — explicit about which underlying tool)
- Boundary-tagged when ambiguous: `git_diff_in_sandbox` if there's also a host-side `git_diff` (rare; usually sandbox is implied)

### Signature conventions (spec shorthand vs. code)

Two stylistic differences between the signatures below and the implementation:

- **Optional list/dict params**: signatures here sometimes show a mutable default
  (`paths: list[str] = ["."]`, `args: list[str] = []`). The code avoids mutable
  defaults — it uses `param: list[X] | None = None` and substitutes the default
  inside the function. Read `= []` / `= [...]` as "optional, defaults to that."
- **Concrete generics**: bare `dict` / `list[dict]` here are `dict[str, Any]` /
  `list[dict[str, Any]]` in the typed code.

---

## 2. The BaseAgent class

Every role-specific agent (Orchestrator, ResearchWorker, CoderWorker, etc.) extends `BaseAgent`.

### Class signature

```python
# src/maf_coder/agents/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

from openai_agents import Agent as SDKAgent, Runner
from openai_agents.models import LitellmModel

from ..blackboard import ArtifactStore, EventLog
from ..models import ModelRouter
from ..schemas import Role, Task

T = TypeVar("T")  # The role-specific output type (Handoff, ReviewVerdict, ...)


@dataclass(frozen=True)
class AgentResult(Generic[T]):
    """Outcome of one BaseAgent.run invocation."""
    role: Role
    task_id: str
    parsed_output: T              # role-specific structured output
    raw_output: str               # the raw final_output text from the SDK
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_sec: float
    model_used: str
    fallback_used: bool
    tools_invoked: list[str]      # ordered list of tool function names called
    errored: bool = False
    error_reason: str | None = None


@dataclass(frozen=True)
class TaskContext:
    """Per-task execution context. Closed over by tool factories.

    This object is the link between the SDK-level tool call and the
    underlying state (mission, sandbox, blackboard, router). It is constructed
    once per BaseAgent.run invocation; its fields are immutable except the
    `tools_invoked` accumulator, to which each tool appends its name.
    """
    task: Task
    mission_id: str
    store: ArtifactStore
    event_log: EventLog
    router: ModelRouter
    sandbox: "SandboxClient"      # see §15
    coder_provider_in_use: str | None = None  # for异-provider enforcement
    tools_invoked: list[str] = field(default_factory=list, compare=False)  # appended by record_tool_call


class BaseAgent(ABC, Generic[T]):
    """Shared agent shell. All role agents subclass this.

    Subclasses MUST provide:
      - role: the Role enum value
      - prompt_path: the prompts/<role>.md file
      - parse_output(raw: str) -> T: extract structured output from final_output
    """

    role: Role                    # class attribute, set by subclass
    prompt_path: Path             # class attribute, set by subclass

    def __init__(
        self,
        *,
        store: ArtifactStore,
        event_log: EventLog,
        router: ModelRouter,
        sandbox: "SandboxClient",
    ) -> None:
        self.store = store
        self.event_log = event_log
        self.router = router
        self.sandbox = sandbox
        self._instructions = self.prompt_path.read_text(encoding="utf-8")

    @abstractmethod
    def build_tools(self, ctx: TaskContext) -> list:
        """Return the list of tool factories scoped to this task.
        
        The list shape is what OpenAI Agents SDK expects (list of @function_tool
        decorated callables). Each tool MUST close over `ctx`.
        """
        ...

    @abstractmethod
    def build_first_user_message(self, ctx: TaskContext) -> str:
        """Construct the first user message handed to the SDK Agent.
        
        Typically includes: task goal, contract refs, profile, research refs.
        See WORKED_EXAMPLE.md for canonical layouts per role.
        """
        ...

    @abstractmethod
    def parse_output(self, raw_output: str, ctx: TaskContext) -> T:
        """Extract the structured output from final_output text.

        For roles that emit JSON (Validators), parses + validates against
        Pydantic schema. For roles that emit markdown (Orchestrator planner),
        may just wrap the text.
        """
        ...

    async def run(
        self,
        task: Task,
        *,
        mission_id: str,
        coder_provider_in_use: str | None = None,
    ) -> AgentResult[T]:
        """Execute the agent loop for one task.

        Wraps:
          - Context construction
          - Model selection via router
          - SDK Agent instantiation with tools + instructions + model
          - Runner.run() execution
          - Output parsing
          - Result aggregation + event logging

        MUST honor task.budget.max_runtime_sec via asyncio.wait_for.
        MUST honor task.budget.max_tokens via model config.
        """
        # Implementation outline:
        # 1. Build ctx
        # 2. router.get_primary_model(self.role, coder_provider_in_use=...)
        # 3. tools = self.build_tools(ctx)
        # 4. sdk_agent = SDKAgent(name=self.role.value,
        #                         instructions=self._instructions,
        #                         tools=tools,
        #                         model=LitellmModel(model_cfg.model),
        #                         model_settings=ModelSettings(
        #                             temperature=model_cfg.temperature,
        #                             max_tokens=model_cfg.max_tokens))
        # 5. first_msg = self.build_first_user_message(ctx)
        # 6. result = await asyncio.wait_for(
        #        Runner.run(sdk_agent, first_msg),
        #        timeout=task.budget.max_runtime_sec)
        # 7. parsed = self.parse_output(result.final_output, ctx)
        # 8. Return AgentResult(...)
        ...
```

### Subclass minimal pattern

```python
# src/maf_coder/agents/orchestrator.py
class OrchestratorAgent(BaseAgent[OrchestratorOutput]):
    role = Role.ORCHESTRATOR
    prompt_path = Path("prompts/orchestrator.md")

    def build_tools(self, ctx: TaskContext):
        return [
            make_dispatch_task(ctx),
            make_read_artifact(ctx),
            make_save_artifact(ctx),
            make_emit_event(ctx),
            make_escalate_to_human_gate(ctx),
            make_create_checkpoint(ctx),
            make_poll_user_messages(ctx),
            make_get_mission_state(ctx),
            make_update_mission_state(ctx),
            make_get_budget_status(ctx),
        ]

    def build_first_user_message(self, ctx: TaskContext) -> str:
        # ... see WORKED_EXAMPLE.md for canonical layout
        ...

    def parse_output(self, raw_output: str, ctx: TaskContext) -> OrchestratorOutput:
        # Orchestrator output is freeform markdown; wrap in OrchestratorOutput
        return OrchestratorOutput(text=raw_output)
```

### What BaseAgent does NOT do

- Does NOT decide which model to use — that's `ModelRouter`
- Does NOT decide which tools to register — that's the subclass `build_tools`
- Does NOT enforce permissions inside tools — that's each tool's responsibility (§5)
- Does NOT persist anything — tools that need to persist call `ctx.store` / `ctx.event_log`
- Does NOT retry on failure — that's the Scheduler's job (§13)
- Does NOT decide what "task complete" looks like — that's the parse_output method per role

---

## 3. TaskContext and tool factories

### The factory pattern

Every tool function lives inside a *factory* function. The factory takes `ctx: TaskContext` and returns the actual tool function. The tool function closes over `ctx`.

```python
def make_<tool_name>(ctx: TaskContext) -> Tool:
    @function_tool  # from openai_agents
    async def <tool_name>(arg1: T1, arg2: T2 = default) -> R:
        """Docstring becomes the tool description seen by the LLM."""
        # Step 1: permission check (always)
        # Step 2: dispatch to executor
        # Step 3: log event
        # Step 4: return structured result
        ...
    return <tool_name>
```

This pattern means:
- Each `BaseAgent.run` invocation gets a fresh set of tool closures bound to the current task's context
- Tools never need parameters for "which task am I in" — they read it from `ctx`
- Tests can construct a synthetic `ctx` (with fake store/sandbox) and exercise tools in isolation

### Common error types for tools

All tool function exceptions MUST be one of:

```python
# src/maf_coder/agents/errors.py
class ToolError(Exception):
    """Base for all tool-level errors."""

class PermissionDeniedError(ToolError):
    """Tool call denied by permission check.
    
    Raised when the agent tries to access a path/tool/domain outside the
    task's permission boundary. The agent sees this as a tool result and
    typically retries with a different approach or escalates via handoff.
    """
    def __init__(self, what: str, why: str):
        self.what = what
        self.why = why
        super().__init__(f"{what}: {why}")


class SandboxError(ToolError):
    """Error executing a command in the sandbox container.
    
    Wraps non-zero exit codes ONLY when the contract is "this command must
    succeed" (e.g. failure to start a long-running probe). Routine command
    failures like `cargo test` returning non-zero are NOT exceptions — they
    are returned as CommandResult with exit_code != 0.
    """


class ArtifactError(ToolError):
    """Error reading/writing an artifact.
    
    Common: ArtifactNotFoundError, ContractAlreadyLockedError (re-raised from
    ArtifactStore), PathEscapeError.
    """


class ExternalContentError(ToolError):
    """Error fetching external content, or sanitizer rejection."""


class BudgetExceededError(ToolError):
    """Tool call denied because task budget is exhausted."""
```

The SDK sees raised exceptions as tool failures and feeds the error string back to the agent. Agents are expected to interpret these gracefully (`PermissionDeniedError` → "I cannot do that; try a different approach"). Critical failures should cause the agent to emit a handoff documenting the issue rather than crash.

---

## 4. Result types

Common return types used across many tools.

```python
# src/maf_coder/agents/results.py
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a sandbox command execution (cargo test, git diff, run_bash, etc).
    
    Used by every sandbox tool that wraps a shell command. Non-zero exit_code
    is NOT an exception — it's returned to the agent who decides how to react.
    """
    command: str
    exit_code: int
    stdout: str             # may be truncated; see truncated_stdout flag
    stderr: str             # may be truncated
    duration_sec: float
    truncated_stdout: bool = False
    truncated_stderr: bool = False


@dataclass(frozen=True)
class FileContent:
    """Outcome of a file read."""
    path: str               # relative to sandbox worktree
    content: str
    size_bytes: int
    truncated: bool = False  # True if file exceeded read limit (default 1MB)


@dataclass(frozen=True)
class GrepMatch:
    """One match from a grep tool call."""
    path: str
    line_number: int
    line: str
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SanitizedContent:
    """Outcome of an external HTTP fetch with sanitizer applied.
    
    `original_url` is preserved for citation. `sanitization_actions` records
    what the sanitizer modified (e.g. ["stripped 3 <script> blocks",
    "removed prompt-injection markers"]).
    """
    original_url: str
    final_url: str          # may differ if redirected
    content: str
    content_type: str
    sanitization_actions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskHandle:
    """Handle returned by dispatch_task to identify a running task.
    
    Used to await completion or check status. NOT a process / thread handle —
    just an opaque ID + creation timestamp.
    """
    task_id: str
    dispatched_at: float    # monotonic time


# Common Literal types for status fields
TaskStatus = Literal["pending", "ready", "active", "complete", "failed", "blocked"]
```

---

## 5. Permission enforcement layer

The choke point between LLM trust zone and system effects. Implemented as a single module:

```python
# src/maf_coder/agents/permissions.py
from pathlib import PurePosixPath

from ..schemas import Permission, NetworkPolicy
from .errors import PermissionDeniedError


def check_path_access(
    permission: Permission,
    path: str,
    mode: Literal["read", "write"],
) -> None:
    """Raise PermissionDeniedError if `path` is not allowed under `permission`.

    Rules:
      1. Path MUST be relative (no leading /), or under one of allowed_paths.
      2. If path is absolute, it MUST be a prefix-match of an allowed_paths entry.
      3. For mode="write", the path MUST be under allowed_paths
         AND NOT under any explicit deny list (see deny_globs in future).
      4. PathEscapeError-style traversal (../) is rejected.
    """


def check_tool_allowed(permission: Permission, tool_name: str) -> None:
    """Raise PermissionDeniedError if `tool_name` is not in permission.allowed_tools.
    
    Wildcards: 'cargo_*' in allowed_tools matches 'cargo_test', 'cargo_check', etc.
    """


def check_network_allowed(
    permission: Permission,
    url: str,
    domain_whitelist: list[str] | None = None,
) -> None:
    """Raise PermissionDeniedError if outbound HTTP not allowed for this task.
    
    NetworkPolicy values:
      - NONE: deny everything (raise)
      - CRATES_ONLY: allow crates.io, docs.rs, github.com only
      - WHITELIST: allow only domains in domain_whitelist
      - OPEN: allow everything (still subject to global denylist)
    """


def check_command_pattern(
    permission: Permission,
    command: str,
) -> None:
    """Raise PermissionDeniedError if command matches a global denylist.
    
    Denylist (hardcoded, NOT overridable per-task — these are always rejected):
      - git push (Coder/Worker may not push)
      - cargo publish / npm publish / etc
      - rm -rf / curl pipe sh / wget pipe sh
      - sudo
      - any command containing $(curl ...) or `wget ...`
    """
```

**Every tool function calls one or more of these checks before doing work.** No exceptions. If a tool is "trivially safe" and doesn't need a check, that's a code smell — every external surface needs one.

---

## 6. Orchestrator tools (Phase B)

The Orchestrator does NOT run code in the sandbox. All its tools are in-process or wrap reads of the sandbox's filesystem via the host's view of the mount.

### `dispatch_task`

**Purpose**: Schedule a task for execution by the appropriate Worker / Validator.

**Python signature**:
```python
def make_dispatch_task(ctx: TaskContext):
    @function_tool
    async def dispatch_task(
        task_id: str,
        owner: str,                       # role name
        goal: str,
        background: str,
        acceptance_criteria: list[str],
        depends_on: list[str] = [],
        input_artifacts: list[str] = [],
        required_outputs: list[str] = [],
        allowed_paths: list[str] = [],
        allowed_tools: list[str] = [],
        network_policy: str = "none",
        max_tokens: int = 100_000,
        max_runtime_sec: int = 600,
        risk_level: str = "low",
        milestone_id: str | None = None,
    ) -> dict:                            # {"task_id": ..., "dispatched_at": ...}
        """Schedule a task in the mission DAG.

        Validates the task against the locked validation contract: every
        assertion_id in acceptance_criteria MUST exist in the contract.

        `milestone_id` tags the task's milestone; when omitted it defaults to the
        live `mission_state.current_milestone`, then to the Orchestrator turn's own
        milestone.

        Permission: only Orchestrator role may call this tool.
        """
```

**JSON schema** (what the LLM sees — full version generated by OpenAI Agents SDK from the type hints):
```json
{
  "name": "dispatch_task",
  "description": "Schedule a task in the mission DAG. ...",
  "parameters": {
    "type": "object",
    "properties": {
      "task_id": {"type": "string", "description": "Unique task identifier, e.g. 't3'"},
      "owner": {"type": "string", "enum": ["research_worker", "coder_worker", "security_worker", "review_validator", "behavior_validator"]},
      "goal": {"type": "string"},
      "background": {"type": "string"},
      "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "Contract assertion IDs covered, e.g. ['f1.a1', 'f1.a2']"},
      "depends_on": {"type": "array", "items": {"type": "string"}},
      "input_artifacts": {"type": "array", "items": {"type": "string"}},
      "required_outputs": {"type": "array", "items": {"type": "string"}},
      "allowed_paths": {"type": "array", "items": {"type": "string"}},
      "allowed_tools": {"type": "array", "items": {"type": "string"}},
      "network_policy": {"type": "string", "enum": ["none", "crates_only", "whitelist", "open"]},
      "max_tokens": {"type": "integer", "minimum": 1, "default": 100000},
      "max_runtime_sec": {"type": "integer", "minimum": 1, "default": 600},
      "risk_level": {"type": "string", "enum": ["low", "medium", "high"]}
    },
    "required": ["task_id", "owner", "goal", "background", "acceptance_criteria"]
  }
}
```

**Returns**: `TaskHandle` (see §4).

**Raises**:
- `PermissionDeniedError`: caller is not Orchestrator
- `ArtifactError`: validation contract not yet locked, or acceptance_criteria references unknown assertion
- `ValueError`: task_id already exists in DAG

**Logs**: `TASK_DISPATCHED` event with task_id, owner, priority.

---

### `read_artifact`

**Purpose**: Read a mission artifact via the blackboard.

**Python signature**:
```python
def make_read_artifact(ctx: TaskContext):
    @function_tool
    async def read_artifact(path: str) -> str:
        """Read an artifact from the mission blackboard.

        Path is relative to the mission directory. Examples:
          - "plan.md"
          - "validation_contract.yaml"
          - "handoff/t3.json"
          - "verdicts/t3.review.json"
          - "research_notes/api_routing.md"

        Returns the file content as a string. Binary content is base64-encoded.
        """
```

**Returns**: file content (string, UTF-8 by default).

**Raises**:
- `ArtifactError`: file doesn't exist or is unreadable
- `PathEscapeError`: path resolves outside mission_dir

**Notes**:
- No special permission check; Orchestrator may read any artifact in its mission
- For paths > 1MB, content is truncated with a clear marker

---

### `save_artifact`

**Purpose**: Write a mission artifact (Orchestrator-only artifacts like `plan.md`, `risk_register.md`).

**Python signature**:
```python
def make_save_artifact(ctx: TaskContext):
    @function_tool
    async def save_artifact(path: str, content: str) -> str:
        """Write an artifact to the mission blackboard.

        Orchestrator MAY write:
          - plan.md, tasks.yaml, risk_register.md, budget.yaml
          - mission_state.json (via update_mission_state preferred)
          - validation_contract.yaml (ONCE per mission, locks immediately)
          - status_reports/* (typically use emit_status_report instead)
          - final_answer.md, mission_retro.md
          - user_messages/_pending_*.md (for escalation)

        Orchestrator MAY NOT write:
          - handoff/*, patches/*, verdicts/*, reports/* (Worker/Validator outputs)
          - events.jsonl (use emit_event)
          - checkpoints/* (use create_checkpoint)

        Returns the absolute path written.
        """
```

**Returns**: absolute path of written file (string).

**Raises**:
- `PermissionDeniedError`: path is outside Orchestrator's allowed write set
- `ContractAlreadyLockedError`: writing `validation_contract.yaml` when it exists
- `PathEscapeError`: traversal

**Logs**: `ARTIFACT_WRITTEN` event.

---

### `emit_event`

**Purpose**: Append a custom event to the mission EventLog.

**Python signature**:
```python
def make_emit_event(ctx: TaskContext):
    @function_tool
    async def emit_event(
        kind: str,
        payload: dict | None = None,
    ) -> None:
        """Emit a custom event to the mission EventLog.

        For canonical events (TASK_*, LLM_CALL, STATUS_REPORT_EMITTED, etc.)
        prefer using their dedicated helpers — those are called by the
        framework automatically. Use this tool only for events you
        explicitly need to log from agent reasoning.
        """
```

**Returns**: None.

**Raises**: none expected — EventLog appends are non-failing.

---

### `escalate_to_human_gate`

**Purpose**: Write a `_pending` file to `user_messages/` requesting human intervention.

**Python signature**:
```python
def make_escalate_to_human_gate(ctx: TaskContext):
    @function_tool
    async def escalate_to_human_gate(
        reason: str,
        options: list[str],
        recommendation: str | None = None,
        timeout_action: str = "pause_mission",
        timeout_hours: int = 24,
    ) -> None:
        """Request explicit human approval for a decision.

        Creates user_messages/_pending_<timestamp>.md containing:
          - reason (concrete, evidence-based, not "things failed")
          - options (numbered list the human picks from)
          - recommendation (Orchestrator's preferred option if it has one)
          - timeout_action + timeout_hours (what to do if no response)

        Emits ESCALATION_TRIGGERED event.
        Emits an urgent status_report immediately.
        Does NOT block — Orchestrator continues other non-dependent work.

        Resolution: user creates user_messages/<pending_file>.response.md
        with their chosen option; Orchestrator picks it up at next poll.
        """
```

**Returns**: None.

**Raises**: none.

**Logs**: `ESCALATION_TRIGGERED` event.

---

### `create_checkpoint`

**Purpose**: Snapshot mission state at a milestone boundary.

**Python signature**:
```python
def make_create_checkpoint(ctx: TaskContext):
    @function_tool
    async def create_checkpoint(milestone_id: str) -> dict:
        """Create a three-store checkpoint:
          1. git tag the worktree as mission/<mission_id>/<milestone_id>
          2. Docker container commit (image snapshot)
          3. Archive current artifacts into checkpoints/<milestone_id>/

        Updates mission_state.json (completed_milestones, last_checkpoint_at).

        Returns a dict with git_tag, snapshot_id, archive_path,
        cumulative_cost_usd, cumulative_wall_clock_hours.
        """
```

**Returns**: dict with checkpoint metadata (matches `Checkpoint` schema fields).

**Raises**:
- `SandboxError`: git tag or docker commit failed
- `ArtifactError`: archive copy failed

**Logs**: `CHECKPOINT_CREATED` event.

---

### `poll_user_messages`

**Purpose**: Read the user messages inbox at milestone boundaries.

**Python signature**:
```python
def make_poll_user_messages(ctx: TaskContext):
    @function_tool
    async def poll_user_messages() -> list[dict]:
        """List unprocessed user_messages files.

        Returns ordered list of dicts:
          {filename: str, path: str, content: str, urgent: bool, created_at: str}
        Files prefixed with !urgent are returned first.

        After agent processes a message, it MUST call
        mark_user_message_processed(filename) to move it to processed_messages/.
        """
```

**Returns**: list of dicts (see docstring).

---

### `mark_user_message_processed`

**Purpose**: Move a processed user message out of the active inbox.

```python
def make_mark_user_message_processed(ctx: TaskContext):
    @function_tool
    async def mark_user_message_processed(filename: str) -> None:
        """Move user_messages/<filename> to processed_messages/<filename>.
        Updates mission_state.last_user_message_processed_at.
        """
```

---

### `get_mission_state` / `update_mission_state`

**Purpose**: Read / partially update the live mission state.

```python
def make_get_mission_state(ctx: TaskContext):
    @function_tool
    async def get_mission_state() -> dict:
        """Return current mission_state.json as a dict."""

def make_update_mission_state(ctx: TaskContext):
    @function_tool
    async def update_mission_state(updates: dict) -> dict:
        """Patch mission_state.json with the keys in `updates`.
        
        Only specific keys may be patched directly:
          - current_milestone
          - last_status_report_at  (preferred: emit_status_report)
          - coder_provider_in_use
        
        Other keys (completed_milestones, cumulative_cost_usd, etc.) are
        updated by the framework as side effects of other tools — direct
        patching of those raises PermissionDeniedError.
        """
```

---

### `get_budget_status`

```python
def make_get_budget_status(ctx: TaskContext):
    @function_tool
    async def get_budget_status() -> dict:
        """Return current budget state:
        {tokens_used, cost_usd, alert_threshold_usd, projected_total_usd,
         wall_clock_vs_estimate_pct, current_mode (normal|cost_conscious)}
        """
```

### `complete_mission`

```python
def make_complete_mission(ctx: TaskContext):
    @function_tool
    async def complete_mission(summary: str) -> dict:
        """Declare the mission goal fully delivered. Sets
        mission_state.mission_complete = True; the Driver's milestone loop reads
        this and stops re-invoking the Orchestrator. Returns {"mission_complete": True}.
        Permission: Orchestrator only. Call only after the final milestone PASSED.
        """
```

### `save_retro` (Phase F)

```python
def make_save_retro(ctx: TaskContext):
    @function_tool
    async def save_retro(...) -> dict:
        """Assemble + persist mission_retro.md (what worked / failed / surprises /
        global_lessons) and write project- and global-scope memory entries.
        Permission: Orchestrator only. See §8 (cross-mission memory)."""
```

### `create_pr` (Phase F)

```python
def make_create_pr(ctx: TaskContext):
    @function_tool
    async def create_pr(...) -> dict:
        """Open a PR via `gh`/`glab` in the sandbox: refuses on a dirty tree and on
        gitleaks findings (pre-PR gate), generates the PR description from
        mission_retro/final_answer + verdicts, and links artifacts. Returns the PR URL.
        Permission: Orchestrator only. See integrations/vcs.py."""
```

---

## 7. Coder Worker tools (Phase B)

The Coder Worker has the largest tool surface. All of its read/write tools dispatch via the sandbox.

### Permission requirements common to all Coder tools

Before executing, every Coder tool MUST verify:
1. The current task's owner is `Role.CODER_WORKER`
2. Path/tool/command-pattern check per §5
3. Worktree integrity (the `git checkout` at task start has run — see soul.md v3.1 idempotent writes rule)

### `read_file`

```python
def make_read_file(ctx: TaskContext):
    @function_tool
    async def read_file(path: str, max_bytes: int = 1_000_000) -> FileContent:
        """Read a file from the sandbox worktree.
        
        Path is relative to /workspace/<repo>/.
        Returns the file content (truncated to max_bytes if larger).
        """
```

**Permission**: `check_path_access(perm, path, mode="read")`.

**Sandbox call**: `docker exec <container> cat <path>` (or via dockerpy file API).

---

### `write_file`

```python
def make_write_file(ctx: TaskContext):
    @function_tool
    async def write_file(path: str, content: str) -> str:
        """Write a file in the sandbox worktree.
        
        STATE-BASED write — overwrites existing content. Use this for
        creating new files OR rewriting an entire file. For partial edits,
        use edit_file instead.
        
        Returns the relative path written.
        """
```

**Permission**: `check_path_access(perm, path, mode="write")`.

**Sandbox call**: write to a temp file in container, then `mv` (atomic).

---

### `edit_file`

```python
def make_edit_file(ctx: TaskContext):
    @function_tool
    async def edit_file(
        path: str,
        old_string: str,
        new_string: str,
        expected_replacements: int = 1,
    ) -> str:
        """Replace exactly one (or N) occurrence(s) of old_string in path.
        
        FAILS if old_string occurs != expected_replacements times.
        This forces the agent to be specific about which occurrence it means.
        """
```

**Permission**: `check_path_access(perm, path, mode="write")` and read-then-write.

---

### `run_bash`

```python
def make_run_bash(ctx: TaskContext):
    @function_tool
    async def run_bash(
        cmd: str,
        timeout_sec: int = 60,
        cwd: str | None = None,
    ) -> CommandResult:
        """Run an arbitrary bash command in the sandbox.
        
        cwd defaults to /workspace/<repo>/. Timeout applies to the full
        command including any subshells.
        """
```

**Permission**: `check_command_pattern(perm, cmd)` rejects external-side-effect commands per soul.md §13.

**Sandbox call**: `docker exec -w <cwd> <container> bash -c <cmd>`.

**Truncation**: stdout/stderr truncated to 50KB each by default; full output saved to `events.jsonl` payload for forensic replay.

---

### `cargo_check`, `cargo_test`, `cargo_clippy`, `cargo_fmt`, `cargo_nextest`

All follow the same pattern:

```python
def make_cargo_check(ctx: TaskContext):
    @function_tool
    async def cargo_check(args: list[str] = []) -> CommandResult:
        """Run `cargo check` with optional extra args.
        Default: `cargo check --workspace --all-targets`.
        """

def make_cargo_test(ctx: TaskContext):
    @function_tool
    async def cargo_test(args: list[str] = []) -> CommandResult:
        """Run `cargo test` with optional extra args.
        Default: `cargo test --workspace`.
        """

def make_cargo_clippy(ctx: TaskContext):
    @function_tool
    async def cargo_clippy(args: list[str] = []) -> CommandResult:
        """Run `cargo clippy` with optional extra args.
        Default: `cargo clippy --workspace --all-targets --all-features -- -D warnings`.
        """

def make_cargo_fmt(ctx: TaskContext):
    @function_tool
    async def cargo_fmt(check_only: bool = False) -> CommandResult:
        """Run `cargo fmt`. If check_only, runs with --check (no modification)."""

def make_cargo_nextest(ctx: TaskContext):
    @function_tool
    async def cargo_nextest(args: list[str] = []) -> CommandResult:
        """Run `cargo nextest run` with optional extra args.
        Default: `cargo nextest run --workspace --all-features`.
        Falls back to None if nextest not in project_profile.test_strategy.
        """
```

---

### `git_status`, `git_diff`, `git_show`, `git_log`, `git_checkout`

```python
def make_git_status(ctx: TaskContext):
    @function_tool
    async def git_status() -> str:
        """Run git status --short in the worktree."""

def make_git_diff(ctx: TaskContext):
    @function_tool
    async def git_diff(args: list[str] = []) -> str:
        """Run git diff with optional args. Default: HEAD."""

def make_git_show(ctx: TaskContext):
    @function_tool
    async def git_show(ref: str) -> str:
        """Run git show <ref>."""

def make_git_log(ctx: TaskContext):
    @function_tool
    async def git_log(args: list[str] = []) -> str:
        """Run git log with optional args. Default: -10 --oneline."""

def make_git_checkout(ctx: TaskContext):
    @function_tool
    async def git_checkout(target: str = "--", paths: list[str] = []) -> CommandResult:
        """Reset worktree to HEAD (default) or to specific paths.
        
        Used at task start for the v3.1 idempotent-writes rule:
          git_checkout(target="--")
        Discards all uncommitted changes.

        Forbidden targets: branch refs (main, master, etc.) — use only
        for reset operations within the current branch.
        """
```

**Permission**: `git_checkout` rejects any target that looks like a branch name.

---

### Coder output-saving tools

```python
def make_save_patch(ctx: TaskContext):
    @function_tool
    async def save_patch(task_id: str) -> str:
        """Generate the diff for the current worktree state vs the parent
        commit, save to patches/<task_id>.diff. Returns the relative path."""

def make_save_handoff(ctx: TaskContext):
    @function_tool
    async def save_handoff(
        task_id: str,
        completed: list[str],
        incomplete: list[str] = [],
        commands_run: list[dict] = [],
        issues_discovered: list[str] = [],
        deviations_from_plan: list[str] = [],
        contract_coverage: list[dict] = [],
        dependency_changes: list[dict] = [],
        unsafe_usage: list[dict] = [],
        next_recommended_action: str = "send_to_review_validator",
    ) -> str:
        """Save the v3.1 handoff for this task.

        The tool validates against the Handoff schema before writing.
        Recall: at least one of {incomplete, issues_discovered,
        deviations_from_plan} SHOULD be non-empty — the framework
        automatically flags handoffs with all three empty for second-pass.
        """

def make_save_test_report(ctx: TaskContext):
    @function_tool
    async def save_test_report(task_id: str, report: dict) -> str:
        """Save reports/<task_id>.test.json from the Coder's self-test results."""
```

**Permission**: Coder may only write to its own `task_id`'s handoff/patch/report files.

---

## 8. ReviewValidator tools (Phase B)

Read-only on code; write-only to its own verdict + notes. Adversarial sub-agent spawning is the key new capability.

### `read_file`, `grep`, `glob`, `git_diff`, `git_show`, `git_log`

Same signatures as Coder Worker, but with `allowed_paths` defaulting to read-only on the full worktree and `network_policy=NONE`.

### `cargo_check`, `cargo_build`, `cargo_test`, `cargo_clippy`, `cargo_fmt`, `cargo_nextest`, `cargo_test_doc`

Same as Coder Worker, but execution path verifies the verdict will be written, not the worktree modified.

### `apply_patch_in_fresh_worktree`

```python
def make_apply_patch_in_fresh_worktree(ctx: TaskContext):
    @function_tool
    async def apply_patch_in_fresh_worktree(
        patch_path: str,
        base_ref: str = "HEAD",
    ) -> dict:
        """Apply patches/<task_id>.diff to a fresh worktree at base_ref.

        Verifies the patch applies cleanly without conflicts. Returns:
          {applied: bool, conflicts: list[str], files_changed: list[str]}
        """
```

**Sandbox call**: `git worktree add` to a tmp dir, `git apply` the diff, capture result.

---

### `spawn_adversarial_subagent`

The cornerstone of v3.1 review discipline.

```python
def make_spawn_adversarial_subagent(ctx: TaskContext):
    @function_tool
    async def spawn_adversarial_subagent(
        purpose: str,
        inputs: dict,
        instructions_override: str | None = None,
    ) -> dict:
        """Spawn a one-shot sub-agent with scoped context.

        purpose: "intent_test_detection" | "completeness_second_pass" | "<other>"

        inputs: dict of fields the sub-agent receives. Must NOT contain:
          - The Coder's handoff text
          - The ReviewValidator's prior reasoning
          - System prompts of any other agent
        
        instructions_override: optional override of the default sub-agent
        instructions for this purpose. The framework provides a default per
        known purpose.

        Returns:
          {findings: list[str], adversarial_tests_generated: list[str]}
        
        The sub-agent runs on a DIFFERENT provider from both Coder AND the
        parent ReviewValidator (enforced by router; see §3 of soul.md).
        """
```

**Permission**: only ReviewValidator may call.

**Logs**: `SECOND_PASS_TRIGGERED` event for completeness purpose; sub-agent's own task gets `TASK_DISPATCHED` + `TASK_COMPLETE` events.

---

### ReviewValidator output-saving tools

```python
def make_save_review_verdict(ctx: TaskContext):
    @function_tool
    async def save_review_verdict(
        task_id: str,
        result: str,                  # "pass" | "partial" | "fail"
        precise_reason: str,
        next_action_recommendation: str,
        cargo_gate_results: dict,
        assertion_results: list[dict] = [],
        triggered_second_pass: bool = False,
        adversarial_findings: list[str] = [],
        hardcoded_test_warnings: list[str] = [],
    ) -> str:
        """Save verdicts/<task_id>.review.json. Validates against schema."""

def make_save_review_notes(ctx: TaskContext):
    @function_tool
    async def save_review_notes(task_id: str, notes_markdown: str) -> str:
        """Save review_notes/<task_id>.md."""
```

**Logs**: `VALIDATOR_VERDICT` event with `validator: review_validator` and the result.

---

## 9. Research Worker tools (Phase C — sketched)

Read-only on code, parallel-safe, can fetch external content.

```python
def make_fetch_url(ctx: TaskContext):
    @function_tool
    async def fetch_url(url: str, timeout_sec: int = 30) -> SanitizedContent:
        """HTTP GET with sanitizer applied.
        
        Permission: check_network_allowed(perm, url, domain_whitelist).
        For network_policy=crates_only, allowed domains are:
          crates.io, docs.rs, github.com (and *.github.io for crate docs).
        
        Sanitizer:
          - Strips <script>, <iframe>, <object>, <embed>
          - Strips known prompt-injection markers (e.g. "ignore previous
            instructions", "/system:" headings in untrusted markdown)
          - Records all actions taken in SanitizedContent.sanitization_actions
        
        Emits external_content_received event with origin URL and action log.
        """

def make_save_research_note(ctx: TaskContext):
    @function_tool
    async def save_research_note(topic: str, content_markdown: str) -> str:
        """Save research_notes/<topic>.md.
        
        Topic format: kebab-case identifier (e.g. 'axum-routing', 'tokio-vs-async-std').
        """

def make_save_code_map(ctx: TaskContext):
    @function_tool
    async def save_code_map(module: str, content_markdown: str) -> str:
        """Save code_map/<module>.md.
        
        Module format: kebab-case identifier matching the module being mapped.
        """

def make_save_dependency_brief(ctx: TaskContext):
    @function_tool
    async def save_dependency_brief(content_markdown: str) -> str:
        """Save research_notes/dependency_brief.md (a single per-mission brief)."""

def make_save_workspace_overview(ctx: TaskContext):
    @function_tool
    async def save_workspace_overview(content_markdown: str) -> str:
        """Save research_notes/workspace_overview.md (a single per-mission overview)."""

def make_cargo_metadata(ctx: TaskContext):
    @function_tool
    async def cargo_metadata() -> dict:
        """Run `cargo metadata --format-version 1`, return parsed JSON."""

def make_cargo_tree(ctx: TaskContext):
    @function_tool
    async def cargo_tree(args: list[str] | None = None) -> CommandResult:
        """Run `cargo tree` with optional extra args (dependency graph)."""

def make_grep(ctx: TaskContext):
    @function_tool
    async def grep(
        pattern: str,
        paths: list[str] = ["."],
        case_insensitive: bool = False,
        context_lines: int = 0,
    ) -> list[GrepMatch]:
        """Run ripgrep over the worktree."""

def make_glob(ctx: TaskContext):
    @function_tool
    async def glob(pattern: str, cwd: str = ".") -> list[str]:
        """Return paths matching glob pattern."""
```

---

## 10. Security Worker tools (Phase C — sketched)

Read-only on code + audit tool invocations.

```python
def make_cargo_audit(ctx: TaskContext):
    @function_tool
    async def cargo_audit() -> dict:
        """Run cargo audit, return parsed JSON findings."""

def make_cargo_deny_check(ctx: TaskContext):
    @function_tool
    async def cargo_deny_check() -> dict:
        """Run cargo deny check, return parsed findings."""

def make_cargo_geiger(ctx: TaskContext):
    @function_tool
    async def cargo_geiger() -> dict:
        """Run cargo geiger, return unsafe usage report."""

def make_gitleaks_detect(ctx: TaskContext):
    @function_tool
    async def gitleaks_detect(path: str = ".") -> dict:
        """Run gitleaks detect, return a findings report dict (also reused as the
        pre-PR secrets gate by create_pr)."""

def make_trufflehog_scan(ctx: TaskContext):
    @function_tool
    async def trufflehog_scan(path: str = ".") -> dict:
        """Run trufflehog scan, return a findings report dict."""

def make_save_security_verdict(ctx: TaskContext):
    @function_tool
    async def save_security_verdict(
        task_id: str,
        findings: list[dict],
    ) -> str:
        """Save verdicts/<task_id>.security.json. Validates against SecurityVerdict
        schema. blocks_pr is derived from findings; do not pass it."""

def make_save_security_notes(ctx: TaskContext):
    @function_tool
    async def save_security_notes(task_id: str, content_markdown: str) -> str:
        """Save security_notes/<task_id>.md (human-readable scan narrative)."""
```

---

## 11. BehaviorValidator tools (Phase D — sketched)

Probe strategies: `cli_assert_cmd_probe`, `backend_service_health_probe`, `library_example_probe`, `embedded_host_test_probe`, `wasm_node_probe`.

```python
def make_start_service(ctx: TaskContext):
    @function_tool
    async def start_service(
        command: str,
        ready_check: str,
        timeout_sec: int = 300,
    ) -> dict:
        """Start a long-running service command in the sandbox.
        
        Waits for ready_check to return 0 (or until timeout). Returns:
          {service_id, started_at, log_path}
        """

def make_stop_service(ctx: TaskContext):
    @function_tool
    async def stop_service(service_id: str) -> None:
        """Stop a service started by start_service."""

def make_probe_http(ctx: TaskContext):
    @function_tool
    async def probe_http(
        url: str,
        method: str = "GET",
        body: str | None = None,
        expected_status: int | None = None,
    ) -> dict:
        """HTTP probe against a sandbox-internal URL (typically localhost:PORT)."""

def make_probe_cli(ctx: TaskContext):
    @function_tool
    async def probe_cli(
        binary: str,
        args: list[str] = [],
        stdin: str | None = None,
        expected_exit_code: int | None = None,
    ) -> dict:
        """CLI probe — runs a binary with given args, captures stdout/stderr/exit."""

def make_save_behavior_verdict(ctx: TaskContext):
    @function_tool
    async def save_behavior_verdict(
        task_id: str,
        result: str,
        probe_strategy: str,
        observations: list[dict] = [],
        evidence_path: str = "",
        failure_reason: str | None = None,
    ) -> str:
        """Save verdicts/<task_id>.behavior.json. Validates against schema."""

def make_save_behavior_evidence(ctx: TaskContext):
    @function_tool
    async def save_behavior_evidence(task_id: str, name: str, content: bytes) -> str:
        """Save behavior_evidence/<task_id>/<name>. For traces, logs, recorded
        responses that BehaviorVerdict.evidence_path points to."""

def make_run_behavior_probes(ctx: TaskContext):
    @function_tool
    async def run_behavior_probes(task_id: str) -> dict:
        """Drive the probe strategy from project_profile.behavior_probe end to end
        (start service / probe / collect observations), returning a result dict the
        BehaviorValidator turns into a verdict."""
```

---

## 12. Multi-day infrastructure (Phase E)

> The originally-sketched `request_status_report_emission` and `set_budget_mode`
> tools were **not** implemented. Phase E multi-day infrastructure runs instead as
> `SupervisionHook`s on the `MissionSupervisor` tick loop (see §14), not as
> Orchestrator-facing tools:
>
> - **budget guard** (`orchestrator/budget.py`, `make_budget_guard`) — sets
>   `mission_state.budget_mode` automatically at the 50/80/100/150% bands.
> - **status report** (`orchestrator/status_report.py`, `make_status_report_hook`).
> - **inbox poll** (`orchestrator/inbox.py`, `make_inbox_poll_hook`).
>
> The Orchestrator's relevant tools live in §6: `get_budget_status`,
> `poll_user_messages`, `mark_user_message_processed`, and `update_mission_state`.

---

## 13. Scheduler interface

The Scheduler is an internal component, not directly tool-exposed. The Orchestrator dispatches work into it via the `dispatch_task` tool; dispatch is fire-and-forget (the Orchestrator inspects results on its next milestone turn, not by waiting). The Mission Driver builds the Scheduler and runs its loop.

### Interface

```python
# src/maf_coder/orchestrator/scheduler.py
class Scheduler:
    def __init__(
        self,
        *,
        store: ArtifactStore,
        event_log: EventLog,
        router: ModelRouter,
        sandbox: SandboxClient,
        agent_factory: dict[Role, Callable[[], BaseAgent]],
        mission_id: str,
        coder_provider_in_use: str | None = None,
    ) -> None: ...

    async def add_task(self, task: Task) -> TaskHandle:
        """Add a task to the DAG. Returns immediately; execution starts when ready."""

    async def wait_for(self, task_id: str, timeout_sec: float | None = None) -> AgentResult:
        """Block until the given task is complete. Raises TimeoutError if exceeded."""

    async def cancel(self, task_id: str) -> None:
        """Cancel a pending or active task. Future-only for v1."""

    def task_status(self, task_id: str) -> TaskStatus:
        """Read current status from in-memory + EventLog."""

    async def run(self) -> None:
        """The scheduler's main loop. Returns when all tasks complete or fail."""

    def stats(self) -> dict:
        """Snapshot: {pending, active, complete, failed} counts + active_workers map."""
```

### Slot management

Internal state:
```python
self._active_by_role: dict[Role, int] = defaultdict(int)   # in-flight count per role
# A per-role cap (1 for CODER_WORKER / BEHAVIOR_VALIDATOR, unbounded for read-only roles)
# gates dispatch: a task is dispatchable iff _active_by_role[owner] < cap.
```

Dispatch decisions are made under an `asyncio.Lock` to prevent races.

### Concurrency rules implementation

```python
def _is_ready(self, rec) -> bool:
    # 1. All depends_on are complete
    if any(self._tasks[dep].state != "complete" for dep in rec.task.depends_on):
        return False
    # 2. The owner role's slot cap is not exhausted
    return self._active_by_role[owner] < self._cap_for(owner)
```

Behavior_validator has an extra runtime gate: it only runs once its `review_validator`
dependency has a PASS verdict on disk (the dual-validator chain, §D3).

---

## 14. Mission Driver interface

The top-level coroutine that orchestrates a full mission. Owns: scheduler, agents, sandbox, lifecycle.

```python
# src/maf_coder/orchestrator/mission_driver.py
class MissionDriver:
    def __init__(
        self,
        *,
        mission_id: str,
        config: MissionConfig,   # carries repo_path, goal, router_config, sandbox_factory, budget, ...
    ) -> None: ...

    async def start(self) -> None:
        """Run the full mission lifecycle:
          1. Start the sandbox (docker run / local shell)
          2. Initialize mission_state.json; seed budget.yaml (default if absent)
          3. Run project_profiler -> save project_profile.yaml  (Driver-side)
          4. Run the milestone loop under a concurrent MissionSupervisor:
             for each milestone — set current_milestone, build a fresh Scheduler,
             RE-INVOKE the Orchestrator (it plans on the first turn, then dispatches
             this milestone's DAG / reviews + checkpoints prior verdicts), drain the
             Scheduler, and stop when the Orchestrator calls complete_mission.
          5. Finalize (mission_end); the completing Orchestrator turn writes the
             retro + opens the PR via its tools.
          6. Stop the sandbox (preserve volumes)

        Catches:
          - asyncio.CancelledError: graceful shutdown, emit MISSION_END(aborted)
          - Any uncaught exception: log + emit MISSION_END(crashed) + raise
        """

    async def resume(self, from_milestone: str | None = None) -> None:
        """Resume a previously-started mission from disk state.
        See ARCHITECTURE.md §6.3 for resume semantics."""

    async def stop(self, graceful: bool = True) -> None:
        """Stop the mission. graceful=True waits for current task,
        graceful=False kills immediately."""
```

### Supervision hooks (run by the concurrent MissionSupervisor)

`start()` runs the milestone loop under one `MissionSupervisor`, a heartbeat tick
loop (`asyncio.create_task`, stopped cleanly on every exit path). Three hooks are
registered on it — each is a `SupervisionHook` invoked per tick, NOT a separate
coroutine or event subscriber:

- **budget guard** (`make_budget_guard`): each tick, compares EventLog spend to the
  budget and crosses the 50/80/100/150% bands (sets `mission_state.budget_mode`).
- **status report** (`make_status_report_hook`): emits a report when ≥ the interval
  (`DEFAULT_STATUS_INTERVAL` = 4h) has elapsed since the last.
- **inbox poll** (`make_inbox_poll_hook`): drains `user_messages/`.

The supervisor is scheduler-independent, so it spans the whole milestone loop even
though the Scheduler is rebuilt per milestone.

---

## 15. SandboxClient interface

Low-level wrapper around `aiodocker` or `docker-py` (async-wrapped).

```python
# src/maf_coder/sandbox/client.py
class SandboxClient:
    def __init__(self, container_name: str, image: str) -> None: ...

    async def start(self, *, workspace_mount: Path, volumes: dict) -> None:
        """docker run the container. Mount workspace_mount as /workspace.
        Mount each volume as configured (cargo-cache, target-cache, sccache).
        """

    async def stop(self, *, preserve_volumes: bool = True) -> None:
        """docker stop the container. Volumes persist unless preserve_volumes=False.
        """

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str = "/workspace",
        timeout_sec: int = 60,
        capture_output: bool = True,
        stdin: str | None = None,
    ) -> CommandResult:
        """docker exec the command. Returns CommandResult.
        
        Truncates stdout/stderr to 50KB by default; full output written to
        events.jsonl payload for forensic replay.
        """

    async def write_file(self, container_path: str, content: str) -> None:
        """Write content to a file inside the container (atomically via tmp+mv)."""

    async def read_file(self, container_path: str, max_bytes: int = 1_000_000) -> FileContent:
        """Read a file from the container."""

    async def commit_snapshot(self, image_tag: str) -> str:
        """docker commit the container to image_tag. Returns image id."""

    async def health_check(self) -> bool:
        """Returns True if container is running + can execute simple commands.
        Used for sandbox crash detection."""
```

---

## 16. OpenAI Agents SDK integration patterns

### Tool registration

```python
from openai_agents import Agent, Runner, function_tool
from openai_agents.models import LitellmModel
from openai_agents.model_settings import ModelSettings

# Inside BaseAgent.run:
tools = self.build_tools(ctx)  # list of @function_tool decorated callables

agent = Agent(
    name=self.role.value,
    instructions=self._instructions,
    tools=tools,
    model=LitellmModel(model_cfg.model),
    model_settings=ModelSettings(
        temperature=model_cfg.temperature,
        max_tokens=model_cfg.max_tokens,
    ),
)

result = await Runner.run(agent, first_user_message)
parsed = self.parse_output(result.final_output, ctx)
```

### When to use a Handoff vs a tool

OpenAI Agents SDK has both `Tool` and `Handoff` primitives. Our framework uses **tools exclusively in v1**. Handoffs (the SDK term) would suggest agent-to-agent direct transfers; we use blackboard handoff artifacts instead, which fit the multi-day persistence model. Revisit in v2.

### Tracing

The SDK provides built-in tracing. Configure in `MissionDriver.start`:
```python
import openai_agents
openai_agents.set_default_trace_processor(MyTraceProcessor(event_log=ctx.event_log))
```

The trace processor forwards SDK trace events into our EventLog as `LLM_CALL` and `TOOL_CALL` events.

### Sub-agent invocation

For `spawn_adversarial_subagent` (§8), we construct a fresh `Agent` with restricted tools and run it inside the parent's tool call:

```python
async def spawn_adversarial_subagent(purpose, inputs, instructions_override=None):
    sub_role = Role.ADVERSARIAL_SUBAGENT
    sub_model = ctx.router.get_primary_model(
        sub_role,
        coder_provider_in_use=ctx.coder_provider_in_use,
        # Also exclude this agent's provider — see prompt_review_validator
    )
    sub_agent = Agent(
        name="adversarial_subagent",
        instructions=instructions_override or DEFAULT_SUBAGENT_PROMPTS[purpose],
        tools=[],  # NO tools — pure reasoning over inputs
        model=LitellmModel(sub_model.model),
        model_settings=ModelSettings(temperature=0.0),
    )
    sub_msg = build_subagent_message(purpose, inputs)
    sub_result = await Runner.run(sub_agent, sub_msg)
    return parse_subagent_output(sub_result.final_output, purpose)
```

---

## 17. Phase implementation order

This is the sequence Cursor should follow when implementing. Each row depends on rows above.

| Order | Phase | Component | Why this order |
|---|---|---|---|
| 1 | B | `BaseAgent`, `TaskContext`, error types, result types (§2, §3, §4) | Everything else uses these |
| 2 | B | Permission layer (§5) | Tools need this before they can be written safely |
| 3 | B | `SandboxClient` (§15) | Tools need this to execute sandbox commands |
| 4 | B | Coder Worker tools (§7) | Smallest coherent role with most surface area; testable end-to-end |
| 5 | B | `CoderWorkerAgent` (BaseAgent subclass) | Wires tools to a runnable agent |
| 6 | B | ReviewValidator tools + Agent (§8) | Completes the Coder→Validator loop |
| 7 | B | Orchestrator tools (§6), `OrchestratorAgent`, `Scheduler` (§13) | Top-level coordination |
| 8 | B | `MissionDriver` (§14, minimal — no resume, no background tasks yet) | Wires it all together |
| 9 | B | Project Profiler (a special tool/function, not a full agent) | Required at mission start |
| 10 | B | CLI (`maf-coder mission` command) | User entry point |
| 11 | C | Research Worker tools + Agent (§9) | Adds the third Worker |
| 12 | C | Security Worker tools + Agent (§10) | Adds parallel-safe security |
| 13 | C | Sanitizer module + integration | Required before opening Research's network |
| 14 | D | BehaviorValidator tools + Agent (§11) | Adds the second validator |
| 15 | E | Multi-day infrastructure tools (§12), Checkpoint, Status Report timer, Budget Guard, Stuck Recovery, Resume | Multi-day capability |
| 16 | F | Memory store + retrieval | Cross-mission learning |

---

## 18. Appendix: JSON schema conventions

The OpenAI Agents SDK auto-generates JSON schema from Python type hints. Conventions:

- `str | None = None` → optional string param
- `list[str] = []` → optional list with default empty (caller can omit)
- Enum-like string → `Literal["a", "b", "c"]` (generates JSON enum constraint)
- Complex nested dict → define a `TypedDict` or `BaseModel` and reference; SDK inlines schema
- Date/time → `str` with ISO 8601 docstring; we do not use SDK's datetime mapping (inconsistent across providers)

When the auto-generated schema is wrong for the LLM (e.g. an enum needs a non-Literal Python type because of dynamic enum extension), use the SDK's `@function_tool(json_schema={...})` override syntax with an explicit dict.

### Tool description style

The Python docstring is the tool description seen by the LLM. Conventions:

- First line: one-sentence verb-led description
- Followed by paragraphs describing parameters, behavior, side effects
- End with examples if the right usage is non-obvious
- Do NOT include implementation details (the LLM doesn't need to know it's a docker exec underneath)
- DO include constraints the LLM should respect (e.g. "old_string must occur exactly N times in the file")

---

## Cross-references

- For artifact schema field details: `src/maf_coder/schemas/*.py` docstrings
- For agent behavioral rules: `prompts/<role>.md`
- For organizational rules: `agent_team_soul_v3.1.md`
- For lifecycle and topology: `ARCHITECTURE.md`
- For concrete usage examples: `WORKED_EXAMPLE.md` (next deliverable)
