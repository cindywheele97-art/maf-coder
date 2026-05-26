# WORKED_EXAMPLE.md

> A complete end-to-end mission walkthrough with all artifact contents at every step. Serves as the "north star" reference for what good Phase B+ outputs look like.
>
> Companion to:
> - `ARCHITECTURE.md` — system shape (the "what")
> - `AGENT_TOOLS_SPEC.md` — formal signatures (the "how")
> - `agent_team_soul_v3.1.md` — constitution (the "why")
> - `prompts/*.md` — agent behavior contracts (the "how each thinks")
>
> Every artifact in this document is **schema-valid**: parsing it through the corresponding Pydantic model in `src/maf_coder/schemas/` MUST succeed. If you change a schema, update this example to match.

---

## 0. The scenario

A Rust workspace hosting an axum HTTP service called `my_api`. Existing layout:

```
my-api-repo/
├── Cargo.toml                              (workspace)
├── Cargo.lock
├── rust-toolchain.toml                     (channel: stable, version: 1.85)
├── .github/workflows/ci.yml
├── crates/
│   ├── my_api/                              (binary crate)
│   │   ├── Cargo.toml
│   │   ├── src/
│   │   │   ├── main.rs
│   │   │   ├── lib.rs
│   │   │   └── routes/
│   │   │       ├── mod.rs
│   │   │       ├── health.rs
│   │   │       └── users.rs
│   │   └── tests/
│   │       ├── health_test.rs
│   │       └── users_test.rs
│   └── my_api_core/                         (library crate)
│       ├── Cargo.toml
│       └── src/
│           └── lib.rs
```

The service exposes `GET /api/v1/health` and `GET /api/v1/users`. It uses postgres by default, with sqlite as a feature flag. The workspace version is `0.4.7` in `crates/my_api/Cargo.toml`.

The user wants a new endpoint that returns the running crate's version. Simple, but real (this is the kind of small ticket developers delegate constantly).

**User invocation:**

```bash
maf-coder mission \
  --repo /Users/john/code/my-api-repo \
  --budget-alert-usd 5 \
  --goal "Add a GET /api/v1/version endpoint to my_api that returns the running crate's version (as declared in Cargo.toml) in a JSON response body. Do not change behavior of existing endpoints. Tests must verify both that the endpoint exists and that the version matches Cargo.toml at build time."
```

The Mission Driver assigns `mission_id = m-2026-05-25-001`.

---

## 1. Project Profile (mission start)

The Mission Driver runs `project_profiler` against the repo before invoking the Orchestrator. Output:

**`missions/m-2026-05-25-001/project_profile.yaml`**

```yaml
project_type: backend_service
crate_layout: workspace
crates:
  - name: my_api
    type: binary
    targets: [my_api]
  - name: my_api_core
    type: library
    targets: []
toolchain:
  channel: stable
  version: "1.85"
  components:
    - rustfmt
    - clippy
features:
  default: [postgres]
  available: [postgres, sqlite, metrics]
  combinations_to_test:
    - "--all-features"
    - "--features=sqlite"
    - "--no-default-features"
build_system:
  has_build_rs: false
  external_deps:
    - openssl-dev
    - libpq-dev
  cross_compile_targets: []
test_strategy:
  unit_test_command: "cargo test --workspace"
  integration_test_command: "cargo test --workspace --test '*'"
  doc_test_command: "cargo test --workspace --doc"
  benchmark_command: "cargo bench --workspace --no-run"
behavior_probe:
  strategy: backend_service_health_probe
  start_command: "cargo run --bin my_api"
  ready_check: "curl -sf http://localhost:8080/api/v1/health"
  endpoints_to_probe:
    - "/api/v1/health"
    - "/api/v1/users"
  timeout_sec: 300
ci_existing:
  has_github_actions: true
  has_gitlab_ci: false
  workflow_paths:
    - ".github/workflows/ci.yml"
  reuse: true
```

**Why this matters**: `project_type: backend_service` + `behavior_probe.strategy: backend_service_health_probe` tells the Orchestrator (1) what kind of mission it's planning and (2) which BehaviorValidator strategy to invoke later. Without the profile, every downstream decision is guesswork.

---

## 2. Planning phase

The Orchestrator runs with `prompts/orchestrator.md` as its instructions. First user message contains the user goal + serialized profile. The Orchestrator produces three artifacts.

### 2.1 plan.md

**`missions/m-2026-05-25-001/plan.md`**

```markdown
# Plan: m-2026-05-25-001

## Mission Goal

Add a `GET /api/v1/version` endpoint to the `my_api` service that returns the running crate's version from Cargo.toml in a JSON response body. The new endpoint must not break any existing functionality.

## Approach

The scope is small enough for a single milestone. The endpoint pattern matches the existing routes (`/health`, `/users`), so Research will map the conventional pattern and Coder will follow it. BehaviorValidator runs a runtime probe to verify the endpoint actually serves traffic correctly.

## Milestones

### m1 — Survey + Implementation + Verification

| Order | Task | Owner | Purpose |
|---|---|---|---|
| 1 | t1 | research_worker | Map existing routing layer; identify version-access mechanism (env! macro vs cargo_metadata) |
| 2 | t2 | coder_worker | Implement endpoint + tests per validation contract |
| 3a | t3 | security_worker | Scan for dep changes (likely none) — parallel with t4 |
| 3b | t4 | review_validator | Cargo gates + assertion coverage + adversarial review of t2 |
| 4 | t5 | behavior_validator | Runtime probe: start service, GET /api/v1/version, verify body |

Execution order: t1 → t2 → (t3 ‖ t4) → t5.

## Budget estimate

- Cost: $0.50 – $2.00 (likely <$1)
- Wall-clock: 30–90 minutes
- Token usage: ~150k input + 30k output

If budget exceeds $5 before completion (the user's `--budget-alert-usd 5`), Orchestrator enters cost-conscious mode and emits an immediate status report.

## Risks identified at planning

1. **Cargo.toml workspace inheritance**: if `version` uses `workspace = true` inheritance, `env!("CARGO_PKG_VERSION")` still resolves correctly at compile time, so this is a non-risk. Documented in risk_acknowledgements of the contract.
2. **HTTP routing module conflict**: the route name `version` could shadow an existing module if `version.rs` already exists. Research t1 verifies.

## Next step

Orchestrator dispatches t1 (Research). Once research_notes/api_routing.md exists, dispatches t2 (Coder).
```

### 2.2 validation_contract.yaml — LOCKED

The contract is written **once**, immediately locked. The next time anything tries to write to this file, `ArtifactStore.save_validation_contract` raises `ContractAlreadyLockedError` (per AGENT_TOOLS_SPEC §6 `save_artifact` behavior).

**`missions/m-2026-05-25-001/validation_contract.yaml`**

```yaml
mission_id: m-2026-05-25-001
created_at: 2026-05-25T09:00:00Z
created_by: orchestrator
locked: true
project_profile_ref: project_profile.yaml
features:
  - feature_id: f1
    description: "GET /api/v1/version endpoint"
    assertions:
      - id: f1.a1
        statement: "HTTP GET /api/v1/version returns status 200"
        verification_method: behavior_probe
        verification_target: "behavior_probe::backend_service_health_probe::endpoint_version"
      - id: f1.a2
        statement: "Response body is valid JSON with field 'version' of type string"
        verification_method: behavior_probe
        verification_target: "behavior_probe::backend_service_health_probe::endpoint_version"
      - id: f1.a3
        statement: "Response 'version' field equals the my_api crate version declared in Cargo.toml at build time"
        verification_method: integration_test
        verification_target: "crates/my_api/tests/version_test.rs::test_version_matches_cargo"
      - id: f1.a4
        statement: "Adding this endpoint does not modify response status, body shape, or routing for /api/v1/health or /api/v1/users"
        verification_method: integration_test
        verification_target: "crates/my_api/tests (existing 24-test suite)"
non_goals:
  - "Updating OpenAPI / API documentation generation"
  - "Adding version exposure to library crates in the workspace (my_api_core)"
  - "Exposing git hash, build timestamp, or other build-time metadata"
  - "Adding rate limiting or caching to the new endpoint"
risk_acknowledgements:
  - "Cargo.toml version inheritance via workspace.package is non-blocking — env!() resolves at compile time before inheritance is relevant"
  - "If a 'version' module already exists in crates/my_api/src/routes/, Coder will rename to 'version_endpoint' and note in handoff"
```

