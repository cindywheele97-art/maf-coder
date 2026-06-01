"""SandboxClient — execution surface for Worker / Validator tools.

The Phase B spec (AGENT_TOOLS_SPEC §15) describes a Docker-backed sandbox. We
implement the spec interface as an abstract `SandboxClient` plus two concrete
backends:

1. `LocalShellSandbox` — subprocess-based. Runs commands inside a single
   `workspace_mount` directory on the host. Used by:
     - Every unit test
     - `--dry-run` missions where the Coder reads/writes a temp worktree
     - Local dev iterations where Docker is unavailable

   It is NOT a security sandbox — anything the Worker tools call has the same
   filesystem/network rights as the parent Python process. The permission
   layer in `agents/permissions.py` is the real defense-in-depth; this class
   only enforces a `workspace_root` boundary on path operations.

2. `DockerSandbox` — docker-py backed. Spawns a long-lived container with
   `workspace_mount` mounted at `/workspace`, runs commands via `docker exec`.
   Skipped at runtime if `docker` is not installed or the daemon is down.

Both backends honor the same `SandboxClient` interface:

    async def start(*, workspace_mount, volumes) -> None
    async def stop(*, preserve_volumes=True) -> None
    async def exec(cmd, *, cwd, timeout_sec, capture_output, stdin) -> CommandResult
    async def write_file(container_path, content) -> None
    async def read_file(container_path, max_bytes) -> FileContent
    async def commit_snapshot(image_tag) -> str
    async def health_check() -> bool

The interface deliberately models "container_path" as a relative path under
`/workspace` even for the local backend, so tools written against the
interface don't need to know which backend they're talking to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Any

from ..agents.errors import SandboxError
from ..agents.results import CommandResult, FileContent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WORKSPACE_PREFIX = "/workspace"
DEFAULT_TRUNCATE_BYTES = 50_000  # per stdout / stderr stream
DEFAULT_MAX_READ_BYTES = 1_000_000


def _truncate(buf: str, limit: int) -> tuple[str, bool]:
    if len(buf) <= limit:
        return buf, False
    head = buf[: limit // 2]
    tail = buf[-limit // 2 :]
    return (
        head + f"\n... [truncated {len(buf) - limit} bytes] ...\n" + tail,
        True,
    )


def _resolve_container_path(container_path: str) -> PurePosixPath:
    """Normalize a container_path argument to a POSIX path under /workspace.

    Accepts:
      - "src/foo.rs"           -> /workspace/src/foo.rs
      - "/workspace/src/foo.rs" -> /workspace/src/foo.rs

    Rejects:
      - Any path that resolves outside /workspace
      - Paths containing '..' segments
    """
    raw = container_path.replace("\\", "/")
    if raw.startswith("/"):
        path = PurePosixPath(raw)
    else:
        path = PurePosixPath(DEFAULT_WORKSPACE_PREFIX) / raw
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            raise SandboxError(f"path traversal not allowed: {container_path}")
        if part in ("", "."):
            continue
        parts.append(part)
    normalized = PurePosixPath("/" + "/".join(parts))
    ws_root = PurePosixPath(DEFAULT_WORKSPACE_PREFIX)
    if not (normalized == ws_root or ws_root in normalized.parents):
        raise SandboxError(
            f"container_path {container_path!r} resolves outside {DEFAULT_WORKSPACE_PREFIX}"
        )
    return normalized


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class SandboxClient(ABC):
    """Common interface implemented by all sandbox backends."""

    backend_name: str = "abstract"

    @abstractmethod
    async def start(
        self,
        *,
        workspace_mount: Path,
        volumes: dict[str, str] | None = None,
    ) -> None:
        """Start (or attach to) the sandbox.

        `workspace_mount` is the host directory that maps to `/workspace`
        inside the sandbox. `volumes` is a name->container-path mapping for
        cache volumes (cargo-cache, target-cache, sccache); backends ignore
        unknown keys.
        """

    @abstractmethod
    async def stop(self, *, preserve_volumes: bool = True) -> None:
        """Stop the sandbox. Volumes persist unless explicitly removed."""

    @abstractmethod
    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str = DEFAULT_WORKSPACE_PREFIX,
        timeout_sec: int = 60,
        capture_output: bool = True,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute a command. Non-zero exit codes are returned, not raised."""

    @abstractmethod
    async def write_file(self, container_path: str, content: str) -> None:
        """Write `content` to a file in the sandbox (atomic via tmp+rename)."""

    @abstractmethod
    async def read_file(
        self, container_path: str, max_bytes: int = DEFAULT_MAX_READ_BYTES
    ) -> FileContent:
        """Read a file from the sandbox. Content truncated to `max_bytes`."""

    @abstractmethod
    async def commit_snapshot(self, image_tag: str) -> str:
        """Snapshot the sandbox state. Returns a backend-specific snapshot id.

        For Docker: image id of `docker commit`. For local: path to a tarball
        archive of the workspace.
        """

    @abstractmethod
    async def restore_snapshot(self, snapshot_id: str) -> None:
        """Restore the sandbox to a previously committed snapshot.

        Inverse of :meth:`commit_snapshot`. `snapshot_id` is the backend
        identifier that ``commit_snapshot`` returned (Local: tarball path;
        Docker: committed image id/tag). The sandbox must be started.

        Raises FileNotFoundError if the snapshot does not exist, and
        SandboxError for backend-level failures.
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True iff the sandbox can execute commands right now."""

    # -- Helpers shared by backends --------------------------------------

    def _resolve(self, container_path: str) -> PurePosixPath:
        return _resolve_container_path(container_path)


