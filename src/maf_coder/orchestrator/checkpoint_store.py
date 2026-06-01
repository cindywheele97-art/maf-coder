"""CheckpointStore — read-side + GC for mission checkpoints (Phase E E-recovery).

`make_create_checkpoint` (agents/tools/orchestrator_tools.py) is the *writer*:
per milestone it writes a git tag, a sandbox snapshot, an artifact archive at
``checkpoints/<milestone>/`` and a ``Checkpoint`` JSON. Resume / rollback are
the inverse and need to *read* those checkpoints and pick a target, plus garbage
-collect orphaned snapshots. That read/GC logic lives here so MissionDriver and
the CLI share one implementation.

Layout written by the checkpoint writer (via ArtifactStore.save_checkpoint):

    missions/<id>/checkpoints/<milestone_id>/checkpoint.json
    missions/<id>/checkpoints/<milestone_id>/MANIFEST.txt
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..blackboard import ArtifactStore
from ..schemas import Checkpoint, MissionState

logger = logging.getLogger(__name__)

_CHECKPOINTS_DIR = "checkpoints"


class CheckpointStore:
    """Read-side view over a mission's on-disk checkpoints + snapshot GC."""

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    # -- Discovery --------------------------------------------------------

    def list_milestones(self) -> list[str]:
        """Return milestone ids that have a checkpoint.json on disk, sorted.

        Sorted naturally so ``m1 < m2 < m10`` is *not* guaranteed lexically;
        callers that need ordering by completion use ``MissionState`` instead.
        Empty list if the checkpoints/ directory is absent.
        """
        out: list[str] = []
        for entry in self.store.list_dir(_CHECKPOINTS_DIR):
            if not entry.is_dir():
                continue
            if (entry / "checkpoint.json").exists():
                out.append(entry.name)
        return sorted(out)

    def load(self, milestone_id: str) -> Checkpoint:
        """Load one checkpoint. Raises FileNotFoundError if it does not exist."""
        return self.store.load_checkpoint(milestone_id)

    def list_checkpoints(self) -> list[Checkpoint]:
        """Load every checkpoint, ordered by ``created_at`` (oldest first)."""
        cps = [self.load(m) for m in self.list_milestones()]
        return sorted(cps, key=lambda c: c.created_at)

    # -- Target selection -------------------------------------------------

    def resolve_target(
        self,
        state: MissionState,
        from_milestone: str | None = None,
    ) -> Checkpoint:
        """Pick the checkpoint to resume/rollback to.

        - If ``from_milestone`` is given, that exact checkpoint is returned
          (FileNotFoundError if it is missing — a clear error, not a crash).
        - Otherwise the latest checkpoint is chosen: the last entry in
          ``state.completed_milestones`` that actually has a checkpoint on
          disk, falling back to the most-recently-created checkpoint.

        Raises FileNotFoundError if no checkpoint can be resolved.
        """
        if from_milestone is not None:
            return self.load(from_milestone)

        available = set(self.list_milestones())
        if not available:
            raise FileNotFoundError(
                f"mission {self.store.mission_id}: no checkpoints found to resume from"
            )

        for milestone in reversed(state.completed_milestones):
            if milestone in available:
                return self.load(milestone)

        # No overlap with completed_milestones — fall back to newest on disk.
        return self.list_checkpoints()[-1]

    # -- Garbage collection ----------------------------------------------

    def gc_snapshots(
        self,
        state: MissionState,
        *,
        keep_last: int | None = None,
        sandbox_root: Path | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Delete snapshot tarballs for checkpoints outside the retained set.

        Retained set = every checkpoint whose milestone is in
        ``state.completed_milestones`` PLUS, when ``keep_last`` is given, the
        ``keep_last`` most recent checkpoints (by ``created_at``). A checkpoint
        on disk that is neither completed nor in the keep-last window is an
        *orphan* (e.g. a checkpoint written for a milestone that was later
        rolled back / truncated out of ``completed_milestones``) and its
        snapshot is collected.

        ``keep_last=None`` (default) means the keep-last window is empty:
        retention is exactly ``completed_milestones``.

        Only snapshots living under ``sandbox_root`` (default: the workspace
        parent, where LocalShellSandbox writes its tarballs) are touched —
        we never delete arbitrary host paths.

        Returns the list of snapshot ids that were (or, under ``dry_run``,
        would be) deleted.
        """
        checkpoints = self.list_checkpoints()
        retained_milestones = set(state.completed_milestones)
        if keep_last is not None and keep_last > 0:
            for cp in checkpoints[-keep_last:]:
                retained_milestones.add(cp.milestone_id)

        retained_snapshots = {
            cp.sandbox_snapshot_id
            for cp in checkpoints
            if cp.milestone_id in retained_milestones
        }

        deleted: list[str] = []
        for cp in checkpoints:
            if cp.milestone_id in retained_milestones:
                continue
            snap = cp.sandbox_snapshot_id
            if not snap or snap in ("unknown", "") or snap in retained_snapshots:
                continue
            snap_path = Path(snap)
            if sandbox_root is not None:
                root = sandbox_root.resolve()
                try:
                    snap_path.resolve().relative_to(root)
                except ValueError:
                    logger.warning("gc_snapshots: skipping out-of-root snapshot %s", snap)
                    continue
            if not snap_path.exists():
                continue
            deleted.append(snap)
            if not dry_run:
                try:
                    snap_path.unlink()
                except OSError as e:
                    logger.warning("gc_snapshots: failed to delete %s: %r", snap, e)
        return deleted


__all__ = ["CheckpointStore"]