**Why each assertion has a verification_target**: this is the contract's hardest discipline (per `prompts/orchestrator.md` §Validation Contract). `f1.a3` and `f1.a4` have integration test targets — they MUST exist as test code in the patch. `f1.a1` and `f1.a2` have probe targets — they MUST be verified by BehaviorValidator's runtime probe. No assertion has `verification_method: manual`; everything is automatable.

### 2.3 tasks.yaml — the DAG

**`missions/m-2026-05-25-001/tasks.yaml`**

```yaml
mission_id: m-2026-05-25-001
tasks:
  - task_id: t1
    parent_milestone: m1
    owner: research_worker
    priority: medium
    risk_level: low
    goal: "Map existing API routing in crates/my_api/src/ and determine the best way to access Cargo.toml version at runtime"
    background: "Coder t2 needs (1) the routing pattern to mirror and (2) the version-access mechanism. Without this, t2 would explore the codebase blindly."
    acceptance_criteria: []
    input_artifacts:
      - "spec://plan.md"
      - "profile://project_profile.yaml"
    required_outputs:
      - "research_notes/api_routing.md"
      - "code_map/my_api.md"
    permission:
      allowed_paths:
        - "./crates"
        - "./Cargo.toml"
        - "./Cargo.lock"
      allowed_tools:
        - "read_file"
        - "grep"
        - "glob"
        - "cargo_metadata"
        - "fetch_url"
        - "save_research_note"
        - "save_code_map"
      network_policy: open
      human_approval_required: false
    budget:
      max_tokens: 50000
      max_runtime_sec: 600
      cost_ceiling_usd: null
    failure_handling:
      retry_budget: 1
      escalation_target: orchestrator
      rollback_checkpoint: null
    depends_on: []

  - task_id: t2
    parent_milestone: m1
    owner: coder_worker
    priority: high
    risk_level: medium
    goal: "Implement GET /api/v1/version per contract f1.a1, f1.a2, f1.a3, f1.a4"
    background: "All four assertions in f1 must be covered. Use the routing pattern Research found. Version access via env!(\"CARGO_PKG_VERSION\") per research notes."
    acceptance_criteria: ["f1.a1", "f1.a2", "f1.a3", "f1.a4"]
    input_artifacts:
      - "contract://validation_contract.yaml"
      - "research://research_notes/api_routing.md"
      - "research://code_map/my_api.md"
    required_outputs:
      - "patches/t2.diff"
      - "reports/t2.test.json"
      - "handoff/t2.json"
    permission:
      allowed_paths:
        - "./crates/my_api/src"
        - "./crates/my_api/tests"
        - "./crates/my_api/Cargo.toml"
      allowed_tools:
        - "read_file"
        - "write_file"
        - "edit_file"
        - "run_bash"
        - "cargo_check"
        - "cargo_test"
        - "cargo_clippy"
        - "cargo_fmt"
        - "cargo_nextest"
        - "git_status"
        - "git_diff"
        - "git_checkout"
        - "save_patch"
        - "save_handoff"
        - "save_test_report"
      network_policy: crates_only
      human_approval_required: false
    budget:
      max_tokens: 100000
      max_runtime_sec: 1200
      cost_ceiling_usd: null
    failure_handling:
      retry_budget: 2
      escalation_target: orchestrator
      rollback_checkpoint: null
    depends_on: ["t1"]

  - task_id: t3
    parent_milestone: m1
    owner: security_worker
    priority: medium
    risk_level: low
    goal: "Audit any dependency changes introduced by t2 (likely none) and confirm no secrets leaked"
    background: "/version endpoint should not require new deps; verify with cargo audit + deny + geiger + gitleaks"
    acceptance_criteria: []
    input_artifacts:
      - "patches://patches/t2.diff"
    required_outputs:
      - "verdicts/t3.security.json"
      - "security_notes/t3.md"
    permission:
      allowed_paths:
        - "./crates"
        - "./Cargo.toml"
        - "./Cargo.lock"
      allowed_tools:
        - "cargo_audit"
        - "cargo_deny_check"
        - "cargo_geiger"
        - "gitleaks_detect"
        - "trufflehog_scan"
        - "read_file"
        - "save_security_verdict"
      network_policy: crates_only
      human_approval_required: false
    budget:
      max_tokens: 30000
      max_runtime_sec: 600
      cost_ceiling_usd: null
    failure_handling:
      retry_budget: 1
      escalation_target: orchestrator
      rollback_checkpoint: null
    depends_on: ["t2"]

  - task_id: t4
    parent_milestone: m1
    owner: review_validator
    priority: high
    risk_level: medium
    goal: "Validate t2 patch: cargo gates + assertion coverage + adversarial sub-agent review"
    background: "Standard ReviewValidator workflow per prompts/review_validator.md §Verification sequence. Both hardcoded-test detection AND completeness rule second-pass must run."
    acceptance_criteria: ["f1.a3", "f1.a4"]
    input_artifacts:
      - "contract://validation_contract.yaml"
      - "patches://patches/t2.diff"
      - "handoff://handoff/t2.json"
      - "reports://reports/t2.test.json"
      - "research://research_notes/api_routing.md"
    required_outputs:
      - "verdicts/t4.review.json"
      - "review_notes/t4.md"
    permission:
      allowed_paths:
        - "./crates"
      allowed_tools:
        - "read_file"
        - "grep"
        - "glob"
        - "cargo_check"
        - "cargo_build"
        - "cargo_test"
        - "cargo_clippy"
        - "cargo_fmt"
        - "cargo_nextest"
        - "cargo_test_doc"
        - "git_diff"
        - "git_show"
        - "git_log"
        - "apply_patch_in_fresh_worktree"
        - "spawn_adversarial_subagent"
        - "save_review_verdict"
        - "save_review_notes"
      network_policy: none
      human_approval_required: false
    budget:
      max_tokens: 60000
      max_runtime_sec: 1200
      cost_ceiling_usd: null
    failure_handling:
      retry_budget: 0
      escalation_target: orchestrator
      rollback_checkpoint: null
    depends_on: ["t2"]

  - task_id: t5
    parent_milestone: m1
    owner: behavior_validator
    priority: high
    risk_level: low
    goal: "Run backend_service_health_probe: start my_api, probe /api/v1/version, verify f1.a1 + f1.a2"
    background: "Runs only after t4 returns PASS. Uses probe strategy from project_profile.behavior_probe."
    acceptance_criteria: ["f1.a1", "f1.a2"]
    input_artifacts:
      - "contract://validation_contract.yaml"
      - "verdicts://verdicts/t4.review.json"
      - "profile://project_profile.yaml"
    required_outputs:
      - "verdicts/t5.behavior.json"
      - "behavior_evidence/t5/"
      - "behavior_trace/t5.md"
    permission:
      allowed_paths:
        - "./crates"
      allowed_tools:
        - "start_service"
        - "stop_service"
        - "probe_http"
        - "save_behavior_evidence"
        - "save_behavior_verdict"
        - "read_file"
      network_policy: none
      human_approval_required: false
    budget:
      max_tokens: 30000
      max_runtime_sec: 600
      cost_ceiling_usd: null
    failure_handling:
      retry_budget: 1
      escalation_target: orchestrator
      rollback_checkpoint: null
    depends_on: ["t4"]
```

