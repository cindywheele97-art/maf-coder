"""External integrations (Build Plan §Phase F).

Currently houses the VCS / PR workflow (F5): thin async wrappers around the
``gh`` / ``glab`` CLIs invoked through the sandbox, PR-description generation
from mission artifacts, and the gitleaks pre-PR secret gate.
"""

from __future__ import annotations

from .vcs import (
    GitleaksGateError,
    build_artifact_links,
    create_pull_request,
    render_pr_body,
    run_gitleaks_gate,
)

__all__ = [
    "GitleaksGateError",
    "build_artifact_links",
    "create_pull_request",
    "render_pr_body",
    "run_gitleaks_gate",
]
