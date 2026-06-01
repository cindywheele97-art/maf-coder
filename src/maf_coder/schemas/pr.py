"""Pull-request workflow schemas (Build Plan §Phase F · F5).

The PR workflow is the mission-end action that opens a PR/MR from a finished
mission: it composes a description from the mission's artifacts, links back to
the artifact directory, and runs a final gitleaks scan as a pre-PR gate.

Two models:

- ``PullRequestSpec``   — the composed request handed to the gh/glab wrapper:
  title + body + branch refs + provider + repo path + the artifact links the
  body references.
- ``PullRequestResult`` — the wrapper's outcome: either the created PR URL, or
  a refusal (e.g. the gitleaks gate found secrets) with the surfaced finding.

Both use ``extra="forbid"`` to match every other schema in this package — an
unexpected key is a bug, not silently-dropped data.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class VcsProvider(str, Enum):
    """Supported VCS hosting providers and their CLI wrapper.

    ``gh`` → GitHub (``gh pr create``); ``glab`` → GitLab (``glab mr create``).
    Default across the workflow is ``gh``.
    """

    GH = "gh"
    GLAB = "glab"


class PullRequestSpec(BaseModel):
    """Composed request for opening a PR/MR.

    Stored implicitly (never written to disk by this layer) and passed straight
    to the gh/glab wrapper. ``artifact_links`` are mission-relative references
    (e.g. ``verdicts/t5.review.json``) that the body already embeds; they are
    kept structured so callers/tests can assert linkage independent of the
    rendered markdown.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    mission_id: str
    title: str
    body: str
    head_branch: str = Field(description="Source branch the PR/MR is opened from.")
    base_branch: str = Field(default="main", description="Target branch to merge into.")
    provider: VcsProvider = Field(default=VcsProvider.GH)
    draft: bool = Field(default=False, description="Open as a draft PR/MR.")
    repo_path: str = Field(
        description="Path to the git repo (the gh/glab CWD inside the sandbox)."
    )
    artifact_links: list[str] = Field(
        default_factory=list,
        description="Mission-relative artifact paths the body links to.",
    )


class PullRequestResult(BaseModel):
    """Outcome of a create-PR attempt.

    On success: ``created=True`` and ``url`` is the parsed PR/MR URL. On the
    gitleaks-dirty path: ``created=False``, ``refused=True``, and
    ``refusal_reason`` + ``gitleaks_findings`` surface what blocked it. The two
    failure surfaces (gate refusal vs. CLI error) are distinguished by
    ``refused``: a refusal is a deliberate gate decision, a non-refusal failure
    is an infrastructure/CLI problem (``exit_code`` / ``stderr``).
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    mission_id: str
    provider: VcsProvider
    created: bool
    url: str | None = None
    refused: bool = Field(
        default=False,
        description="True iff a pre-PR gate (e.g. gitleaks) deliberately blocked creation.",
    )
    refusal_reason: str | None = None
    gitleaks_findings: list[dict[str, object]] = Field(
        default_factory=list,
        description="Raw gitleaks findings surfaced when the secret gate refuses the PR.",
    )
    command: str | None = Field(
        default=None, description="The composed gh/glab command that was run (for audit)."
    )
    exit_code: int | None = None
    stderr: str | None = None
    artifact_links: list[str] = Field(default_factory=list)


__all__ = ["PullRequestResult", "PullRequestSpec", "VcsProvider"]