Note `t4.acceptance_criteria` only lists `f1.a3` and `f1.a4` — the ones it can verify itself (integration tests). `f1.a1` and `f1.a2` are behavior probe assertions, assigned to t5. This is how the contract gets distributed across validators.

Also note `t4.failure_handling.retry_budget: 0` — validators don't retry. If validation fails, the Coder retries (per t2's retry_budget), not the validator.

---

## 3. Research task (t1)

Research Worker dispatched. It reads code, runs `cargo metadata`, optionally fetches docs.rs / crates.io for `env!` macro semantics, then writes two notes.

### 3.1 research_notes/api_routing.md

**`missions/m-2026-05-25-001/research_notes/api_routing.md`**

```markdown
# API Routing in `crates/my_api`

## Pattern in use

The crate uses **nested axum routers** for module separation. Each route lives in `src/routes/<name>.rs` exposing a `router() -> axum::Router` function. The top-level assembly happens in `src/routes/mod.rs`:

\`\`\`rust
// src/routes/mod.rs (current state)
pub mod health;
pub mod users;

use axum::Router;

pub fn api_router() -> Router {
    Router::new()
        .nest("/health", health::router())
        .nest("/users", users::router())
}
\`\`\`

The `/api/v1/` prefix is applied **one layer up** in `src/main.rs`:

\`\`\`rust
// src/main.rs (excerpt)
let app = Router::new().nest("/api/v1", routes::api_router());
\`\`\`

This means: when adding a `/version` route, the new code should produce a `Router` that handles `/` and nest it at `/version`. The full path `/api/v1/version` is composed by the outer prefix + the nest call.

## Naming check

`grep -r "mod version" crates/my_api/src/` returns nothing. No conflict; we can use `version.rs`.

## How to access crate version at runtime

Three options:

| Option | Cost | Reliability | Verdict |
|---|---|---|---|
| `env!("CARGO_PKG_VERSION")` | Zero (compile-time string literal) | Always correct unless Cargo.toml is corrupt | **Recommended** |
| `cargo_metadata::MetadataCommand::new()...` | Runtime fork + parse | Requires `cargo` in PATH at runtime — bad assumption for deployed binaries | Reject |
| Build-time generated module via `build.rs` | Extra build step | Equivalent to env! but more code | Reject |

**Decision**: use `env!("CARGO_PKG_VERSION")`. This is the same string Cargo would otherwise compute, just delivered at compile time as a `&'static str`. Even when Cargo.toml uses `[workspace.package] version = "..."` inheritance, `env!()` resolves the inherited value correctly.

## Existing tests

`tests/health_test.rs` and `tests/users_test.rs` use `tower::ServiceExt::oneshot` against the assembled `api_router()`. The pattern Coder t2 should mirror:

\`\`\`rust
let app = api_router();
let response = app
    .oneshot(Request::builder().uri("/<path>").body(()).unwrap())
    .await
    .unwrap();
\`\`\`

Notice the URI in `oneshot` calls is **without** the `/api/v1` prefix — they test the routes module directly. f1.a3 (integration test) should follow this convention.

## Sources

- Local code: `crates/my_api/src/routes/{mod.rs, health.rs, users.rs}`
- Local code: `crates/my_api/src/main.rs`
- Local code: `crates/my_api/tests/{health_test.rs, users_test.rs}`
- External: rustdoc for `env!` — https://doc.rust-lang.org/std/macro.env.html (sanitized; see egress.jsonl event 14)
```

### 3.2 code_map/my_api.md

**`missions/m-2026-05-25-001/code_map/my_api.md`** (abbreviated for brevity):

```markdown
# Code Map: crates/my_api/

## Module structure

| File | Purpose | LOC |
|---|---|---|
| `src/main.rs` | Binary entrypoint; assembles top-level Router with `/api/v1` prefix; spawns axum on `0.0.0.0:8080` | 42 |
| `src/lib.rs` | Re-exports public surface for integration tests; exposes `routes::api_router()` | 8 |
| `src/routes/mod.rs` | Router assembly; nests each route module | 12 |
| `src/routes/health.rs` | GET /health returning `{"status": "ok"}` | 18 |
| `src/routes/users.rs` | GET /users + GET /users/:id + POST /users | 87 |
| `src/db/mod.rs` | Postgres / sqlite driver behind feature flag | 134 |
| `src/error.rs` | Crate error type using `thiserror`; converts to axum responses | 56 |

## Conventions observed

- Error handling: `thiserror::Error` enum at crate level; `axum::IntoResponse` impl on the error type
- Logging: `tracing::info!/error!/warn!` (no `println!`)
- Async runtime: `tokio` with `#[tokio::main]` in main.rs; `#[tokio::test]` in tests
- Naming: `snake_case` for all files except `Cargo.toml`; module function `router()` is the public surface
- Test placement: `crates/my_api/tests/<feature>_test.rs` for integration, `mod tests {}` inside src files for unit

## Where t2 will write

Per task spec t2.permission.allowed_paths:
- `crates/my_api/src/routes/version.rs` (new)
- `crates/my_api/src/routes/mod.rs` (edit: add `pub mod version;` and `.nest("/version", ...)`)
- `crates/my_api/tests/version_test.rs` (new)

No other files should be touched.
```

EventLog appended:
- `TASK_DISPATCHED` for t1 with owner=research_worker
- `LLM_CALL` events with role=research_worker, model=anthropic/claude-sonnet-4-6, tokens, cost
- `TOOL_CALL` events for each cargo_metadata, grep, fetch_url
- `ARTIFACT_WRITTEN` for research_notes/api_routing.md
- `ARTIFACT_WRITTEN` for code_map/my_api.md
- `TASK_COMPLETE` for t1

---

## 4. Coder task (t2)

The Coder Worker is dispatched. Following `prompts/coder_worker.md` v3.1 discipline, its very first action inside the sandbox is `git checkout -- .` (idempotent reset). Then it reads research notes, runs `cargo check`, writes the new code, runs the cargo gate set, produces three artifacts.

### 4.1 patches/t2.diff

**`missions/m-2026-05-25-001/patches/t2.diff`**

```diff
diff --git a/crates/my_api/src/routes/mod.rs b/crates/my_api/src/routes/mod.rs
index 7a3b9c2..d8e1f56 100644
--- a/crates/my_api/src/routes/mod.rs
+++ b/crates/my_api/src/routes/mod.rs
@@ -1,5 +1,6 @@
 pub mod health;
 pub mod users;
+pub mod version;
 
 use axum::Router;
 
@@ -7,4 +8,5 @@ pub fn api_router() -> Router {
     Router::new()
         .nest("/health", health::router())
         .nest("/users", users::router())
+        .nest("/version", version::router())
 }
