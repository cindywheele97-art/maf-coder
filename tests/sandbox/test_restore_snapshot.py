"""restore_snapshot tests (Phase E E-recovery — Local backend round-trip).

WHY: resume/rollback are only correct if the sandbox can be put *back* to a
committed state. The contract is: commit_snapshot -> mutate -> restore_snapshot
yields exactly the committed workspace. These tests pin that round-trip so a
regression in either direction (stale files left behind, deleted files not
restored) fails loudly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.agents.errors import SandboxError
from maf_coder.sandbox import LocalShellSandbox


@pytest.fixture
async def sandbox(tmp_path: Path):
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    try:
        yield sb
    finally:
        await sb.stop()


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_restore_brings_back_committed_file(
        self, sandbox: LocalShellSandbox
    ) -> None:
        # Write a file, commit, mutate it, restore -> original content is back.
        await sandbox.write_file("src/foo.rs", "fn main() {}")
        snap = await sandbox.commit_snapshot("snap_round_trip")

        await sandbox.write_file("src/foo.rs", "MUTATED")
        mutated = await sandbox.read_file("src/foo.rs")
        assert mutated.content == "MUTATED"

        await sandbox.restore_snapshot(snap)
        restored = await sandbox.read_file("src/foo.rs")
        assert restored.content == "fn main() {}"

    @pytest.mark.asyncio
    async def test_restore_removes_files_added_after_commit(
        self, sandbox: LocalShellSandbox
    ) -> None:
        # A file created after the snapshot must NOT survive a restore —
        # otherwise resume would carry forward half-done post-checkpoint work.
        await sandbox.write_file("keep.txt", "keep")
        snap = await sandbox.commit_snapshot("snap_orphan")

        await sandbox.write_file("added_later.txt", "transient")
        await sandbox.restore_snapshot(snap)

        kept = await sandbox.read_file("keep.txt")
        assert kept.content == "keep"
        with pytest.raises(SandboxError):
            await sandbox.read_file("added_later.txt")

    @pytest.mark.asyncio
    async def test_restore_recreates_deleted_file(
        self, sandbox: LocalShellSandbox
    ) -> None:
        await sandbox.write_file("gone.txt", "data")
        snap = await sandbox.commit_snapshot("snap_delete")

        await sandbox.exec("rm gone.txt")
        await sandbox.restore_snapshot(snap)

        recovered = await sandbox.read_file("gone.txt")
        assert recovered.content == "data"

    @pytest.mark.asyncio
    async def test_commit_snapshot_tolerates_slash_in_tag(
        self, sandbox: LocalShellSandbox
    ) -> None:
        # Checkpoints use `mission/<id>/<milestone>` as the tag; the local
        # backend must sanitize '/' so the tarball lands somewhere real and
        # restore_snapshot can read it back.
        await sandbox.write_file("a.txt", "x")
        snap = await sandbox.commit_snapshot("mission/m1/m2")
        assert Path(snap).exists()
        await sandbox.write_file("a.txt", "y")
        await sandbox.restore_snapshot(snap)
        assert (await sandbox.read_file("a.txt")).content == "x"


class TestErrors:
    @pytest.mark.asyncio
    async def test_restore_missing_snapshot_raises_file_not_found(
        self, sandbox: LocalShellSandbox
    ) -> None:
        with pytest.raises(FileNotFoundError):
            await sandbox.restore_snapshot("/nonexistent/snapshot.tar.gz")

    @pytest.mark.asyncio
    async def test_restore_before_start_raises(self) -> None:
        sb = LocalShellSandbox()
        with pytest.raises(SandboxError):
            await sb.restore_snapshot("whatever.tar.gz")
