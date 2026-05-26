"""Project Profile (soul.md §6.1).

Produced at mission startup by `project_profiler`. Drives:
- Which Worker tools to enable
- Which BehaviorValidator probe strategy to use
- Which Cargo gate combinations ReviewValidator runs
- Which sandbox image features (target-triple, feature flags) to load

Stored at: missions/<mission_id>/project_profile.yaml
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .common import ProjectType


class Crate(BaseModel):
    """One crate in a Rust project (single-crate or member of a workspace)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = Field(description="'binary' | 'library' | 'proc-macro' | 'mixed'")
    targets: list[str] = Field(default_factory=list, description="Cargo target names")


class Toolchain(BaseModel):
    """Rust toolchain pinning. Reads rust-toolchain.toml if present."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(default="stable", description="'stable' | 'beta' | 'nightly' | 'X.Y.Z'")
    version: str | None = None
    components: list[str] = Field(default_factory=lambda: ["rustfmt", "clippy"])


class FeatureMatrix(BaseModel):
    """Cargo features detected from Cargo.toml."""

    model_config = ConfigDict(extra="forbid")

    default: list[str] = Field(default_factory=list)
    available: list[str] = Field(default_factory=list)
    combinations_to_test: list[str] = Field(
        default_factory=lambda: ["--all-features", "--no-default-features"],
        description="Feature flag combinations ReviewValidator must verify",
    )


class BuildSystem(BaseModel):
    """build.rs presence + external system deps."""

    model_config = ConfigDict(extra="forbid")

    has_build_rs: bool = False
    external_deps: list[str] = Field(
        default_factory=list,
        description="System packages required, e.g. ['protoc', 'openssl-dev', 'libsqlite3-dev']",
    )
    cross_compile_targets: list[str] = Field(
        default_factory=list,
        description="Target triples like 'wasm32-unknown-unknown', 'thumbv7em-none-eabi'",
    )


class TestStrategy(BaseModel):
    """How tests run for this project."""

    model_config = ConfigDict(extra="forbid")

    unit_test_command: str = "cargo test --workspace"
    integration_test_command: str = "cargo test --workspace --test '*'"
    doc_test_command: str = "cargo test --workspace --doc"
    benchmark_command: str = Field(
        default="cargo bench --workspace --no-run",
        description="--no-run default: actual bench runs are explicit user request only",
    )


class BehaviorProbeSpec(BaseModel):
    """Maps the project type to a BehaviorValidator probe strategy."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(
        description="One of: cli_assert_cmd_probe | backend_service_health_probe | "
        "library_example_probe | embedded_host_test_probe | wasm_node_probe"
    )
    start_command: str | None = Field(
        default=None,
        description="For service: e.g. 'cargo run --bin api'. For CLI: omitted (probe invokes per-test).",
    )
    ready_check: str | None = Field(
        default=None,
        description="Command/check that returns 0 when service is ready, e.g. 'curl -sf localhost:8080/health'",
    )
    endpoints_to_probe: list[str] = Field(default_factory=list)
    timeout_sec: int = Field(default=300)


class CIExisting(BaseModel):
    """Whether project has its own CI we can reuse."""

    model_config = ConfigDict(extra="forbid")

    has_github_actions: bool = False
    has_gitlab_ci: bool = False
    workflow_paths: list[str] = Field(default_factory=list)
    reuse: bool = Field(
        default=True, description="If True, ReviewValidator may invoke existing CI scripts"
    )


class ProjectProfile(BaseModel):
    """The auto-detected project profile.

    Produced by `project_profiler` reading Cargo.toml, workspace structure,
    rust-toolchain.toml, .github/workflows/, etc.
    """

    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    project_type: ProjectType
    crate_layout: str = Field(description="'single' | 'workspace'")
    crates: list[Crate] = Field(min_length=1)
    toolchain: Toolchain = Field(default_factory=Toolchain)
    features: FeatureMatrix = Field(default_factory=FeatureMatrix)
    build_system: BuildSystem = Field(default_factory=BuildSystem)
    test_strategy: TestStrategy = Field(default_factory=TestStrategy)
    behavior_probe: BehaviorProbeSpec
    ci_existing: CIExisting = Field(default_factory=CIExisting)