diff --git a/crates/my_api/src/routes/version.rs b/crates/my_api/src/routes/version.rs
new file mode 100644
index 0000000..e3a7b21
--- /dev/null
+++ b/crates/my_api/src/routes/version.rs
@@ -0,0 +1,32 @@
+//! GET /api/v1/version — returns the running crate version.
+//!
+//! Version is sourced from `CARGO_PKG_VERSION` at compile time. This matches
+//! the `version` field of `crates/my_api/Cargo.toml` (workspace inheritance
+//! resolves before `env!` evaluates).
+
+use axum::{routing::get, Json, Router};
+use serde::Serialize;
+
+/// JSON response shape for GET /version.
+#[derive(Serialize)]
+pub struct VersionResponse {
+    pub version: String,
+}
+
+/// Router exposing GET / (mounted at `/version` by routes::api_router).
+pub fn router() -> Router {
+    Router::new().route("/", get(get_version))
+}
+
+async fn get_version() -> Json<VersionResponse> {
+    Json(VersionResponse {
+        version: env!("CARGO_PKG_VERSION").to_string(),
+    })
+}
+
+#[cfg(test)]
+mod tests {
+    // Unit-level smoke. Full assertion coverage in tests/version_test.rs.
+    use super::*;
+    #[test]
+    fn router_constructs() { let _ = router(); }
+}
diff --git a/crates/my_api/tests/version_test.rs b/crates/my_api/tests/version_test.rs
new file mode 100644
index 0000000..f1b2c34
--- /dev/null
+++ b/crates/my_api/tests/version_test.rs
@@ -0,0 +1,52 @@
+//! Integration tests for GET /api/v1/version. Covers f1.a3.
+//!
+//! f1.a1 and f1.a2 are runtime-behavior assertions verified by
+//! BehaviorValidator probe (see verdicts/t5.behavior.json).
+
+use axum::body::to_bytes;
+use axum::http::{Request, StatusCode};
+use http_body_util::BodyExt;
+use my_api::routes::api_router;
+use serde_json::Value;
+use tower::ServiceExt;
+
+async fn fetch_version_body() -> (StatusCode, Value) {
+    let app = api_router();
+    let response = app
+        .oneshot(
+            Request::builder()
+                .uri("/version")
+                .body(axum::body::Body::empty())
+                .unwrap(),
+        )
+        .await
+        .unwrap();
+    let status = response.status();
+    let body = to_bytes(response.into_body(), 1024).await.unwrap();
+    let parsed: Value = serde_json::from_slice(&body).unwrap();
+    (status, parsed)
+}
+
+#[tokio::test]
+async fn test_version_endpoint_returns_200() {
+    let (status, _) = fetch_version_body().await;
+    assert_eq!(status, StatusCode::OK);
+}
+
+#[tokio::test]
+async fn test_version_returns_valid_json_with_version_field() {
+    let (_, body) = fetch_version_body().await;
+    let v = body.get("version").and_then(Value::as_str);
+    assert!(v.is_some(), "version field missing or not a string: {body:?}");
+    assert!(!v.unwrap().is_empty(), "version field is empty");
+}
+
+#[tokio::test]
+async fn test_version_matches_cargo() {
+    // f1.a3: response.version equals Cargo.toml version at build time.
+    let (_, body) = fetch_version_body().await;
+    let expected = env!("CARGO_PKG_VERSION");
+    assert_eq!(body["version"].as_str(), Some(expected));
+}
```

### 4.2 reports/t2.test.json

**`missions/m-2026-05-25-001/reports/t2.test.json`**

```json
{
  "task_id": "t2",
  "cargo_check": {"exit_code": 0, "warnings": 0},
  "cargo_test": {"exit_code": 0, "passed": 27, "failed": 0, "ignored": 0},
  "cargo_clippy": {"exit_code": 0, "warnings": 0, "errors": 0},
  "cargo_fmt": {"exit_code": 0, "needs_format": false},
  "cargo_nextest": {"exit_code": 0, "passed": 27, "failed": 0},
  "doc_test": {"exit_code": 0, "passed": 0},
  "additional_commands": []
}
```

Note: 27 tests = 24 pre-existing + 3 new. Coder explicitly verifies all 24 still pass (the `f1.a4` "no regression" assertion).

### 4.3 handoff/t2.json — note v3.1 completeness rule

**`missions/m-2026-05-25-001/handoff/t2.json`**

```json
{
  "task_id": "t2",
  "completed": [
    "Created crates/my_api/src/routes/version.rs implementing GET /version endpoint via env!(CARGO_PKG_VERSION)",
    "Edited crates/my_api/src/routes/mod.rs: added 'pub mod version' and '.nest(\"/version\", version::router())'",
    "Created crates/my_api/tests/version_test.rs with 3 integration tests covering f1.a1, f1.a2, f1.a3",
    "Verified all 24 pre-existing tests still pass (f1.a4)"
  ],
  "incomplete": [],
  "commands_run": [
    {"command": "git checkout -- .", "exit_code": 0, "summary": "worktree clean before task start"},
    {"command": "cargo check --workspace --all-targets --all-features", "exit_code": 0, "summary": "clean"},
    {"command": "cargo test --workspace", "exit_code": 0, "summary": "27 passed (24 pre-existing + 3 new), 0 failed"},
    {"command": "cargo clippy --workspace --all-targets --all-features -- -D warnings", "exit_code": 0, "summary": "clean"},
    {"command": "cargo fmt --check", "exit_code": 0, "summary": "clean"},
    {"command": "cargo nextest run --workspace --all-features", "exit_code": 0, "summary": "27 passed"}
  ],
  "issues_discovered": [
    "Existing crates/my_api/src/lib.rs re-exports 'routes' module but does NOT re-export individual route modules. Followed this convention — version module is not separately re-exported. If a future task needs to call version::router() from outside the crate, lib.rs will need a follow-up edit."
  ],
  "deviations_from_plan": [
    "Plan implied using cargo_metadata for version access; switched to env!(CARGO_PKG_VERSION) per research_notes/api_routing.md recommendation. Functionally identical, zero runtime cost, no new dependency."
  ],
  "contract_coverage": [
    {"assertion_id": "f1.a1", "covered": false, "location": null, "reason_if_uncovered": "Requires runtime probe; deferred to BehaviorValidator t5 per task spec"},
    {"assertion_id": "f1.a2", "covered": false, "location": null, "reason_if_uncovered": "Requires runtime probe; deferred to BehaviorValidator t5 per task spec"},
    {"assertion_id": "f1.a3", "covered": true, "location": "crates/my_api/tests/version_test.rs::test_version_matches_cargo", "reason_if_uncovered": null},
    {"assertion_id": "f1.a4", "covered": true, "location": "crates/my_api/tests (24/24 pre-existing pass)", "reason_if_uncovered": null}
  ],
  "dependency_changes": [],
  "unsafe_usage": [],
  "next_recommended_action": "send_to_review_validator"
}
```

**v3.1 completeness rule check**: `issues_discovered` and `deviations_from_plan` are both non-empty. `triggers_second_pass` evaluates to `false`. ReviewValidator will run the hardcoded-test sub-agent (always) but not the completeness second-pass sub-agent.

**Note on `f1.a1` and `f1.a2`**: Coder marks them `covered: false` with `reason_if_uncovered` explaining why this is correct (not a failure). They're behavior-probe assertions; coverage is by t5, not t2.

---

## 5. Validators run in parallel (t3 + t4)

Per the DAG and concurrency rules in ARCHITECTURE.md §3, Security Worker (t3) is read-only and parallelizes with ReviewValidator (t4). The scheduler dispatches both as soon as t2 completes.

### 5.1 verdicts/t3.security.json

**`missions/m-2026-05-25-001/verdicts/t3.security.json`**

```json
{
  "task_id": "t3",
  "findings": [],
  "critical_count": 0,
  "high_count": 0,
  "blocks_pr": false,
  "created_at": "2026-05-25T09:54:12Z"
}
```

Security Worker ran `cargo audit`, `cargo deny check`, `cargo geiger`, `gitleaks detect`, `trufflehog scan` — all clean (no new deps, no new unsafe, no secrets). Brief verdict.

### 5.2 security_notes/t3.md

**`missions/m-2026-05-25-001/security_notes/t3.md`**

```markdown
# Security Audit: Task t2

## Scope

Patch `patches/t2.diff` introduces:
- New file: crates/my_api/src/routes/version.rs (32 lines)
- New file: crates/my_api/tests/version_test.rs (52 lines)
- Edit: crates/my_api/src/routes/mod.rs (+2 lines)

No Cargo.toml or Cargo.lock changes.

## Tools run