# ---------------------------------------------------------------------------
# LocalShellSandbox
# ---------------------------------------------------------------------------


class LocalShellSandbox(SandboxClient):
    """Subprocess-based sandbox: maps `/workspace/<path>` to `<workspace_root>/<path>`.

    Commands execute on the host as the parent process. Use this for tests,
    dry-runs, and Docker-less dev. NOT a real security boundary.
    """

    backend_name = "local_shell"

    def __init__(self) -> None:
        self.workspace_root: Path | None = None
        self._started = False

    async def start(
        self,
        *,
        workspace_mount: Path,
        volumes: dict[str, str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_mount).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._started = True
        logger.info("LocalShellSandbox started at %s", self.workspace_root)

    async def stop(self, *, preserve_volumes: bool = True) -> None:
        self._started = False
        # Files persist on disk in workspace_root; no container to stop.

    def _host_path(self, container_path: str) -> Path:
        if self.workspace_root is None:
            raise SandboxError("LocalShellSandbox not started")
        normalized = self._resolve(container_path)
        rel = str(normalized).removeprefix(DEFAULT_WORKSPACE_PREFIX).lstrip("/")
        return self.workspace_root / rel

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str = DEFAULT_WORKSPACE_PREFIX,
        timeout_sec: int = 60,
        capture_output: bool = True,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if not self._started or self.workspace_root is None:
            raise SandboxError("LocalShellSandbox not started")

        cwd_host = self._host_path(cwd)
        cwd_host.mkdir(parents=True, exist_ok=True)

        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        full_env = {**os.environ, **(env or {})}

        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.PIPE if capture_output else None,
                stderr=asyncio.subprocess.PIPE if capture_output else None,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                cwd=str(cwd_host),
                env=full_env,
            )
        except (FileNotFoundError, OSError) as e:
            raise SandboxError(f"failed to spawn command: {e}") from e

        try:
            stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_bytes), timeout=timeout_sec
            )
            exit_code = proc.returncode if proc.returncode is not None else -1
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.communicate()
            duration = time.monotonic() - t0
            return CommandResult(
                command=cmd_str,
                exit_code=124,  # standard "timeout" exit code
                stdout="",
                stderr=f"command timed out after {timeout_sec}s",
                duration_sec=duration,
            )

        duration = time.monotonic() - t0
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        stdout_trunc, stdout_was_trunc = _truncate(stdout, DEFAULT_TRUNCATE_BYTES)
        stderr_trunc, stderr_was_trunc = _truncate(stderr, DEFAULT_TRUNCATE_BYTES)

        return CommandResult(
            command=cmd_str,
            exit_code=exit_code,
            stdout=stdout_trunc,
            stderr=stderr_trunc,
            duration_sec=duration,
            truncated_stdout=stdout_was_trunc,
            truncated_stderr=stderr_was_trunc,
        )

    async def write_file(self, container_path: str, content: str) -> None:
        host = self._host_path(container_path)
        host.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write
        fd, tmp = tempfile.mkstemp(prefix=f".{host.name}.", suffix=".tmp", dir=str(host.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, host)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()
            raise

    async def read_file(
        self, container_path: str, max_bytes: int = DEFAULT_MAX_READ_BYTES
    ) -> FileContent:
        host = self._host_path(container_path)
        if not host.exists():
            raise SandboxError(f"file not found: {container_path}")
        data = host.read_bytes()
        size = len(data)
        if size > max_bytes:
            data = data[:max_bytes]
            truncated = True
        else:
            truncated = False
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        return FileContent(
            path=container_path,
            content=text,
            size_bytes=size,
            truncated=truncated,
        )

    async def commit_snapshot(self, image_tag: str) -> str:
        """For local backend, snapshot = tarball of the workspace root.

        The returned id is the absolute path to the tarball. `image_tag` may
        contain '/' (checkpoints use `mission/<id>/<milestone>`); we sanitize
        it to a flat filename so the archive lands beside the workspace root
        rather than in non-existent nested dirs.
        """
        if self.workspace_root is None:
            raise SandboxError("LocalShellSandbox not started")
        safe_tag = image_tag.replace("/", "__")
        base = self.workspace_root.parent / safe_tag
        # `shutil.make_archive` is sync; offload to a thread.
        loop = asyncio.get_event_loop()
        archive = await loop.run_in_executor(
            None,
            lambda: shutil.make_archive(str(base), "gztar", str(self.workspace_root)),
        )
        return str(archive)

    async def restore_snapshot(self, snapshot_id: str) -> None:
        """Restore the workspace from a tarball produced by commit_snapshot.

        Round-trips commit_snapshot: clears the current workspace contents and
        unpacks the archive back into workspace_root. `snapshot_id` is the
        tarball path returned by commit_snapshot.
        """
        if self.workspace_root is None:
            raise SandboxError("LocalShellSandbox not started")
        tarball = Path(snapshot_id)
        if not tarball.exists():
            raise FileNotFoundError(f"snapshot not found: {snapshot_id}")
        root = self.workspace_root

        def _restore() -> None:
            # Clear existing workspace contents (keep the root dir itself).
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    with contextlib.suppress(FileNotFoundError):
                        child.unlink()
            shutil.unpack_archive(str(tarball), str(root), "gztar")

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _restore)
        except (shutil.ReadError, OSError) as e:
            raise SandboxError(f"restore_snapshot failed: {e}") from e

    async def health_check(self) -> bool:
        if not self._started:
            return False
        try:
            # `true` is a shell builtin in /bin/sh; reliable even when /usr/bin
            # is not on PATH or the binary path is restricted by an outer
            # sandbox.
            result = await self.exec("true", timeout_sec=5)
        except SandboxError:
            return False
        return result.exit_code == 0


