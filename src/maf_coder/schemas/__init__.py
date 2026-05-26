"""Schema layer — Pydantic v2 models for all artifacts and messages.

Public API: import everything from `maf_coder.schemas`. Submodule
re-exports below.

Schema files in this package mirror soul.md sections directly:
- common.py     — enums (Role / Intent / Severity / ...)
- message.py    — §11.1 inter-agent message envelope
- task.py       — §16 task template
- handoff.py    — §11.3 + v3.1 完备性规则
- contract.py   — §11.4 validation contract
- profile.py    — §6.1 project profile
- verdict.py    — §3.4–3.6 validator outputs
- lifecycle.py  — §5.2–5.3 status reports + checkpoints
"""

from .common import (
    Intent,
    NetworkPolicy,
    ProjectType,
    RiskLevel,
    Role,
    Severity,
    VerdictResult,
    VerificationMethod,
)
from .contract import Assertion, Feature, ValidationContract
from .handoff import (
    CommandRun,
    ContractCoverage,
    DependencyChange,
    Handoff,
    UnsafeUsage,
)
from .lifecycle import (
    BudgetStatus,
    Checkpoint,
    MilestoneStatus,
    MissionState,
    StatusReport,
)
from .message import Budgets, Message, RiskFlag
from .profile import (
    BehaviorProbeSpec,
    BuildSystem,
    CIExisting,
    Crate,
    FeatureMatrix,
    ProjectProfile,
    TestStrategy,
    Toolchain,
)
from .task import FailureHandling, Permission, Task, TaskBudget
from .verdict import (
    AssertionResult,
    BehaviorObservation,
    BehaviorVerdict,
    CargoGateResults,
    ReviewVerdict,
    SecurityFinding,
    SecurityVerdict,
)

__all__ = [
    # Common
    "Intent",
    "NetworkPolicy",
    "ProjectType",
    "RiskLevel",
    "Role",
    "Severity",
    "VerdictResult",
    "VerificationMethod",
    # Message
    "Message",
    "RiskFlag",
    "Budgets",
    # Task
    "Task",
    "Permission",
    "TaskBudget",
    "FailureHandling",
    # Handoff
    "Handoff",
    "CommandRun",
    "ContractCoverage",
    "DependencyChange",
    "UnsafeUsage",
    # Contract
    "ValidationContract",
    "Feature",
    "Assertion",
    # Profile
    "ProjectProfile",
    "Crate",
    "Toolchain",
    "FeatureMatrix",
    "BuildSystem",
    "TestStrategy",
    "BehaviorProbeSpec",
    "CIExisting",
    # Verdict
    "ReviewVerdict",
    "BehaviorVerdict",
    "SecurityVerdict",
    "AssertionResult",
    "BehaviorObservation",
    "CargoGateResults",
    "SecurityFinding",
    # Lifecycle
    "StatusReport",
    "Checkpoint",
    "MissionState",
    "BudgetStatus",
    "MilestoneStatus",
]