| Tool | Exit | Findings |
|---|---|---|
| cargo audit | 0 | No advisories |
| cargo deny check | 0 | All bans / licenses / sources pass |
| cargo geiger | 0 | unsafe count unchanged (0 in this crate) |
| gitleaks detect | 0 | No secrets in patch |
| trufflehog scan | 0 | No secrets in patch |

## Findings

None.

## Notes

Patch uses `env!("CARGO_PKG_VERSION")` which expands at compile time to a constant string. No runtime configuration surface, no input handling, no SQL or path traversal vectors. The endpoint returns a hardcoded shape (single JSON field) — no information disclosure risk beyond exposing the public crate version, which is intentional.
```

### 5.3 verdicts/t4.review.json — PASS case

ReviewValidator runs the full sequence per `prompts/review_validator.md`:

1. Apply patch in fresh worktree — clean
2. Cargo gates — all pass
3. Per-assertion coverage check — f1.a3 + f1.a4 verified; f1.a1 + f1.a2 pending (probe responsibility)
4. Handoff completeness check — `triggers_second_pass = false` (issues_discovered + deviations_from_plan non-empty)
5. Hardcoded-test sub-agent — runs every review

**`missions/m-2026-05-25-001/verdicts/t4.review.json`**

```json
{
  "task_id": "t4",
  "result": "pass",
  "precise_reason": "",
  "next_action_recommendation": "dispatch_behavior_validator_t5",
  "cargo_gate_results": {
    "build": true,
    "test": true,
    "clippy": true,
    "fmt": true,
    "nextest": true,
    "doc_test": true
  },
  "assertion_results": [
    {"assertion_id": "f1.a1", "result": "pending", "detail": "Behavior probe required (verification_method=behavior_probe); deferred to t5"},
    {"assertion_id": "f1.a2", "result": "pending", "detail": "Behavior probe required (verification_method=behavior_probe); deferred to t5"},
    {"assertion_id": "f1.a3", "result": "pass", "detail": "verified at crates/my_api/tests/version_test.rs::test_version_matches_cargo (3 attempts, all pass)"},
    {"assertion_id": "f1.a4", "result": "pass", "detail": "Pre-existing 24-test suite intact (cargo test --workspace shows 24 + 3 new = 27 pass)"}
  ],
  "triggered_second_pass": false,
  "adversarial_findings": [],
  "hardcoded_test_warnings": [],
  "created_at": "2026-05-25T09:58:33Z"
}
```

### 5.4 review_notes/t4.md

**`missions/m-2026-05-25-001/review_notes/t4.md`**

```markdown
# Review of Task t2

**Verdict**: PASS

## Cargo gates

- [x] build
- [x] test (27 passed: 24 pre-existing + 3 new, 0 ignored)
- [x] clippy (0 warnings)
- [x] fmt
- [x] nextest (27 passed)
- [x] doc test (0 doc tests in patch, OK)

## Contract coverage

| Assertion | Result | Location |
|---|---|---|
| f1.a1 | pending | Behavior probe (t5) |
| f1.a2 | pending | Behavior probe (t5) |
| f1.a3 | ✓ pass | tests/version_test.rs::test_version_matches_cargo |
| f1.a4 | ✓ pass | 24/24 pre-existing tests pass |

## Second-pass status

Handoff completeness rule: **not triggered**. Coder's handoff lists 1 issue_discovered and 1 deviation_from_plan; the field is doing what it's supposed to do.

## Hardcoded-test analysis (v3.1 sub-agent)

Sub-agent spawned with patch + contract + research notes; NO handoff content.

- `test_version_endpoint_returns_200` — asserts `StatusCode::OK`, no body-content check. Intent-oriented (verifies endpoint reachability, not a specific shape). **PASS.**
- `test_version_returns_valid_json_with_version_field` — asserts the field exists and is a non-empty string, NOT a specific value. Intent-oriented. **PASS.**
- `test_version_matches_cargo` — compares response to `env!("CARGO_PKG_VERSION")`, which is the spec-defined source of truth (contract f1.a3 explicitly says "equals the my_api crate version declared in Cargo.toml at build time"). Using env! is the correct way to test against the source of truth. **PASS — not a hardcoded test.**

## Recommendation

Dispatch BehaviorValidator t5 to verify f1.a1 + f1.a2 via runtime probe against a running service.
```

---

## 6. BehaviorValidator task (t5)

Triggered only because t4 returned PASS. Starts the service in the sandbox, hits it with HTTP probes, captures evidence.

### 6.1 verdicts/t5.behavior.json — PASS

**`missions/m-2026-05-25-001/verdicts/t5.behavior.json`**

```json
{
  "task_id": "t5",
  "result": "pass",
  "probe_strategy": "backend_service_health_probe",
  "observations": [
    {"assertion_id": "f1.a1", "observed": "200 OK", "expected": "200", "matched": true},
    {"assertion_id": "f1.a2", "observed": "{\"version\": \"0.4.7\"}", "expected": "JSON with string field 'version'", "matched": true}
  ],
  "evidence_path": "behavior_evidence/t5/",
  "failure_reason": null,
  "created_at": "2026-05-25T10:04:17Z"
}
```

### 6.2 behavior_trace/t5.md

**`missions/m-2026-05-25-001/behavior_trace/t5.md`**

```markdown
# Behavior Validation Trace: Task t5

## Probe strategy

`backend_service_health_probe` (from project_profile.behavior_probe).

## Execution

1. **Start service** — `cargo run --bin my_api` (in sandbox; bound to 127.0.0.1:8080)
   - Service ready in 6.3s (after compile)
   - Ready check: `curl -sf http://localhost:8080/api/v1/health` returned 200 with `{"status": "ok"}`

2. **Probe f1.a1** — `GET http://localhost:8080/api/v1/version`
   - Response status: 200
   - Latency: 1.2ms
   - Result: matched expected (200)

3. **Probe f1.a2** — `GET http://localhost:8080/api/v1/version` (same call, body inspected)
   - Response body: `{"version":"0.4.7"}`
   - Content-Type: `application/json`
   - JSON parses successfully
   - `version` field present, type string, non-empty
   - Result: matched expected shape

4. **Regression check** — probe existing endpoints to ensure no side effects:
   - `GET /api/v1/health` → 200 `{"status": "ok"}` ✓
   - `GET /api/v1/users` → 200 (empty array, expected for default test fixture) ✓

5. **Stop service** — clean shutdown after probe complete

## Evidence files saved

- `behavior_evidence/t5/probe_a1.json` — raw HTTP response for f1.a1
- `behavior_evidence/t5/probe_a2.json` — raw HTTP response for f1.a2
- `behavior_evidence/t5/regression_health.json`
- `behavior_evidence/t5/regression_users.json`
- `behavior_evidence/t5/service_stdout.log` — full service stdout during probe window
- `behavior_evidence/t5/service_stderr.log`

## Verdict

PASS. Both target assertions matched. No side effects on existing endpoints.
```

---

## 7. Checkpoint creation

After all validators pass for milestone m1, Mission Driver creates a checkpoint.

### 7.1 What happens

```
1. git tag mission/m-2026-05-25-001/m1 (inside sandbox)
2. docker commit <container_id> maf-coder-snapshot:m-2026-05-25-001-m1
3. Copy contents of missions/m-2026-05-25-001/ (except checkpoints/) into 
   missions/m-2026-05-25-001/checkpoints/m1/