# ---------------------------------------------------------------------------
# DockerSandbox
# ---------------------------------------------------------------------------


class DockerSandbox(SandboxClient):
    """Docker-backed sandbox. Spawns a container, exec's commands via docker.

    Skipped silently at instantiation when `docker` is not importable. Callers
    should use `is_available()` to fall back to LocalShellSandbox.
    """

    backend_name = "docker"

    def __init__(self, image: str, container_name: str | None = None) -> None:
        self.image = image
        self.container_name = container_name or f"maf-coder-{uuid.uuid4().hex[:8]}"
        self._client: Any = None
        self._container: Any = None
        self._started = False
        self._volume_binds: dict[str, dict[str, str]] = {}

    # -- Availability -----------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """True iff docker-py is importable and the daemon is reachable."""
        try:
            import docker
        except ImportError:
            return False
        try:
            c = docker.from_env()
            c.ping()
            return True
        except Exception:
            return False

    # -- Lifecycle --------------------------------------------------------

    async def start(
        self,
        *,
        workspace_mount: Path,
        volumes: dict[str, str] | None = None,
    ) -> None:
        try:
            import docker
        except ImportError as e:
            raise SandboxError("docker-py not installed. Install with `pip install docker`.") from e

        self._client = docker.from_env()
        try:
            self._client.ping()
        except Exception as e:
            raise SandboxError(f"Docker daemon unreachable: {e}") from e

        mount_path = Path(workspace_mount).resolve()
        mount_path.mkdir(parents=True, exist_ok=True)
        volume_binds: dict[str, dict[str, str]] = {
            str(mount_path): {"bind": DEFAULT_WORKSPACE_PREFIX, "mode": "rw"},
        }
        for name, container_dest in (volumes or {}).items():
            volume_binds[name] = {"bind": container_dest, "mode": "rw"}
        self._volume_binds = volume_binds

        loop = asyncio.get_event_loop()
        self._container = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                self.image,
                name=self.container_name,
                command="sleep infinity",
                detach=True,
                tty=False,
                volumes=volume_binds,
                working_dir=DEFAULT_WORKSPACE_PREFIX,
                auto_remove=False,
                network_mode="none",  # sandbox: no network unless tool opts in
            ),
        )
        self._started = True
        logger.info("DockerSandbox started container=%s image=%s", self.container_name, self.image)

    async def stop(self, *, preserve_volumes: bool = True) -> None:
        if not self._started or self._container is None:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._container.stop)
        except Exception:
            logger.exception("DockerSandbox stop() failed; continuing")
        try:
            await loop.run_in_executor(None, lambda: self._container.remove(v=not preserve_volumes))
        except Exception:
            logger.exception("DockerSandbox remove() failed; continuing")
        self._started = False
        self._container = None

    # -- exec / IO --------------------------------------------------------

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str = DEFAULT_WORKSPACE_PREFIX,
        timeout_sec: int = 60,
        capture_output: bool = True,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        if not self._started or self._container is None:
            raise SandboxError("DockerSandbox not started")
        cmd_list = ["bash", "-c", cmd] if isinstance(cmd, str) else cmd
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()
        try:
            res = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._container.exec_run(
                        cmd_list,
                        workdir=cwd,
                        environment=env or {},
                        stdin=stdin is not None,
                        demux=True,
                    ),
                ),
                timeout=timeout_sec,
            )
        except TimeoutError:
            duration = time.monotonic() - t0
            return CommandResult(
                command=cmd_str,
                exit_code=124,
                stdout="",
                stderr=f"command timed out after {timeout_sec}s",
                duration_sec=duration,
            )

        duration = time.monotonic() - t0
        exit_code = int(getattr(res, "exit_code", -1) or 0)
        output = getattr(res, "output", (b"", b""))
        if isinstance(output, tuple) and len(output) == 2:
            stdout_b, stderr_b = output
        else:
            stdout_b, stderr_b = output, b""
        stdout = (stdout_b or b"").decode("utf-8", errors="replace") if stdout_b else ""
        stderr = (stderr_b or b"").decode("utf-8", errors="replace") if stderr_b else ""
        stdout_trunc, stdout_was_trunc = _truncate(stdout, DEFAULT_TRUNCATE_BYTES)
        stderr_trunc, stderr_was_trunc = _truncate(stderr, DEFAULT_TRUNCATE_BYTES)
        return CommandResult(
            command=cmd_str,
            exit_code=exit_code,
            stdout=stdout_trunc,
            stderr=stderr_trunc,
            duration_sec=duration,
            truncated_stdout=stdout_was_trunc,
            truncated_stderr=stderr_was_trunc,
        )

    async def write_file(self, container_path: str, content: str) -> None:
        norm = self._resolve(container_path)
        # Use a heredoc-style write through `tee` to keep this self-contained.
        # For larger files / binary, dockerpy's `put_archive` would be faster.
        result = await self.exec(f"mkdir -p {os.path.dirname(str(norm))}", timeout_sec=10)
        if result.exit_code != 0:
            raise SandboxError(f"mkdir failed: {result.stderr}")
        tmp = f"{norm}.tmp.{uuid.uuid4().hex[:8]}"
        cmd = f"cat > {tmp} && mv {tmp} {norm}"
        result = await self.exec(cmd, stdin=content, timeout_sec=30)
        if result.exit_code != 0:
            raise SandboxError(f"write_file failed: {result.stderr}")

    async def read_file(
        self, container_path: str, max_bytes: int = DEFAULT_MAX_READ_BYTES
    ) -> FileContent:
        norm = self._resolve(container_path)
        # Discover size first to set the truncation flag correctly.
        size_res = await self.exec(f"stat -c %s {norm} || stat -f %z {norm}", timeout_sec=10)
        try:
            size = int(size_res.stdout.strip())
        except (ValueError, AttributeError):
            size = 0
        head_res = await self.exec(f"head -c {max_bytes} {norm}", timeout_sec=30)
        if head_res.exit_code != 0:
            raise SandboxError(f"read_file failed: {head_res.stderr}")
        truncated = size > max_bytes
        return FileContent(
            path=container_path,
            content=head_res.stdout,
            size_bytes=size,
            truncated=truncated,
        )

    async def commit_snapshot(self, image_tag: str) -> str:
        if not self._started or self._container is None:
            raise SandboxError("DockerSandbox not started")
        loop = asyncio.get_event_loop()
        image = await loop.run_in_executor(
            None, lambda: self._container.commit(repository=image_tag)
        )
        return str(getattr(image, "id", image))

    async def restore_snapshot(self, snapshot_id: str) -> None:
        """Recreate the container from a committed image (minimal restore).

        Stops/removes the current container and runs a fresh one from
        `snapshot_id` (the committed image id/tag), reusing the original
        volume binds captured at start(). The host workspace mount is shared,
        so file state on the mount is whatever the host has; this restores the
        container's own (non-mounted) filesystem layers.
        """
        if self._client is None:
            raise SandboxError("DockerSandbox not started")
        loop = asyncio.get_event_loop()

        def _exists() -> bool:
            try:
                self._client.images.get(snapshot_id)
                return True
            except Exception:
                return False

        if not await loop.run_in_executor(None, _exists):
            raise FileNotFoundError(f"snapshot image not found: {snapshot_id}")

        # Tear down the current container before recreating from the snapshot.
        if self._container is not None:
            try:
                await loop.run_in_executor(None, self._container.stop)
                await loop.run_in_executor(None, lambda: self._container.remove(v=False))
            except Exception:
                logger.exception("DockerSandbox.restore_snapshot: teardown failed; continuing")
            self._container = None

        self._container = await loop.run_in_executor(
            None,
            lambda: self._client.containers.run(
                snapshot_id,
                name=self.container_name,
                command="sleep infinity",
                detach=True,
                tty=False,
                volumes=self._volume_binds,
                working_dir=DEFAULT_WORKSPACE_PREFIX,
                auto_remove=False,
                network_mode="none",
            ),
        )
        self._started = True
        logger.info("DockerSandbox restored from snapshot=%s", snapshot_id)

    async def health_check(self) -> bool:
        if not self._started:
            return False
        try:
            res = await self.exec("true", timeout_sec=5)
            return res.exit_code == 0
        except SandboxError:
            return False


__all__ = ["DockerSandbox", "LocalShellSandbox", "SandboxClient"]
