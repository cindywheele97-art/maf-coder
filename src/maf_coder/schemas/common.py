"""Common types and enums shared across schemas.

These map 1:1 to the role / intent / severity vocabulary in agent_team_soul.md.
Changes here are semantically MAJOR — they reshape what's expressible in messages.
"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """All agent roles in soul.md §3."""

    ORCHESTRATOR = "orchestrator"
    RESEARCH_WORKER = "research_worker"
    CODER_WORKER = "coder_worker"
    SECURITY_WORKER = "security_worker"
    REVIEW_VALIDATOR = "review_validator"
    BEHAVIOR_VALIDATOR = "behavior_validator"
    ADVERSARIAL_SUBAGENT = "adversarial_subagent"
    HUMAN_GATE = "human_gate"


class Intent(str, Enum):
    """Message intent — what the sender wants the recipient to do.

    Mirrors soul.md §11.1 intent vocabulary.
    """

    PLAN = "plan"
    IMPLEMENT = "implement"
    RESEARCH = "research"
    VALIDATE = "validate"
    SECURITY_AUDIT = "security_audit"
    BEHAVIOR_PROBE = "behavior_probe"
    ESCALATE = "escalate"
    REPORT = "report"
    STATUS = "status"


class RiskLevel(str, Enum):
    """Task-level risk classification. Used for routing & escalation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Severity(str, Enum):
    """Security finding severity (soul.md §3.4)."""

    CRITICAL = "critical"  # Blocks PR, escalate to Human Gate
    HIGH = "high"          # Blocks ReviewValidator pass
    MEDIUM = "medium"      # Risk register entry, PR description note
    LOW = "low"            # PR description note only


class VerdictResult(str, Enum):
    """Validator verdict."""

    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"  # Used when v3.1 hardcoded-test detection partially fails


class VerificationMethod(str, Enum):
    """How an assertion in validation_contract.yaml is verified."""

    UNIT_TEST = "unit_test"
    INTEGRATION_TEST = "integration_test"
    DOC_TEST = "doc_test"
    BEHAVIOR_PROBE = "behavior_probe"
    STATIC_CHECK = "static_check"
    MANUAL = "manual"


class ProjectType(str, Enum):
    """Rust project archetype, detected by project_profiler (soul.md §6.1)."""

    LIBRARY = "library"
    CLI = "cli"
    BACKEND_SERVICE = "backend_service"
    EMBEDDED = "embedded"
    WASM = "wasm"
    MIXED = "mixed"


class NetworkPolicy(str, Enum):
    """Per-task network access policy. See soul.md §7.1."""

    OPEN = "open"                # Research worker default — full internet
    CRATES_ONLY = "crates_only"  # Only crates.io / docs.rs / GitHub
    WHITELIST = "whitelist"      # Allow-list domains only
    NONE = "none"                # Sandbox, no network (Coder default)