4. Write Checkpoint artifact
5. Update mission_state.json
6. Emit CHECKPOINT_CREATED event
```

### 7.2 checkpoints/m1/checkpoint.json

**`missions/m-2026-05-25-001/checkpoints/m1/checkpoint.json`**

```json
{
  "mission_id": "m-2026-05-25-001",
  "milestone_id": "m1",
  "git_tag": "mission/m-2026-05-25-001/m1",
  "sandbox_snapshot_id": "sha256:7c4f9a3b8e2d1f56a9c3e0b2d4f8e6a1b9c5d3e7f0a2b4d6e8a1c3e5f9d7b2a4",
  "artifact_archive_path": "checkpoints/m1/archive/",
  "cumulative_cost_usd": 0.87,
  "cumulative_wall_clock_hours": 0.42,
  "created_at": "2026-05-25T10:06:01Z"
}
```

### 7.3 Updated mission_state.json

**`missions/m-2026-05-25-001/mission_state.json`** (after t5 + checkpoint)

```json
{
  "mission_id": "m-2026-05-25-001",
  "started_at": "2026-05-25T09:00:00Z",
  "current_milestone": null,
  "completed_milestones": ["m1"],
  "cumulative_cost_usd": 0.87,
  "cumulative_wall_clock_hours": 0.42,
  "cumulative_tokens": 156432,
  "last_status_report_at": "2026-05-25T09:30:00Z",
  "last_checkpoint_at": "2026-05-25T10:06:01Z",
  "coder_provider_in_use": "anthropic",
  "last_user_message_processed_at": null
}
```

`current_milestone: null` because m1 is complete; mission is heading to finalization (PR creation).

---

## 8. Final mission artifacts

### 8.1 final_answer.md

**`missions/m-2026-05-25-001/final_answer.md`**

```markdown
# Mission Complete: m-2026-05-25-001

## What was delivered

GET /api/v1/version endpoint in crates/my_api:
- New file: src/routes/version.rs (Json response wrapping env!(CARGO_PKG_VERSION))
- Edit: src/routes/mod.rs (router registration)
- New tests file: tests/version_test.rs (3 integration tests)

## Validation results

All four contract assertions verified:
- f1.a1 (200 status) — verified by BehaviorValidator runtime probe
- f1.a2 (JSON shape) — verified by BehaviorValidator runtime probe
- f1.a3 (matches Cargo.toml) — verified by integration test test_version_matches_cargo
- f1.a4 (no regression) — verified by integration test (24/24 pre-existing pass)

## Cost and time

- Total API cost: $0.87
- Wall-clock: 25 minutes
- Tokens: 156k input + 14k output

## PR created

https://github.com/john/my-api-repo/pull/142

Branch: mission/m-2026-05-25-001
```

### 8.2 mission_retro.md

**`missions/m-2026-05-25-001/mission_retro.md`**

```markdown
# Mission Retrospective: m-2026-05-25-001

## What worked

- Research worker correctly identified env!(CARGO_PKG_VERSION) as the right approach before Coder started — saved an exploration cycle in Coder
- Coder handoff was complete (issues_discovered + deviations_from_plan both populated); no second-pass needed
- Validators in parallel (t3 security + t4 review): saved ~3min wall-clock
- Behavior probe found nothing unexpected; the integration test in t2 had already covered the substantive content

## What surprised us

- Nothing significant. This was a simple mission and executed close to plan.

## Cost analysis

| Role | LLM cost | % |
|---|---|---|
| orchestrator | $0.18 | 21% |
| research_worker | $0.21 | 24% |
| coder_worker | $0.31 | 36% |
| security_worker | $0.04 | 5% |
| review_validator | $0.09 | 10% |
| behavior_validator | $0.02 | 2% |
| adversarial_subagent | $0.02 | 2% |
| **total** | **$0.87** | **100%** |

## Global lesson candidate

**Lesson**: For Rust binaries, `env!("CARGO_PKG_VERSION")` is the canonical way to expose the running crate's version at runtime. It is preferable to runtime cargo_metadata invocations (which require cargo in PATH and fork a process) or build.rs generation (which adds build-time complexity).

**Apply to**: any future mission whose goal includes "expose version" or "introspect own metadata" for Rust services.

**Confidence**: high; this is a well-known idiom.

**global_lesson: yes**

## Process notes for next mission

- Plan said "lookup version via cargo metadata"; Coder deviated based on Research's recommendation. This is the right outcome but the Plan should have asked Research about the access method BEFORE locking the contract. For future similar missions, Orchestrator should pose the access-method question to Research during planning rather than committing the plan to a specific approach.
```

### 8.3 PR description on GitHub

When Orchestrator runs `gh pr create`, the body is generated from the template in `prompts/orchestrator.md` §9, populated from artifacts:

```markdown
# m-2026-05-25-001: Add GET /api/v1/version endpoint

> Auto-generated by MAF-Coder. Review the checklist below before merging.

## What changed

- New file: `crates/my_api/src/routes/version.rs` (32 lines) — Implements GET /version returning JSON `{"version": "<crate version>"}`
- Edit: `crates/my_api/src/routes/mod.rs` (+2 lines) — Registers the new route
- New file: `crates/my_api/tests/version_test.rs` (52 lines) — 3 integration tests for f1.a1, f1.a2, f1.a3

## Validation Contract Coverage

- [x] **f1.a1**: HTTP GET /api/v1/version returns status 200 — verified by BehaviorValidator runtime probe
- [x] **f1.a2**: Response body is valid JSON with field 'version' of type string — verified by BehaviorValidator runtime probe
- [x] **f1.a3**: Response 'version' field equals my_api crate version at build time — verified by `tests/version_test.rs::test_version_matches_cargo`
- [x] **f1.a4**: No regression on /api/v1/health or /api/v1/users — verified by 24/24 pre-existing tests passing

## Validator Verdicts

- **ReviewValidator**: PASS (cargo test 27/27, clippy 0 warn, fmt clean, doc test 0 ok)
- **BehaviorValidator**: PASS (health probe ok, /version returns 200 with {"version": "0.4.7"})
- **Security**: 0 findings — `verdicts/t3.security.json` clean

## Review Checklist for Human

- [ ] Business semantics actually match what you wanted
- [ ] No surprising deviation in `crates/my_api/Cargo.toml` (none expected — no dep changes)
- [ ] Integration test pattern matches your team conventions (uses tower::ServiceExt::oneshot, same as existing tests)
- [ ] No unintended exposure of internal data through the new endpoint (only the public crate version)

## Mission Artifacts

- Plan: missions/m-2026-05-25-001/plan.md
- Contract: missions/m-2026-05-25-001/validation_contract.yaml (locked)
- Retro: missions/m-2026-05-25-001/mission_retro.md
- Events: missions/m-2026-05-25-001/events.jsonl

## Cost & Time

- API cost: $0.87
- Wall-clock: 25 minutes
- Tokens: 156k input + 14k output

