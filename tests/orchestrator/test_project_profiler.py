"""Project Profiler tests (AGENT_TOOLS_SPEC §17 step 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from maf_coder.orchestrator.project_profiler import (
    ProjectProfileError,
    profile_project,
)
from maf_coder.schemas import ProjectType


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_missing_cargo_toml_returns_placeholder(tmp_path: Path) -> None:
    profile = profile_project(tmp_path)
    assert profile.project_type == ProjectType.LIBRARY
    assert profile.crate_layout == "single"
    assert len(profile.crates) == 1


def test_strict_missing_cargo_toml_raises(tmp_path: Path) -> None:
    # Real runs must fail loud, not silently mis-profile a non-Cargo dir.
    with pytest.raises(ProjectProfileError):
        profile_project(tmp_path, strict=True)


def test_strict_malformed_cargo_toml_raises(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", "this is = = not valid toml [[[")
    with pytest.raises(ProjectProfileError):
        profile_project(tmp_path, strict=True)


def test_strict_valid_cargo_toml_profiles_normally(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "demo"\n\n[[bin]]\nname = "demo"\n',
    )
    profile = profile_project(tmp_path, strict=True)
    assert profile.crates  # parsed a real crate, no exception


def test_single_binary_cli_project(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        "[package]\n"
        'name = "my-cli"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        'description = "A small command-line tool"\n'
        "[[bin]]\n"
        'name = "my-cli"\n',
    )
    profile = profile_project(tmp_path)
    assert profile.project_type == ProjectType.CLI
    assert profile.crate_layout == "single"
    assert profile.crates[0].name == "my-cli"
    assert profile.behavior_probe.strategy == "cli_assert_cmd_probe"


def test_backend_service_detected_from_description(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        "[package]\n"
        'name = "api"\n'
        'version = "0.1.0"\n'
        'description = "An HTTP service that serves /health"\n'
        "[[bin]]\n"
        'name = "api"\n',
    )
    profile = profile_project(tmp_path)
    assert profile.project_type == ProjectType.BACKEND_SERVICE
    assert profile.behavior_probe.strategy == "backend_service_health_probe"


def test_workspace_layout(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[workspace]\nmembers = ["a", "b"]\n',
    )
    _write(
        tmp_path / "a" / "Cargo.toml",
        '[package]\nname = "a"\nversion = "0.1.0"\n[lib]\nname = "a"\n',
    )
    _write(
        tmp_path / "b" / "Cargo.toml",
        '[package]\nname = "b"\nversion = "0.1.0"\n[[bin]]\nname = "b"\n',
    )
    profile = profile_project(tmp_path)
    assert profile.crate_layout == "workspace"
    names = sorted(c.name for c in profile.crates)
    assert names == ["a", "b"]


def test_rust_toolchain_toml_read(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "lib"\nversion = "0.1.0"\n[lib]\nname = "lib"\n',
    )
    _write(
        tmp_path / "rust-toolchain.toml",
        '[toolchain]\nchannel = "nightly-2026-01-15"\n'
        'components = ["rustfmt", "clippy", "rust-analyzer"]\n',
    )
    profile = profile_project(tmp_path)
    assert profile.toolchain.channel == "nightly-2026-01-15"
    assert "rust-analyzer" in profile.toolchain.components


def test_features_detected(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "lib"\nversion = "0.1.0"\n[lib]\nname = "lib"\n'
        '[features]\ndefault = ["json"]\njson = []\nasync = []\n',
    )
    profile = profile_project(tmp_path)
    assert profile.features.default == ["json"]
    assert set(profile.features.available) == {"json", "async"}


def test_ci_detected(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "lib"\nversion = "0.1.0"\n[lib]\nname = "lib"\n',
    )
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\n")
    profile = profile_project(tmp_path)
    assert profile.ci_existing.has_github_actions is True
    assert any(p.endswith("ci.yml") for p in profile.ci_existing.workflow_paths)


def test_build_rs_detected(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "lib"\nversion = "0.1.0"\n[lib]\nname = "lib"\n',
    )
    _write(tmp_path / "build.rs", "fn main() {}\n")
    profile = profile_project(tmp_path)
    assert profile.build_system.has_build_rs is True
