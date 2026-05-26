"""LocalShellSandbox tests (AGENT_TOOLS_SPEC §15 — local backend)."""
from __future__ import annotations

import asyncio
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


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_health_check_when_running(self, sandbox: LocalShellSandbox) -> None:
        assert await sandbox.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_when_stopped(self, tmp_path: Path) -> None:
        sb = LocalShellSandbox()
        assert await sb.health_check() is False

    @pytest.mark.asyncio
    async def test_exec_before_start_raises(self) -> None:
        sb = LocalShellSandbox()
        with pytest.raises(SandboxError):
            await sb.exec("echo hi")


class TestExec:
    @pytest.mark.asyncio
    async def test_basic_echo(self, sandbox: LocalShellSandbox) -> None:
        r = await sandbox.exec("echo hello")
        assert r.exit_code == 0
        assert "hello" in r.stdout

    @pytest.mark.asyncio
    async def test_non_zero_exit_not_raised(self, sandbox: LocalShellSandbox) -> None:
        r = await sandbox.exec("exit 7")
        assert r.exit_code == 7

    @pytest.mark.asyncio
    async def test_stderr_captured(self, sandbox: LocalShellSandbox) -> None:
        r = await sandbox.exec("echo error >&2")
        assert "error" in r.stderr

    @pytest.mark.asyncio
    async def test_cwd_relative(self, sandbox: LocalShellSandbox) -> None:
        # mkdir + pwd inside a subdir
        await sandbox.exec("mkdir -p sub")
        r = await sandbox.exec("pwd", cwd="/workspace/sub")
        assert r.stdout.strip().endswith("/sub")

    @pytest.mark.asyncio
    async def test_timeout(self, sandbox: LocalShellSandbox) -> None:
        r = await sandbox.exec("sleep 5", timeout_sec=1)
        assert r.exit_code == 124
        assert "timed out" in r.stderr

    @pytest.mark.asyncio
    async def test_stdin(self, sandbox: LocalShellSandbox) -> None:
        r = await sandbox.exec("cat", stdin="hello from stdin")
        assert "hello from stdin" in r.stdout

    @pytest.mark.asyncio
    async def test_env(self, sandbox: LocalShellSandbox) -> None:
        r = await sandbox.exec("echo $MAF_TEST_VAR", env={"MAF_TEST_VAR": "marker"})
        assert "marker" in r.stdout


class TestFileIO:
    @pytest.mark.asyncio
    async def test_write_and_read(self, sandbox: LocalShellSandbox) -> None:
        await sandbox.write_file("src/foo.rs", "fn main() {}\n")
        fc = await sandbox.read_file("src/foo.rs")
        assert fc.content == "fn main() {}\n"
        assert fc.size_bytes == len(b"fn main() {}\n")
        assert fc.truncated is False

    @pytest.mark.asyncio
    async def test_write_creates_subdirs(self, sandbox: LocalShellSandbox) -> None:
        await sandbox.write_file("a/b/c/x.txt", "hi")
        r = await sandbox.exec("cat a/b/c/x.txt")
        assert "hi" in r.stdout

    @pytest.mark.asyncio
    async def test_read_truncates(self, sandbox: LocalShellSandbox) -> None:
        big = "x" * 5000
        await sandbox.write_file("big.txt", big)
        fc = await sandbox.read_file("big.txt", max_bytes=100)
        assert fc.truncated is True
        assert fc.size_bytes == 5000
        assert len(fc.content) == 100

    @pytest.mark.asyncio
    async def test_read_missing_raises(self, sandbox: LocalShellSandbox) -> None:
        with pytest.raises(SandboxError):
            await sandbox.read_file("nope.txt")

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, sandbox: LocalShellSandbox) -> None:
        with pytest.raises(SandboxError):
            await sandbox.write_file("../escape.txt", "evil")
        with pytest.raises(SandboxError):
            await sandbox.read_file("../escape.txt")


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_commit_snapshot_creates_archive(
        self, sandbox: LocalShellSandbox, tmp_path: Path
    ) -> None:
        await sandbox.write_file("src/foo.rs", "fn main() {}")
        archive = await sandbox.commit_snapshot("snap_test")
        assert Path(archive).exists()
        assert Path(archive).stat().st_size > 0