🤖 Generated with MAF-Coder
```

---

## 9. FAIL case examples

The happy path above is the most common. But the schemas + verdicts must also cleanly represent failures. Three failure modes:

### 9.1 ReviewValidator FAIL — clippy regression

Suppose Coder's patch left a clippy warning. ReviewValidator catches it:

**`verdicts/t4.review.json`** (FAIL case)

```json
{
  "task_id": "t4",
  "result": "fail",
  "precise_reason": "cargo clippy failed: crates/my_api/src/routes/version.rs:18 — error: useless conversion (clippy::useless_conversion). The Coder's handoff claimed clippy clean but it is not. Specific error: 'env!(\"CARGO_PKG_VERSION\").to_string()' — env! returns &str; calling .to_string() is necessary for Serialize but clippy flags it because the field type could be &'static str. Either accept the conversion (silence the lint with rationale) or change field type to Cow<'static, str>.",
  "next_action_recommendation": "coder_fix_clippy_warning_at_routes_version_rs_line_18",
  "cargo_gate_results": {
    "build": true,
    "test": true,
    "clippy": false,
    "fmt": true,
    "nextest": true,
    "doc_test": true
  },
  "assertion_results": [
    {"assertion_id": "f1.a1", "result": "pending", "detail": "Not reached due to clippy failure"},
    {"assertion_id": "f1.a2", "result": "pending", "detail": "Not reached due to clippy failure"},
    {"assertion_id": "f1.a3", "result": "pending", "detail": "Not reached due to clippy failure"},
    {"assertion_id": "f1.a4", "result": "pending", "detail": "Not reached due to clippy failure"}
  ],
  "triggered_second_pass": false,
  "adversarial_findings": [],
  "hardcoded_test_warnings": [],
  "created_at": "2026-05-25T09:58:33Z"
}
```

**What the Orchestrator does next** (per stuck recovery §7 of ARCHITECTURE.md):

- Task t2 has `retry_budget: 2`. Current retry count: 0.
- Risk class: LOW (cargo gate failure with precise location).
- Action: re-dispatch t2 with adjusted context (the `precise_reason` from the verdict is included in the new task's `background`).
- Emit `TASK_FAILED` event with reason="clippy_failure_at_version_rs_18" and `will_retry: true`.

### 9.2 Hardcoded test warning — PARTIAL

Suppose Coder wrote a test like:

```rust
#[tokio::test]
async fn test_version_matches_cargo() {
    let (_, body) = fetch_version_body().await;
    assert_eq!(body["version"].as_str(), Some("0.4.7"));  // ← hardcoded!
}
```

The adversarial sub-agent flags this:

**`verdicts/t4.review.json`** (PARTIAL case)

```json
{
  "task_id": "t4",
  "result": "partial",
  "precise_reason": "Hardcoded test on critical assertion f1.a3 detected. tests/version_test.rs::test_version_matches_cargo asserts the literal string '0.4.7' rather than the spec-defined source of truth env!(CARGO_PKG_VERSION). A wrong implementation that happens to return '0.4.7' for unrelated reasons would pass this test, defeating the assertion's purpose.",
  "next_action_recommendation": "coder_rewrite_test_version_matches_cargo_to_use_env_macro_as_expected",
  "cargo_gate_results": {
    "build": true,
    "test": true,
    "clippy": true,
    "fmt": true,
    "nextest": true,
    "doc_test": true
  },
  "assertion_results": [
    {"assertion_id": "f1.a1", "result": "pending", "detail": "Behavior probe required (verification_method=behavior_probe)"},
    {"assertion_id": "f1.a2", "result": "pending", "detail": "Behavior probe required (verification_method=behavior_probe)"},
    {"assertion_id": "f1.a3", "result": "partial", "detail": "Test exists but uses hardcoded value '0.4.7' instead of env!(CARGO_PKG_VERSION). See hardcoded_test_warnings."},
    {"assertion_id": "f1.a4", "result": "pass", "detail": "24/24 pre-existing pass"}
  ],
  "triggered_second_pass": false,
  "adversarial_findings": [],
  "hardcoded_test_warnings": [
    "tests/version_test.rs::test_version_matches_cargo uses assert_eq!(body['version'].as_str(), Some(\"0.4.7\")) — should use env!(\"CARGO_PKG_VERSION\") which is the contract's defined source of truth for f1.a3"
  ],
  "created_at": "2026-05-25T09:58:33Z"
}
```

Result: PARTIAL (not FAIL) because the cargo gates pass and one assertion is genuinely covered. But Orchestrator re-dispatches t2 with the specific rewriting requirement.

### 9.3 Second-pass triggered — empty handoff

Suppose Coder produced a handoff with all three optional fields empty:

```json
{
  "task_id": "t2",
  "completed": [...],
  "incomplete": [],
  "commands_run": [...],
  "issues_discovered": [],
  "deviations_from_plan": [],
  ...
}
```

`Handoff.triggers_second_pass` returns `true`. ReviewValidator's flow changes:

1. All previous steps unchanged
2. After hardcoded-test sub-agent, runs a **second-pass sub-agent** with fresh context (no handoff text, no review reasoning):

```json
{
  "purpose": "completeness_second_pass",
  "inputs": {
    "patch_path": "patches/t2.diff",
    "contract_path": "validation_contract.yaml",
    "research_refs": ["research_notes/api_routing.md", "code_map/my_api.md"]
  }
}
```

Sub-agent reads the patch independently and outputs concerns. If it surfaces real issues that the Coder didn't flag, the verdict moves to PARTIAL.

**Sample event** when this happens:

```json
{"timestamp":"2026-05-25T09:55:01Z","kind":"second_pass_triggered","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t4","actor":"review_validator","payload":{"reason":"handoff has empty incomplete/issues/deviations"}}
```

---

## 10. Escalation example — Human Gate

Suppose during planning the Orchestrator discovers the goal is ambiguous: the user said "return the running crate's version" but the workspace has multiple crates. Does the user mean my_api (the binary) or the workspace-level virtual manifest's version?

Orchestrator writes:

**`missions/m-2026-05-25-001/user_messages/_pending_2026-05-25-090012.md`**

```markdown
# Pending Human Decision: Mission m-2026-05-25-001

## What I'm asking

The goal says "return the running crate's version" but the workspace has two crates:
- `my_api` (binary, version 0.4.7) — the service that runs
- `my_api_core` (library, version 0.2.3) — internal library the service depends on

Per principle of least surprise, I read "running crate's version" as the binary's version (0.4.7), since the user is asking about a service endpoint. But this is a planning-phase ambiguity worth confirming before locking the validation contract.

## Options

1. **Use my_api version (0.4.7)** — my recommended default. The endpoint exposes the deployable artifact's version. This is the typical convention.
2. **Use workspace virtual manifest version** — workspace has no version field at the top level (only individual crates have versions). This option is not actually available; flagging for completeness.
3. **Use my_api_core version (0.2.3)** — if the user thinks of the "core" as the source of truth. Unusual for a service endpoint.
4. **Expose all crate versions in the response body** — out of spec, more info than asked for. Likely no.

## What I will do without a response

After 24 hours of no response, I will proceed with option 1 (my_api version) and document this choice in the contract's risk_acknowledgements. The mission will pause now.

## How to respond

Create `missions/m-2026-05-25-001/user_messages/_pending_2026-05-25-090012.response.md` with a body like:

\`\`\`
Option chosen: 1
\`\`\`

I'll pick it up at the next milestone-boundary poll (which is now, since I'm waiting on you).
```

After user creates the response file:

**`missions/m-2026-05-25-001/user_messages/_pending_2026-05-25-090012.response.md`**

```markdown
Option chosen: 1

Also: please make the response include `crate_name` field too so it's unambiguous which crate's version is reported.
```

Orchestrator processes the response, updates the validation_contract.yaml (adding f1.a5 for crate_name field, with Human Gate authorization noted), moves the file to processed_messages/, and continues planning.

Events:
```json
{"timestamp":"2026-05-25T09:00:30Z","kind":"escalation_triggered","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","payload":{"target":"human_gate","reason":"ambiguous_goal_during_planning","options_count":4}}
{"timestamp":"2026-05-25T09:00:35Z","kind":"user_message_received","mission_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"message_path":"_pending_2026-05-25-090012.response.md","urgent":false}}
{"timestamp":"2026-05-25T09:00:35Z","kind":"user_message_processed","mission_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"message_path":"_pending_2026-05-25-090012.response.md","outcome":"contract_extended_with_f1_a5"}}
```

---

## 11. Sample events.jsonl

Excerpt from the actual mission's event log (first 20 events of the happy path):

**`missions/m-2026-05-25-001/events.jsonl`**

