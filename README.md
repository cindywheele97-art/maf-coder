# MAF-Coder

Multi-Agent Framework for Coder — a production-grade agent team that runs autonomous **Rust** coding missions.

> **Status: Phases A–F code-complete. Phase G (real-world validation) in progress.**
> The framework is fully implemented — orchestrator, workers, dual validators, sandbox,
> multi-day lifecycle, cross-mission memory, and PR workflow all ship with tests. What
> remains is **operator-gated live validation**: running real missions against real Rust
> repos with real API keys. See `MAF-Coder_v2_Build_Plan.md` for the roadmap and
> `agent_team_soul_v3.1.md` for the framework constitution.

**Meta-context that's easy to confuse:** this is a **Python** project that *builds agents
which operate on Rust codebases*. When you work in this repo you are writing Python
(orchestrator, workers, validators, schemas). The Rust knowledge lives in `prompts/` as
content future agents read — it is not your working environment.

## For AI coding agents (Claude Code, Cursor, etc.)

This project has both `AGENTS.md` and `CLAUDE.md` at root with project-specific
instructions, plus `.claude/settings.json` and `.cursorignore` with conservative
permission/ignore rules. AI agents load these automatically when started in the project
directory. The two filenames are kept in sync (Cursor reads `AGENTS.md` natively; Claude
Code prefers `CLAUDE.md`) — edit either, mirror to the other.

The full design package:

- `ARCHITECTURE.md` — system shape: components, lifecycles, the design-decisions log (the "what")
- `AGENT_TOOLS_SPEC.md` — formal signatures: BaseAgent, TaskContext, the tool catalogue, Scheduler, MissionDriver (the "how")
- `WORKED_EXAMPLE.md` — a complete end-to-end mission walkthrough with sample artifacts at every step (the "looks like this")
- `agent_team_soul_v3.1.md` — framework constitution (roles, contracts, escalation)
- `MAF-Coder_v2_Build_Plan.md` — phased delivery roadmap (A–G)
- `docs/MAF_CODER_EXECUTION_PLAN.md` — per-PR breakdown and the metrics definitions
- `docs/FIRST_RUN_RUNBOOK.md` — how to drive the first live shakedown mission
- `prompts/*.md` — agent behavior contracts (treat as production code, not docs)

## The agent team

A mission is driven by an **Orchestrator** that plans the work, locks a validation
contract, and dispatches a task DAG to specialist workers, each gated by a two-stage
validator chain:

- **Orchestrator** — plans, drafts the validation contract, routes the task DAG, writes status reports, escalates, opens the PR.
- **Coder Worker** — Rust implementation under sandbox, with idempotent writes and a structured completeness-checked handoff.
- **Research Worker** — gathers external context through the sanitizer + egress-logged fetch path.
- **Security Worker** — SAST/secret review of produced changes.
- **Review Validator** (+ adversarial sub-agent) — adversarial code review, cargo gate, handoff-completeness check, hardcoded-test detection.
- **Behavior Validator** — runs probe strategies (build / unit / property / WASM-node / CLI) that catch logic bugs review misses. Runs **only after** review passes.
- **Project Profiler** — auto-detects the target repo's shape per mission.

Two hard rules protect the chain:

- **异-provider rule** — a validator may never use the Coder's LLM provider this mission (guards against shared-training-data blind spots). Enforced in `models/router.py`; Smart Router tier overrides pass through the same check.
- **Handoff completeness rule (v3.1)** — a handoff that reports *no* incomplete items, issues, or deviations triggers a mandatory second pass (catches "too-perfect" handoffs).

## What ships (A–F)

- **Schemas** (`schemas/`) — Pydantic v2 models for every soul.md artifact; all reject extra fields.
- **ModelRouter + Smart Router** (`models/`) — LiteLLM wrapper, static + dynamic 异-provider enforcement, tier-based model selection with route-decision logging.
- **Blackboard** (`blackboard/`) — `ArtifactStore` (atomic writes, path-escape protection, write-once contracts) + append-only `EventLog` with cost/token/outcome rollups.
- **Agents** (`agents/`) — `BaseAgent` foundation, the permission layer, and all six roles + their per-role `@function_tool` sets, on the OpenAI Agents SDK.
- **Orchestration** (`orchestrator/`) — `MissionDriver`, the DAG `Scheduler` with the dual-validator gate, the concurrent `MissionSupervisor` tick loop, status reports + push adapters + user-message inbox, the budget guard (50/80/100/150% bands with cautious-mode switch), stuck-recovery triage, and checkpoint/resume/rollback + snapshot restore.
- **Validators** (`validators/`) — the 5 behavior probe strategies (incl. the full Node WASM probe) and validator-conflict arbitration.
- **Sandbox** (`sandbox/`) — `LocalShellSandbox` (dev/dry-run) + `DockerSandbox` (`network_mode=none`, mem/pids limits, `cap_drop=ALL`, `no-new-privileges`).
- **Sanitizer** (`sanitizer/`) — external-content sanitizer with trust-boundary fence defanging.
- **Memory** (`memory/`) — cross-mission lessons (per-repo SQLite + global), time-decay/confidence retrieval, anti-poisoning framing, injected into first-message builders cold-start-safe.
- **Integrations** (`integrations/vcs.py`) — PR workflow via `gh`/`glab`, with a `gitleaks` pre-PR gate that **fails closed**.
- **Metrics** (`metrics/`) — health-baseline harness derived from each mission's `events.jsonl` + `mission_state.json`.
- **CLI** (`cli.py`) — `maf-coder mission`, `resume`, `rollback`, `pr`, `metrics`, `preflight`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

