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


# -- M1: containers must run with resource + privilege limits ---------------


class TestHardeningLimits:
    def test_defaults_bound_memory_pids_and_strip_privileges(self) -> None:
        kw = DockerSandbox(image="rust:latest")._hardening_kwargs()
        assert kw["mem_limit"]  # memory is bounded (OOM vector)
        assert isinstance(kw["pids_limit"], int)  # fork-bomb vector
        assert kw["pids_limit"] > 0
        assert kw["cap_drop"] == ["ALL"]  # no Linux capabilities
        assert any("no-new-privileges" in opt for opt in kw["security_opt"])

    def test_nano_cpus_absent_by_default(self) -> None:
        # CPU starvation doesn't crash the host, so the cap is opt-in.
        assert "nano_cpus" not in DockerSandbox(image="x")._hardening_kwargs()

    def test_overrides_pass_through(self) -> None:
        sb = DockerSandbox(image="x", mem_limit="2g", pids_limit=256, nano_cpus=1_500_000_000)
        kw = sb._hardening_kwargs()
        assert kw["mem_limit"] == "2g"
        assert kw["pids_limit"] == 256
        assert kw["nano_cpus"] == 1_500_000_000

    def test_network_isolation_unchanged(self) -> None:
        # M1 must not weaken the existing egress containment.
        assert DockerSandbox(image="x")._run_base_kwargs()["network_mode"] == "none"

    def test_run_base_includes_hardening(self) -> None:
        # The kwargs both start() and restore_snapshot() pass to containers.run
        # must carry the hardening limits, not just the bare container config.
        base = DockerSandbox(image="x")._run_base_kwargs()
        for key in ("mem_limit", "pids_limit", "cap_drop", "security_opt"):
            assert key in base


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
