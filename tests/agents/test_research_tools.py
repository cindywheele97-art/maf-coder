"""Research Worker tool factory tests (AGENT_TOOLS_SPEC §9).

`fetch_url` uses an injected fetcher so tests never touch the network.
`grep` / `glob` / `cargo_*` go through a real LocalShellSandbox.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request

import pytest

from maf_coder.agents.base import TaskContext
from maf_coder.agents.errors import (
    ExternalContentError,
    PermissionDeniedError,
    ToolError,
)
from maf_coder.agents.permissions import check_network_allowed, check_resolved_host_safe
from maf_coder.agents.tools.research_tools import (
    _make_validating_fetcher,
    _PinnedHTTPHandler,
    _PinnedHTTPSHandler,
    _safe_create_connection,
    _validating_opener,
    _ValidatingRedirectHandler,
    build_research_tools,
    make_cargo_metadata,
    make_cargo_tree,
    make_fetch_url,
    make_glob,
    make_grep,
    make_save_code_map,
    make_save_dependency_brief,
    make_save_research_note,
    make_save_workspace_overview,
)
from maf_coder.blackboard import ArtifactStore
from maf_coder.blackboard.event_log import EventKind
from maf_coder.models.router import ModelRouter
from maf_coder.sandbox import LocalShellSandbox
from maf_coder.schemas import (
    NetworkPolicy,
    Permission,
    RiskLevel,
    Role,
    Task,
    TaskBudget,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def router(tmp_path: Path) -> ModelRouter:
    cfg = tmp_path / "droid.yaml"
    cfg.write_text(
        "version: 1\n"
        "roles:\n"
        "  research_worker:\n"
        "    primary:\n"
        "      model: anthropic/x\n"
        "      temperature: 0.1\n"
        "      max_tokens: 1000\n"
        "    fallback: []\n"
    )
    return ModelRouter(cfg)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "missions", "m1")


@pytest.fixture
async def sandbox(tmp_path: Path) -> AsyncIterator[LocalShellSandbox]:
    sb = LocalShellSandbox()
    await sb.start(workspace_mount=tmp_path / "ws")
    await sb.exec("git init -q -b main", cwd="/workspace")
    await sb.exec("git config user.email t@t && git config user.name t", cwd="/workspace")
    await sb.exec("touch initial && git add -A && git commit -q -m initial", cwd="/workspace")
    try:
        yield sb
    finally:
        await sb.stop()


def _ctx(
    sandbox: LocalShellSandbox,
    store: ArtifactStore,
    router: ModelRouter,
    *,
    permission: Permission | None = None,
    task_id: str = "research-1",
    network_policy: NetworkPolicy = NetworkPolicy.OPEN,
) -> TaskContext:
    perm = permission or Permission(
        allowed_paths=["**"],
        allowed_tools=[],
        network_policy=network_policy,
    )
    task = Task(
        task_id=task_id,
        parent_milestone="m1",
        owner=Role.RESEARCH_WORKER,
        priority=RiskLevel.MEDIUM,
        risk_level=RiskLevel.LOW,
        goal="map the axum http layer",
        background="background",
        acceptance_criteria=["f1.a1"],
        required_outputs=["research_notes/axum-routing.md"],
        permission=perm,
        budget=TaskBudget(max_tokens=1000, max_runtime_sec=60),
    )
    return TaskContext(
        task=task,
        mission_id="m1",
        store=store,
        event_log=store.event_log(),
        router=router,
        sandbox=sandbox,
    )


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------


def _stub_fetcher(*, body: str, content_type: str = "text/html", status: int = 200):
    """Build a synchronous stub HTTP transport. Records call args on the closure."""
    calls: list[tuple[str, int]] = []

    def fn(url: str, timeout_sec: int) -> tuple[str, str, int, str]:
        calls.append((url, timeout_sec))
        return (url, content_type, status, body)

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


def _public_resolver(host: str) -> list[str]:
    """Hermetic DNS stub: every host resolves to a public IP (M2 check passes)."""
    return ["1.1.1.1"]


class TestFetchUrl:
    @pytest.mark.asyncio
    async def test_ok_path_sanitizes_and_logs(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        fetcher = _stub_fetcher(body="<p>safe</p><script>x()</script>")
        tool = make_fetch_url(ctx, fetcher=fetcher, resolver=_public_resolver)
        result = await tool(url="https://crates.io/crates/serde")
        assert "x()" not in result.content
        assert "safe" in result.content
        assert any("script" in a for a in result.sanitization_actions)
        # Both events logged
        kinds = {e.kind for e in ctx.event_log.iter_events()}
        assert EventKind.EGRESS_REQUEST.value in kinds
        assert EventKind.EXTERNAL_CONTENT_RECEIVED.value in kinds

    @pytest.mark.asyncio
    async def test_denied_by_network_policy_logs_blocked(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
    ) -> None:
        ctx = _ctx(sandbox, store, router, network_policy=NetworkPolicy.NONE)
        tool = make_fetch_url(ctx, fetcher=_stub_fetcher(body=""), resolver=_public_resolver)
        with pytest.raises(PermissionDeniedError):
            await tool(url="https://crates.io/")
        egress = [
            e for e in ctx.event_log.iter_events() if e.kind == EventKind.EGRESS_REQUEST.value
        ]
        assert len(egress) == 1
        assert egress[0].payload["blocked_reason"] == "permission-denied"

    @pytest.mark.asyncio
    async def test_denied_outside_crates_only_allowlist(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
    ) -> None:
        ctx = _ctx(sandbox, store, router, network_policy=NetworkPolicy.CRATES_ONLY)
        tool = make_fetch_url(ctx, fetcher=_stub_fetcher(body=""), resolver=_public_resolver)
        with pytest.raises(PermissionDeniedError):
            await tool(url="https://evil.example.com/")
        # crates.io itself is allowed
        await tool(url="https://crates.io/")

    @pytest.mark.asyncio
    async def test_whitelist_policy(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
    ) -> None:
        ctx = _ctx(sandbox, store, router, network_policy=NetworkPolicy.WHITELIST)
        tool = make_fetch_url(
            ctx,
            fetcher=_stub_fetcher(body=""),
            domain_whitelist=["example.com"],
            resolver=_public_resolver,
        )
        await tool(url="https://docs.example.com/")
        with pytest.raises(PermissionDeniedError):
            await tool(url="https://other.com/")

    @pytest.mark.asyncio
    async def test_fetch_error_wrapped(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
    ) -> None:
        def boom(url: str, timeout_sec: int) -> tuple[str, str, int, str]:
            raise ConnectionError("DNS")

        ctx = _ctx(sandbox, store, router)
        tool = make_fetch_url(ctx, fetcher=boom, resolver=_public_resolver)
        with pytest.raises(ExternalContentError):
            await tool(url="https://crates.io/")
        egress = [
            e for e in ctx.event_log.iter_events() if e.kind == EventKind.EGRESS_REQUEST.value
        ]
        assert len(egress) == 1
        assert "fetch-error" in (egress[0].payload["blocked_reason"] or "")

    @pytest.mark.asyncio
    async def test_host_resolving_to_metadata_is_denied(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
    ) -> None:
        """M2 — a policy-allowed hostname that resolves to a cloud-metadata IP
        is rejected (and logged) before any fetch happens."""
        ctx = _ctx(sandbox, store, router, network_policy=NetworkPolicy.OPEN)

        def _to_metadata(host: str) -> list[str]:
            return ["169.254.169.254"]

        tool = make_fetch_url(ctx, fetcher=_stub_fetcher(body=""), resolver=_to_metadata)
        with pytest.raises(PermissionDeniedError):
            await tool(url="https://sneaky.example.com/")
        egress = [
            e for e in ctx.event_log.iter_events() if e.kind == EventKind.EGRESS_REQUEST.value
        ]
        assert len(egress) == 1
        assert egress[0].payload["blocked_reason"] == "permission-denied"


# ---------------------------------------------------------------------------
# Redirect re-validation (H1 — SSRF allowlist must hold across redirects)
# ---------------------------------------------------------------------------


class TestRedirectValidation:
    """urllib auto-follows 3xx; without re-validation an allowed host could
    302 the fetch onto a denied host or an SSRF target. Each hop must re-run
    the SAME network gate as the initial URL.
    """

    @staticmethod
    def _redirect(handler: _ValidatingRedirectHandler, from_url: str, to_url: str) -> Request | None:
        # Mirror urllib's HTTPRedirectHandler.redirect_request(req, fp, code, msg, headers, newurl)
        req = Request(from_url)
        return handler.redirect_request(req, None, 302, "Found", http.client.HTTPMessage(), to_url)

    def test_opener_installs_the_validating_handler(self) -> None:
        opener = _validating_opener(lambda _u: None)
        assert any(isinstance(h, _ValidatingRedirectHandler) for h in opener.handlers)

    def test_hop_to_ssrf_target_is_blocked(self) -> None:
        perm = Permission(allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.OPEN)
        handler = _ValidatingRedirectHandler(lambda u: check_network_allowed(perm, u, None))
        # OPEN policy still blocks the cloud-metadata / RFC-1918 denylist.
        with pytest.raises(PermissionDeniedError):
            self._redirect(handler, "https://crates.io/", "http://169.254.169.254/latest/meta-data/")

    def test_hop_outside_allowlist_is_blocked(self) -> None:
        perm = Permission(
            allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.CRATES_ONLY
        )
        handler = _ValidatingRedirectHandler(lambda u: check_network_allowed(perm, u, None))
        with pytest.raises(PermissionDeniedError):
            self._redirect(handler, "https://crates.io/", "https://evil.example.com/")

    def test_permitted_hop_passes_through(self) -> None:
        perm = Permission(
            allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.CRATES_ONLY
        )
        handler = _ValidatingRedirectHandler(lambda u: check_network_allowed(perm, u, None))
        new = self._redirect(handler, "https://crates.io/", "https://static.crates.io/x")
        assert new is not None
        assert new.full_url == "https://static.crates.io/x"

    def test_hop_resolving_to_private_ip_is_blocked(self) -> None:
        """M2 + H1 — a redirect to an allowlist-passing host that resolves to a
        private IP is still blocked, because the hop runs the resolved-host gate."""
        perm = Permission(allowed_paths=["**"], allowed_tools=[], network_policy=NetworkPolicy.OPEN)

        def validate(u: str) -> None:
            check_network_allowed(perm, u, None)
            check_resolved_host_safe(
                (urlparse(u).hostname or "").lower(), resolver=lambda _h: ["10.0.0.5"]
            )

        handler = _ValidatingRedirectHandler(validate)
        with pytest.raises(PermissionDeniedError):
            self._redirect(handler, "https://crates.io/", "https://internal.example.com/")

    def test_default_fetcher_validates_each_hop(self) -> None:
        """The validating fetcher must route every redirect through the validate
        callback before opening it (network-free — we stub the opener)."""
        seen: list[str] = []

        class _FakeResp:
            status = 200
            headers = http.client.HTTPMessage()

            def read(self) -> bytes:
                return b"ok"

            def geturl(self) -> str:
                return "https://static.crates.io/final"

            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *a: object) -> None:
                return None

        class _FakeOpener:
            def __init__(self, validate):  # type: ignore[no-untyped-def]
                self._validate = validate

            def open(self, req: Request, timeout: int) -> _FakeResp:
                # Simulate one redirect hop the way urllib would: validate first.
                self._validate("https://static.crates.io/final")
                return _FakeResp()

        def opener_factory(validate):  # type: ignore[no-untyped-def]
            return _FakeOpener(validate)

        fetcher = _make_validating_fetcher(
            lambda u: seen.append(u), opener_factory=opener_factory
        )
        _final_url, _ct, status, body = fetcher("https://crates.io/", 10)
        assert status == 200
        assert body == "ok"
        assert "https://static.crates.io/final" in seen


# ---------------------------------------------------------------------------
# Pin-and-connect transport (M2 TOCTOU — resolve once, connect to that IP)
# ---------------------------------------------------------------------------


class _FakeSocket:
    def __init__(self, family: int, socktype: int, proto: int) -> None:
        self.family = family
        self.connected: tuple[str, int] | None = None
        self.closed = False

    def settimeout(self, t: object) -> None:
        self.timeout = t

    def bind(self, addr: tuple[str, int]) -> None:
        self.bound = addr

    def connect(self, addr: tuple[str, int]) -> None:
        self.connected = addr

    def close(self) -> None:
        self.closed = True


def _addrinfo(*ips: str, port: int = 443) -> list[tuple]:
    import socket as _s

    return [(_s.AF_INET, _s.SOCK_STREAM, 0, "", (ip, port)) for ip in ips]


class TestPinAndConnect:
    """M2 TOCTOU — the transport resolves once and connects to that exact,
    validated IP; a rebind to a private IP can never be reached, and a socket is
    never even opened when a resolved address is blocked."""

    def test_blocked_resolved_ip_refused_before_any_socket(self) -> None:
        made: list[_FakeSocket] = []

        def factory(family: int, socktype: int, proto: int) -> _FakeSocket:
            s = _FakeSocket(family, socktype, proto)
            made.append(s)
            return s

        with pytest.raises(PermissionDeniedError):
            _safe_create_connection(
                ("evil.example.com", 443),
                _getaddrinfo=lambda *a, **k: _addrinfo("169.254.169.254"),
                _socket_factory=factory,
            )
        assert made == []  # validation happens before any socket is created

    def test_mixed_records_refused(self) -> None:
        # One public, one private A record → refuse (round-robin rebinding).
        with pytest.raises(PermissionDeniedError):
            _safe_create_connection(
                ("mixed.example.com", 443),
                _getaddrinfo=lambda *a, **k: _addrinfo("1.1.1.1", "10.0.0.7"),
                _socket_factory=lambda *a: _FakeSocket(*a),
            )

    def test_connects_to_validated_address(self) -> None:
        made: list[_FakeSocket] = []

        def factory(family: int, socktype: int, proto: int) -> _FakeSocket:
            s = _FakeSocket(family, socktype, proto)
            made.append(s)
            return s

        sock = _safe_create_connection(
            ("good.example.com", 443),
            timeout=5,
            _getaddrinfo=lambda *a, **k: _addrinfo("93.184.216.34"),
            _socket_factory=factory,
        )
        assert isinstance(sock, _FakeSocket)
        assert sock.connected == ("93.184.216.34", 443)

    def test_opener_installs_pinned_handlers(self) -> None:
        handlers = _validating_opener(lambda _u: None).handlers
        assert any(isinstance(h, _PinnedHTTPHandler) for h in handlers)
        assert any(isinstance(h, _PinnedHTTPSHandler) for h in handlers)


# ---------------------------------------------------------------------------
# Save tools
# ---------------------------------------------------------------------------


class TestSaveNotes:
    @pytest.mark.asyncio
    async def test_save_research_note(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_research_note(ctx)(topic="axum-routing", content_markdown="# Axum")
        assert path == "research_notes/axum-routing.md"
        assert store.read_text(path).startswith("# Axum")

    @pytest.mark.asyncio
    async def test_save_research_note_rejects_bad_slug(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_save_research_note(ctx)(topic="Axum Routing!", content_markdown="x")

    @pytest.mark.asyncio
    async def test_save_code_map(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_code_map(ctx)(module="api-handlers", content_markdown="# API")
        assert path == "code_map/api-handlers.md"

    @pytest.mark.asyncio
    async def test_save_dependency_brief(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_dependency_brief(ctx)(content_markdown="# Deps")
        assert path == "dependency_brief.md"

    @pytest.mark.asyncio
    async def test_save_workspace_overview(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        path = await make_save_workspace_overview(ctx)(content_markdown="# Layout")
        assert path == "workspace_overview.md"


# ---------------------------------------------------------------------------
# cargo_metadata + cargo_tree
# ---------------------------------------------------------------------------


class TestCargoMetadata:
    @pytest.mark.asyncio
    async def test_parses_json(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        sample = json.dumps({"packages": [{"name": "foo"}], "workspace_root": "/x"})

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout=sample, stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        data = await make_cargo_metadata(ctx)()
        assert data["packages"][0]["name"] == "foo"

    @pytest.mark.asyncio
    async def test_non_zero_exit_raises(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=1, stdout="", stderr="no manifest", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_cargo_metadata(ctx)()

    @pytest.mark.asyncio
    async def test_bad_json_raises(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout="not json", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_cargo_metadata(ctx)()


class TestCargoTree:
    @pytest.mark.asyncio
    async def test_returns_command_result(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout="foo v0.1.0", stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_cargo_tree(ctx)(args=["--edges", "normal"])
        assert result.ok
        assert "foo" in result.stdout


# ---------------------------------------------------------------------------
# grep + glob
# ---------------------------------------------------------------------------


class TestGrep:
    @pytest.mark.asyncio
    async def test_parses_matches(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        rg_out = "\n".join(
            [
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "src/lib.rs"},
                            "lines": {"text": "pub fn hello() {}\n"},
                            "line_number": 4,
                        },
                    }
                ),
                json.dumps({"type": "summary", "data": {}}),
            ]
        )

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=0, stdout=rg_out, stderr="", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        matches = await make_grep(ctx)(pattern="hello")
        assert len(matches) == 1
        assert matches[0].path == "src/lib.rs"
        assert matches[0].line_number == 4

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(command=cmd, exit_code=1, stdout="", stderr="", duration_sec=0.01)

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        matches = await make_grep(ctx)(pattern="missing")
        assert matches == []

    @pytest.mark.asyncio
    async def test_error_exit_raises(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            return CommandResult(
                command=cmd, exit_code=2, stdout="", stderr="ripgrep blew up", duration_sec=0.01
            )

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        with pytest.raises(ToolError):
            await make_grep(ctx)(pattern="x")


class TestGlob:
    @pytest.mark.asyncio
    async def test_filters_git_ls_files(
        self,
        sandbox: LocalShellSandbox,
        store: ArtifactStore,
        router: ModelRouter,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from maf_coder.agents.results import CommandResult

        async def fake_exec(cmd: str, *, cwd: str = "", timeout_sec: int = 0) -> CommandResult:
            ls = "Cargo.toml\nsrc/lib.rs\nsrc/main.rs\nREADME.md\n"
            return CommandResult(command=cmd, exit_code=0, stdout=ls, stderr="", duration_sec=0.01)

        monkeypatch.setattr(sandbox, "exec", fake_exec)
        ctx = _ctx(sandbox, store, router)
        result = await make_glob(ctx)(pattern="src/*.rs")
        assert sorted(result) == ["src/lib.rs", "src/main.rs"]


# ---------------------------------------------------------------------------
# Builder smoke
# ---------------------------------------------------------------------------


class TestBuilder:
    def test_build_returns_callable_list(
        self, sandbox: LocalShellSandbox, store: ArtifactStore, router: ModelRouter
    ) -> None:
        ctx = _ctx(sandbox, store, router)
        tools = build_research_tools(ctx)
        assert len(tools) == 9
        for t in tools:
            assert callable(t)