The Docker sandbox build (`scripts/build_sandbox.sh`) is separate from Python dev — you
only need it when launching real missions, not when developing the framework.

## Commands you'll run constantly

```bash
pytest                                  # unit tests (657, fast)
pytest tests/test_schemas.py -v         # one module
pytest -k "completeness" -v             # by name pattern
ruff check src tests scripts            # lint
mypy                                    # strict type check
bandit -r src/maf_coder --severity-level medium --confidence-level medium   # SAST
pip-audit --skip-editable               # dependency CVE audit
```

The pre-commit gate (also run in CI, `.github/workflows/ci.yml`) is all of the above.
`pytest` must stay green on `main`.

Live tests are gated behind `RUN_LIVE_TESTS=1` and require API keys; the default suite
makes no network calls.

## Running a mission

```bash
# Production-readiness gate — checks keys/config/docker, inspect-only
maf-coder preflight --sandbox local

# Dry-run plans the mission with no API calls and no agent code execution
maf-coder mission new --repo /path/to/rust/repo --task "..."   # defaults to dry-run/local

# A real run defaults to the Docker sandbox (isolated; fails loud if Docker is down)
maf-coder mission new --repo /path/to/rust/repo --task "..." --no-dry-run

maf-coder mission status <mission-id>
maf-coder resume <mission-id>           # multi-day checkpoint resume
maf-coder rollback <mission-id> --to <checkpoint>
maf-coder pr <mission-id>               # gitleaks-gated PR creation
maf-coder metrics <mission-id>
```

See `docs/FIRST_RUN_RUNBOOK.md` for the end-to-end live shakedown procedure.

### Model configuration

Roles map to models in `config/droid_whispering.yaml`. Any LiteLLM-addressable provider
works (official APIs or self-hosted/proxied endpoints) — a role entry names its model,
optional `api_base`, and an **`api_key_env`** variable name. **Secrets never go in config
or source** — only the env-var *name* lives in yaml; the value comes from the environment.

```bash
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
```

`scripts/check_endpoints.py` probes a config's endpoints for reachability and — with
`--tools` — for function-calling support (the framework dispatches all work via tool
calls, so a chat-only model cannot drive a role). `scripts/smoke_test.py` runs the
multi-provider stability gate; see `SMOKE_TEST.md` for interpreting failures.

## Security hardening

The egress, sandbox, and PR-secret paths were gap-scanned before live runs. Each item
below has tests — don't weaken them without discussion (see `CLAUDE.md` → "Production
hardening"):

- **Egress SSRF** — `check_network_allowed` enforces an http/https scheme allowlist; redirects are re-validated on every hop; resolved IPs are rejected if private/loopback/link-local/metadata; DNS is pinned-and-connected to close the TOCTOU rebind window (TLS SNI/cert still verify the original hostname).
- **PR secret gate** — the `gitleaks` pre-PR check fails closed: if the tool is missing, the PR is refused, not assumed clean.
- **Docker limits** — every container runs with mem/pids limits, `cap_drop=ALL`, `no-new-privileges`, optional CPU cap, and `network_mode=none`.
- **CI security scanning** — `bandit` (SAST) + `pip-audit` run in the gate; the aiohttp CVE is pinned out.
- **Local sandbox** — strips secret-named host env vars before running agent commands.

## Hard invariants

Each is enforced by at least one test. If you find yourself disabling such a test, stop
and discuss (full list in `CLAUDE.md`):

1. `Handoff.triggers_second_pass` ⇔ all of `incomplete` / `issues_discovered` / `deviations_from_plan` empty.
2. `ModelRouter` blocks any validator model whose provider matches the Coder's provider this mission.
3. `ArtifactStore.save_validation_contract` is write-once unless `allow_overwrite=True`.
4. `ArtifactStore` rejects any path resolving outside `mission_dir` (`PathEscapeError`).
5. `prompts/coder_worker.md` mandates idempotent writes + `git checkout` at task start (prompt-enforced, protects multi-day resume).
6. `agent_team_soul_v3.1.md` edits to role boundaries / contracts / hard rules need a version bump + changelog entry.

## Where things live

```
src/maf_coder/
├── schemas/      # Pydantic models — most stable layer
├── models/       # ModelRouter + tier_router (LiteLLM, 异-provider)
├── blackboard/   # ArtifactStore + EventLog
├── agents/       # BaseAgent + roles + agents/tools/ (per-role tool sets)
├── orchestrator/ # mission_driver, scheduler, supervisor, budget, recovery, checkpoint
├── validators/   # probes/ (5 strategies) + arbitration
├── sandbox/      # LocalShellSandbox + DockerSandbox
├── sanitizer/    # external-content sanitizer
├── memory/       # cross-mission lessons
├── integrations/ # vcs.py (PR workflow)
├── metrics/      # health-baseline harness
└── cli.py        # maf-coder entry point

prompts/          # production agent system prompts; treat as code
config/           # droid_whispering.yaml + rust_sandbox.dockerfile
scripts/          # smoke_test.py, check_endpoints.py, build_sandbox.sh
tests/            # mirrors src/maf_coder/ structure
missions/         # gitignored — created per-mission at runtime
```

## Conventions

- Python 3.11+: `from __future__ import annotations`, `X | Y` not `Optional[X]`, `list[X]` not `List[X]`.
- Pydantic v2 only; every model uses `ConfigDict(extra="forbid")`.
- `mypy` strict; `ruff` for lint/format (configured in `pyproject.toml`).
- Atomic file writes and `_resolve()` path-safety inside `ArtifactStore` — don't bypass them.

For setup, conventions, hard invariants, and the suggested next concrete task, **`CLAUDE.md`
/ `AGENTS.md` is the single source of truth** for "how do I start working on this codebase today."
