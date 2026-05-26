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
