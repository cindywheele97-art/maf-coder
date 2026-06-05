"""Project Profiler (AGENT_TOOLS_SPEC §17 step 8 + soul.md §6.1).

Reads `Cargo.toml`, `Cargo.lock`, `rust-toolchain.toml`, and any
`.github/workflows/` files in `repo_path` and produces a `ProjectProfile`. The
profile drives downstream tool wiring (Behavior probe strategy, cargo feature
combinations, test commands).

Phase B: best-effort parsing using Python stdlib only. tomllib is stdlib on
Python 3.11+; PyYAML is already a project dep. We do NOT shell out to cargo.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from ..schemas import (
    BehaviorProbeSpec,
    BuildSystem,
    CIExisting,
    Crate,
    FeatureMatrix,
    ProjectProfile,
    ProjectType,
    TestStrategy,
    Toolchain,
)

logger = logging.getLogger(__name__)


class ProjectProfileError(RuntimeError):
    """Raised by `profile_project(strict=True)` when the repo can't be profiled.

    A missing or malformed Cargo.toml on a real mission means the operator
    pointed `--repo` at the wrong place (or a non-Cargo project); silently
    profiling a placeholder `library` crate would mislead the entire mission, so
    real runs fail loud instead.
    """


def profile_project(repo_path: str | Path, *, strict: bool = False) -> ProjectProfile:
    """Inspect `repo_path` and return a ProjectProfile.

    By default (``strict=False``, the bootstrap/dry-run path) this tolerates a
    missing or malformed Cargo.toml and falls back to a `library` profile with a
    single placeholder crate — a partial profile over a crash.

    Real missions (``--no-dry-run``) pass ``strict=True``: a missing/malformed
    Cargo.toml raises :class:`ProjectProfileError` rather than silently
    mis-profiling, which would mislead the whole mission.
    """
    root = Path(repo_path).resolve()
    cargo_toml = root / "Cargo.toml"

    if not cargo_toml.exists():
        if strict:
            raise ProjectProfileError(
                f"{cargo_toml} not found — point --repo at a Cargo project, or use --dry-run"
            )
        logger.warning("profile_project: %s missing", cargo_toml)
        return _placeholder_profile(name=root.name or "unknown")

    try:
        with cargo_toml.open("rb") as fh:
            cargo = tomllib.load(fh)
    except Exception as e:
        if strict:
            raise ProjectProfileError(f"failed to parse {cargo_toml}: {e}") from e
        logger.warning("profile_project: failed to parse %s: %r", cargo_toml, e)
        return _placeholder_profile(name=root.name or "unknown")

    crate_layout, crates = _detect_layout(root, cargo)
    project_type = _detect_project_type(crates, cargo)
    toolchain = _read_toolchain(root)
    features = _read_features(cargo)
    build_system = _read_build_system(root, crates)
    behavior_probe = _default_probe_for(project_type)
    ci_existing = _detect_ci(root)
    test_strategy = TestStrategy()

    return ProjectProfile(
        project_type=project_type,
        crate_layout=crate_layout,
        crates=crates,
        toolchain=toolchain,
        features=features,
        build_system=build_system,
        test_strategy=test_strategy,
        behavior_probe=behavior_probe,
        ci_existing=ci_existing,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _placeholder_profile(*, name: str) -> ProjectProfile:
    return ProjectProfile(
        project_type=ProjectType.LIBRARY,
        crate_layout="single",
        crates=[Crate(name=name, type="library", targets=[])],
        behavior_probe=BehaviorProbeSpec(strategy="library_example_probe"),
    )


def _detect_layout(root: Path, cargo: dict[str, Any]) -> tuple[str, list[Crate]]:
    ws = cargo.get("workspace")
    if ws and "members" in ws:
        crates: list[Crate] = []
        for member_glob in ws.get("members", []):
            for member_dir in root.glob(member_glob):
                if not (member_dir / "Cargo.toml").exists():
                    continue
                try:
                    with (member_dir / "Cargo.toml").open("rb") as fh:
                        sub = tomllib.load(fh)
                except Exception:
                    continue
                crates.append(_crate_from(sub, fallback_name=member_dir.name))
        if not crates:
            crates = [Crate(name=root.name, type="library", targets=[])]
        return "workspace", crates
    return "single", [_crate_from(cargo, fallback_name=root.name)]


def _crate_from(cargo: dict[str, Any], *, fallback_name: str) -> Crate:
    pkg = cargo.get("package", {})
    name = pkg.get("name", fallback_name)
    has_bin = "bin" in cargo or (root_has_bin_main_rs := False) or False  # noqa: F841
    has_lib = "lib" in cargo
    if has_bin and has_lib:
        ctype = "mixed"
    elif has_bin:
        ctype = "binary"
    elif has_lib:
        ctype = "library"
    else:
        # Heuristic: package without explicit bin/lib → library default
        ctype = "library"
    targets: list[str] = []
    for bin_def in cargo.get("bin", []) or []:
        n = bin_def.get("name") if isinstance(bin_def, dict) else None
        if n:
            targets.append(n)
    return Crate(name=str(name), type=ctype, targets=targets)


def _detect_project_type(crates: list[Crate], cargo: dict[str, Any]) -> ProjectType:
    has_binary = any(c.type in {"binary", "mixed"} for c in crates)
    has_library = any(c.type in {"library", "mixed"} for c in crates)
    targets = cargo.get("package", {}).get("metadata", {})
    keywords = cargo.get("package", {}).get("keywords", []) or []
    description = (cargo.get("package", {}).get("description") or "").lower()
    if has_binary and has_library:
        return ProjectType.MIXED
    if has_binary:
        if any(k in description for k in ("server", "service", "api", "http", "grpc")):
            return ProjectType.BACKEND_SERVICE
        if any(k in keywords for k in ("server", "service", "http", "api")):
            return ProjectType.BACKEND_SERVICE
        if "wasm" in targets:
            return ProjectType.WASM
        return ProjectType.CLI
    return ProjectType.LIBRARY


def _read_toolchain(root: Path) -> Toolchain:
    rt = root / "rust-toolchain.toml"
    if rt.exists():
        try:
            with rt.open("rb") as fh:
                tc = tomllib.load(fh).get("toolchain", {})
            return Toolchain(
                channel=str(tc.get("channel", "stable")),
                version=tc.get("version"),
                components=list(tc.get("components", ["rustfmt", "clippy"])),
            )
        except Exception:
            pass
    legacy = root / "rust-toolchain"
    if legacy.exists():
        return Toolchain(channel=legacy.read_text(encoding="utf-8").strip() or "stable")
    return Toolchain()


def _read_features(cargo: dict[str, Any]) -> FeatureMatrix:
    feats = cargo.get("features", {}) or {}
    default = list(feats.get("default", []) or [])
    available = [k for k in feats if k != "default"]
    return FeatureMatrix(default=default, available=available)


def _read_build_system(root: Path, crates: list[Crate]) -> BuildSystem:
    has_build_rs = (root / "build.rs").exists()
    if not has_build_rs:
        # Workspace: any member has build.rs?
        for member in root.glob("*/build.rs"):
            if member.exists():
                has_build_rs = True
                break
    return BuildSystem(has_build_rs=has_build_rs)


def _default_probe_for(pt: ProjectType) -> BehaviorProbeSpec:
    mapping = {
        ProjectType.CLI: "cli_assert_cmd_probe",
        ProjectType.BACKEND_SERVICE: "backend_service_health_probe",
        ProjectType.LIBRARY: "library_example_probe",
        ProjectType.EMBEDDED: "embedded_host_test_probe",
        ProjectType.WASM: "wasm_node_probe",
        ProjectType.MIXED: "cli_assert_cmd_probe",
    }
    return BehaviorProbeSpec(strategy=mapping.get(pt, "library_example_probe"))


def _detect_ci(root: Path) -> CIExisting:
    gh = (
        list((root / ".github" / "workflows").glob("*.y*ml"))
        if (root / ".github" / "workflows").is_dir()
        else []
    )
    gitlab = (root / ".gitlab-ci.yml").exists()
    return CIExisting(
        has_github_actions=bool(gh),
        has_gitlab_ci=gitlab,
        workflow_paths=[str(p.relative_to(root)) for p in gh],
    )


__all__ = ["ProjectProfileError", "profile_project"]
