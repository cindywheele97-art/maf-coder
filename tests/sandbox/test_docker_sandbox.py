"""DockerSandbox tests — most are skipped if Docker isn't available locally.

The unit tests below cover only the things we can validate without a running
daemon: import behavior, `is_available()` shape, and SandboxError on use
before start. Real exec tests against a containerd daemon live in a separate
integration suite.
"""

from __future__ import annotations

import pytest

from maf_coder.agents.errors import SandboxError
from maf_coder.sandbox import DockerSandbox


class TestAvailability:
    def test_is_available_returns_bool(self) -> None:
        assert DockerSandbox.is_available() in (True, False)


class TestUseBeforeStart:
    @pytest.mark.asyncio
    async def test_exec_before_start_raises(self) -> None:
        sb = DockerSandbox(image="rust:latest")
        with pytest.raises(SandboxError):
            await sb.exec("echo hi")

    @pytest.mark.asyncio
    async def test_write_before_start_raises(self) -> None:
        sb = DockerSandbox(image="rust:latest")
        with pytest.raises(SandboxError):
            await sb.write_file("x.txt", "hi")

    @pytest.mark.asyncio
    async def test_commit_before_start_raises(self) -> None:
        sb = DockerSandbox(image="rust:latest")
        with pytest.raises(SandboxError):
            await sb.commit_snapshot("snap")

    @pytest.mark.asyncio
    async def test_health_check_before_start_returns_false(self) -> None:
        sb = DockerSandbox(image="rust:latest")
        assert await sb.health_check() is False


# -- NF1: DockerSandbox file-op paths must be shell-quoted -------------------


class _RecordingDocker(DockerSandbox):
    """DockerSandbox whose exec records the command instead of touching a daemon."""

    def __init__(self) -> None:
        super().__init__(image="x")
        self.recorded: list[str] = []

    async def exec(self, cmd, **kw):  # type: ignore[override, no-untyped-def]
        from maf_coder.agents.results import CommandResult

        self.recorded.append(cmd if isinstance(cmd, str) else " ".join(cmd))
        return CommandResult(command=str(cmd), exit_code=0, stdout="0", stderr="", duration_sec=0.0)


class TestPathQuoting:
    @pytest.mark.asyncio
    async def test_read_file_path_is_shell_quoted(self) -> None:
        """A metacharacter path is one quoted token, so the injected `touch` never
        becomes a standalone command (shlex.split would surface it otherwise)."""
        import shlex

        sb = _RecordingDocker()
        await sb.read_file("foo; touch PWNED")
        assert sb.recorded
        for cmd in sb.recorded:
            assert "touch" not in shlex.split(cmd)

    @pytest.mark.asyncio
    async def test_write_file_path_is_shell_quoted(self) -> None:
        import shlex

        sb = _RecordingDocker()
        await sb.write_file("foo; touch PWNED", "data")
        assert sb.recorded
        for cmd in sb.recorded:
            assert "touch" not in shlex.split(cmd)
