"""Tool error hierarchy (AGENT_TOOLS_SPEC §3).

Every exception raised inside a tool function MUST be a subclass of `ToolError`.
The SDK serializes these into the tool-result string the agent sees; the agent
is expected to interpret denials/conflicts and recover rather than crash.

Layered with care:
- `PermissionDeniedError` — the security boundary. NEVER swallow this in tool
  code; the agent must see the denial and choose a different path.
- `SandboxError` — wraps "infrastructure broke" failures, NOT routine non-zero
  exit codes (those are returned via CommandResult).
- `ArtifactError` — wraps disk-side issues from ArtifactStore + traversal.
- `ExternalContentError` — sanitizer rejection or HTTP fetch errors.
- `BudgetExceededError` — token / runtime / cost exhaustion.
"""
from __future__ import annotations


class ToolError(Exception):
    """Base for all tool-level errors."""


class PermissionDeniedError(ToolError):
    """Tool call denied by permission check.

    Raised when the agent tries to access a path/tool/domain/command-pattern
    outside the task's permission boundary. Carries `what` (the thing being
    denied) and `why` (the reason, suitable for the agent to read).
    """

    def __init__(self, what: str, why: str) -> None:
        self.what = what
        self.why = why
        super().__init__(f"{what}: {why}")


class SandboxError(ToolError):
    """Error executing a command in the sandbox container.

    Wraps non-zero exit codes ONLY when the contract is "this command must
    succeed" (e.g. failure to start a long-running probe). Routine command
    failures like `cargo test` returning non-zero are NOT exceptions — they
    are returned as `CommandResult` with `exit_code != 0`.
    """


class ArtifactError(ToolError):
    """Error reading/writing an artifact.

    Common subclasses raised from `ArtifactStore`:
    - `PathEscapeError` (re-exported here for tool callers)
    - `ContractAlreadyLockedError`
    - `FileNotFoundError`-derived "artifact missing" cases
    """


class ExternalContentError(ToolError):
    """Error fetching external content, or sanitizer rejection."""


class BudgetExceededError(ToolError):
    """Tool call denied because task budget is exhausted."""


class TaskAlreadyDispatchedError(ToolError):
    """`dispatch_task` called for a task_id that already exists in the DAG."""


class AssertionUnknownError(ToolError):
    """`dispatch_task.acceptance_criteria` references an assertion not in the
    locked validation contract.
    """


__all__ = [
    "ToolError",
    "PermissionDeniedError",
    "SandboxError",
    "ArtifactError",
    "ExternalContentError",
    "BudgetExceededError",
    "TaskAlreadyDispatchedError",
    "AssertionUnknownError",
]
