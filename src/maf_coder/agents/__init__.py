"""maf_coder.agents — BaseAgent shell + tool factories + role agents.

Public surface (Phase B):

- BaseAgent / TaskContext / AgentResult        (base.py)
- Tool errors (ToolError, PermissionDeniedError, ...)  (errors.py)
- Result dataclasses (CommandResult, FileContent, ...) (results.py)
- Permission helpers (check_path_access, ...)          (permissions.py)
- CoderWorkerAgent      (coder.py)
- ReviewValidatorAgent  (review.py)
- OrchestratorAgent     (orchestrator.py)
"""
from __future__ import annotations

from .base import AgentResult, BaseAgent, TaskContext
from .errors import (
    ArtifactError,
    AssertionUnknownError,
    BudgetExceededError,
    ExternalContentError,
    PermissionDeniedError,
    SandboxError,
    TaskAlreadyDispatchedError,
    ToolError,
)
from .results import (
    CommandResult,
    FileContent,
    GrepMatch,
    SanitizedContent,
    TaskHandle,
    TaskStatus,
)

__all__ = [
    # Base
    "BaseAgent", "TaskContext", "AgentResult",
    # Errors
    "ToolError", "PermissionDeniedError", "SandboxError", "ArtifactError",
    "ExternalContentError", "BudgetExceededError", "TaskAlreadyDispatchedError",
    "AssertionUnknownError",
    # Results
    "CommandResult", "FileContent", "GrepMatch", "SanitizedContent",
    "TaskHandle", "TaskStatus",
]