```jsonl
{"timestamp":"2026-05-25T09:00:00Z","kind":"mission_start","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","payload":{"goal":"Add a GET /api/v1/version endpoint...","repo":"/Users/john/code/my-api-repo","expected_budget_usd":5.0}}
{"timestamp":"2026-05-25T09:00:08Z","kind":"artifact_written","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"path":"project_profile.yaml"}}
{"timestamp":"2026-05-25T09:00:23Z","kind":"llm_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"model":"anthropic/claude-opus-4-7","tokens_in":3421,"tokens_out":1872,"cost_usd":0.0814,"latency_sec":12.3,"fallback_used":false}}
{"timestamp":"2026-05-25T09:00:24Z","kind":"artifact_written","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"path":"plan.md"}}
{"timestamp":"2026-05-25T09:00:51Z","kind":"llm_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"model":"anthropic/claude-opus-4-7","tokens_in":4108,"tokens_out":2104,"cost_usd":0.0927,"latency_sec":14.1,"fallback_used":false}}
{"timestamp":"2026-05-25T09:00:52Z","kind":"artifact_written","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"path":"validation_contract.yaml"}}
{"timestamp":"2026-05-25T09:01:14Z","kind":"artifact_written","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","actor":"orchestrator","payload":{"path":"tasks.yaml"}}
{"timestamp":"2026-05-25T09:01:14Z","kind":"task_dispatched","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"orchestrator","payload":{"owner":"research_worker","priority":"medium"}}
{"timestamp":"2026-05-25T09:01:18Z","kind":"tool_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"research_worker","payload":{"tool":"cargo_metadata","args_summary":"","exit_code":0,"duration_sec":0.8}}
{"timestamp":"2026-05-25T09:01:23Z","kind":"tool_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"research_worker","payload":{"tool":"glob","args_summary":"pattern=crates/my_api/src/routes/*","exit_code":0,"duration_sec":0.1}}
{"timestamp":"2026-05-25T09:01:34Z","kind":"llm_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"research_worker","payload":{"model":"anthropic/claude-sonnet-4-6","tokens_in":2103,"tokens_out":1456,"cost_usd":0.0354,"latency_sec":8.7,"fallback_used":false}}
{"timestamp":"2026-05-25T09:02:01Z","kind":"artifact_written","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"research_worker","payload":{"path":"research_notes/api_routing.md"}}
{"timestamp":"2026-05-25T09:02:03Z","kind":"artifact_written","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"research_worker","payload":{"path":"code_map/my_api.md"}}
{"timestamp":"2026-05-25T09:02:05Z","kind":"task_complete","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t1","actor":"research_worker","payload":{"duration_sec":51.2}}
{"timestamp":"2026-05-25T09:02:05Z","kind":"task_dispatched","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t2","actor":"orchestrator","payload":{"owner":"coder_worker","priority":"high"}}
{"timestamp":"2026-05-25T09:02:08Z","kind":"tool_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t2","actor":"coder_worker","payload":{"tool":"git_checkout","args_summary":"target=--","exit_code":0,"duration_sec":0.1}}
{"timestamp":"2026-05-25T09:02:15Z","kind":"tool_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t2","actor":"coder_worker","payload":{"tool":"cargo_check","args_summary":"","exit_code":0,"duration_sec":4.3}}
{"timestamp":"2026-05-25T09:02:20Z","kind":"tool_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t2","actor":"coder_worker","payload":{"tool":"read_file","args_summary":"path=crates/my_api/src/routes/mod.rs","exit_code":0,"duration_sec":0.05}}
{"timestamp":"2026-05-25T09:02:32Z","kind":"llm_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t2","actor":"coder_worker","payload":{"model":"anthropic/claude-sonnet-4-6","tokens_in":8211,"tokens_out":3127,"cost_usd":0.1284,"latency_sec":16.4,"fallback_used":false}}
{"timestamp":"2026-05-25T09:02:51Z","kind":"tool_call","mission_id":"m-2026-05-25-001","trace_id":"m-2026-05-25-001","task_id":"t2","actor":"coder_worker","payload":{"tool":"write_file","args_summary":"path=crates/my_api/src/routes/version.rs","exit_code":0,"duration_sec":0.08}}
```

(Total: ~140 events for the full mission. Aggregated via `EventLog.total_cost_usd()` etc. for the retro.)

---

## 12. Cross-reference table

For each artifact in this document, where to find its schema, prompt rules, and writing tool:

| Artifact | Pydantic schema | Written by | Tool function |
|---|---|---|---|
| `project_profile.yaml` | `ProjectProfile` in `schemas/profile.py` | project_profiler (utility, not an agent) | `save_project_profile` on ArtifactStore |
| `plan.md` | None (freeform markdown) | Orchestrator | `save_artifact` |
| `validation_contract.yaml` | `ValidationContract` in `schemas/contract.py` | Orchestrator (once, locks) | `save_validation_contract` |
| `tasks.yaml` | List of `Task` in `schemas/task.py` | Orchestrator | `save_artifact` |
| `research_notes/*.md`, `code_map/*.md` | None (freeform markdown) | Research Worker | `save_research_note`, `save_code_map` |
| `patches/<task_id>.diff` | None (unified diff format) | Coder Worker | `save_patch` |
| `reports/<task_id>.test.json` | (untyped TestReport dict, fields documented in prompt) | Coder Worker | `save_test_report` |
| `handoff/<task_id>.json` | `Handoff` in `schemas/handoff.py` | Coder Worker (also other Workers eventually) | `save_handoff` |
| `verdicts/<task_id>.review.json` | `ReviewVerdict` in `schemas/verdict.py` | ReviewValidator | `save_review_verdict` |
| `verdicts/<task_id>.behavior.json` | `BehaviorVerdict` in `schemas/verdict.py` | BehaviorValidator | `save_behavior_verdict` |
| `verdicts/<task_id>.security.json` | `SecurityVerdict` in `schemas/verdict.py` | Security Worker | `save_security_verdict` |
| `review_notes/<task_id>.md` | None (freeform markdown) | ReviewValidator | `save_review_notes` |
| `behavior_trace/<task_id>.md` | None (freeform markdown) | BehaviorValidator | `save_artifact` (no specialized tool) |
| `security_notes/<task_id>.md` | None (freeform markdown) | Security Worker | `save_artifact` |
| `status_reports/status_<N>.{md,json}` | `StatusReport` in `schemas/lifecycle.py` | Mission Driver background task | `save_status_report` |
| `checkpoints/<milestone_id>/checkpoint.json` | `Checkpoint` in `schemas/lifecycle.py` | Mission Driver | `create_checkpoint` |
| `mission_state.json` | `MissionState` in `schemas/lifecycle.py` | Mission Driver (continuous updates) | `save_mission_state` / `update_mission_state` |
| `events.jsonl` | `Event` in `blackboard/event_log.py` | Everything (append-only) | `EventLog.append` or any `log_*` helper |
| `user_messages/_pending_*.md` | None (freeform markdown with header conventions) | Orchestrator (escalation) | `escalate_to_human_gate` |
| `final_answer.md`, `mission_retro.md` | None (freeform markdown) | Orchestrator (mission end) | `save_artifact` |

This table is **canonical**. If you find an artifact in the codebase that's not here, that's a sign either (a) it's an internal/temporary artifact not part of the framework's public surface or (b) this document needs an update.

---

## 13. Phasing note

This example demonstrates capabilities across multiple Phases of the Build Plan:

- Phase B: Orchestrator + Coder + ReviewValidator + Project Profiler + CLI
- Phase C: Research Worker, Security Worker
- Phase D: BehaviorValidator
- Phase E: Checkpoints, Status Reports (not shown in detail above but mission_state.json + checkpoint.json are there)

For Phase B, only sections §1, §2, §4, §5.3, §5.4 (the Orchestrator+Coder+ReviewValidator path) are fully exercisable. Sections §3 (Research), §5.1-§5.2 (Security), §6 (BehaviorValidator), §7 (Checkpoint), §10 (Escalation) describe the eventual end-state — Cursor should use them as targets, not as Phase B's deliverables.

The cross-reference table in §12 IS valid for Phase B implementation — every artifact has its schema and tool defined.
