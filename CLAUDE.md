# CLAUDE.md

This file is loaded by Claude Code at the start of every session for this project.

## What this project is

**MAF-Coder** is a multi-agent framework for autonomous Rust coding missions. Phases A–F are code-complete (plus the Smart Router track and the G3 metrics harness); Phase G is real-world validation, run by the human operator. See `MAF-Coder_v2_Build_Plan.md` for the roadmap and `agent_team_soul_v3.1.md` for the framework's organizational constitution.

**Meta-context that's easy to confuse**: this is a Python project that *builds* agents which *operate on Rust codebases*. When you're working in this repo, you're writing **Python** (orchestrator, workers, validators, schemas). The Rust-specific knowledge in `prompts/coder_worker.md` and `prompts/review_validator.md` is content that future agents will read — not your working environment.

## Quick links — read these on demand, don't pre-load all of them

- `agent_team_soul_v3.1.md` — framework constitution (roles, contracts, escalation). Read sections selectively when touching the parts they govern. Do not modify casually.
- `ARCHITECTURE.md` — system shape: components, lifecycles, 20-item design decisions log. The "what" of the system. Read §10 to find your current Phase, §17 if a design choice surprises you.
- `AGENT_TOOLS_SPEC.md` — formal signatures: BaseAgent, TaskContext, 51 tools across all roles, Scheduler, MissionDriver, OpenAI Agents SDK integration patterns. The "how" of the system. §17 has the canonical Phase B implementation order.
- `WORKED_EXAMPLE.md` — a complete end-to-end mission walkthrough with sample artifacts at every step. The "looks like this" of the system. §12 has a cross-reference table mapping every artifact to its schema and writing tool.
- `MAF-Coder_v2_Build_Plan.md` — phased delivery roadmap (Phase A through G). Tells you what's in scope right now.
- `SMOKE_TEST.md` — what the two host-side Phase A gates do, how to interpret failures.
- `prompts/` — agent system prompts that go into OpenAI Agents SDK as `instructions=`. Editing these changes agent behavior; treat as production code, not docs.
- `README.md` — newcomer-facing setup.

## Setup (run once per machine)

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Commands you'll run constantly

```bash
pytest                                  # unit tests, fast
pytest tests/test_schemas.py -v         # one module
pytest -k "completeness" -v             # by test name pattern
ruff check src tests scripts            # lint
ruff format src tests scripts           # format
mypy                                    # type check (strict mode per pyproject)
bandit -r src/maf_coder --severity-level medium --confidence-level medium  # SAST
pip-audit --skip-editable               # dependency CVE audit
python scripts/smoke_test.py --dry-run  # plan check, no API calls
```

`pytest` should always pass on `main`. If you commit while red, you've broken the bar.

## Current phase: A–F code-complete (memory + PR workflow live); Phase G (real-world validation) next

Phase A delivered:
- All Pydantic schemas (`src/maf_coder/schemas/`)
- ModelRouter with 异-provider enforcement (`src/maf_coder/models/router.py`)
- ArtifactStore + EventLog (`src/maf_coder/blackboard/`)
- Rust sandbox Dockerfile + smoke test runner
- Three agent prompts (`prompts/`)
- 4 test modules with critical-invariant coverage

Tier 1 design package delivered (~3900 lines across three docs):
- `ARCHITECTURE.md` (830 lines)
- `AGENT_TOOLS_SPEC.md` (1641 lines)
- `WORKED_EXAMPLE.md` (1433 lines)

Phase B delivered the BaseAgent foundation, permission layer, SandboxClient, Coder Worker, ReviewValidator + adversarial sub-agent, Project Profiler, the sequential Scheduler, MissionDriver, and the `maf-coder mission` CLI entry point.

Phase C delivered the Research Worker, Security Worker, the content sanitizer, egress logging, and the parallel worker matrix.

Phase D is code-complete: the BehaviorValidator (`agents/behavior.py`, `prompts/behavior_validator.md`), the 5 probe strategies + behavior tools (`validators/probes/`, `agents/tools/behavior_tools.py`), the runtime dual-validator chain gate (`orchestrator/scheduler.py` — behavior runs only after review PASS), and validator conflict arbitration (`validators/arbitration.py`). A parallel Smart Router track also landed: tier-based model selection (`models/tier_router.py`, `ModelRouter.resolve_model`) with route-decision logging, never weakening the 异-provider rule. 393 unit tests pass; live tests gated by `RUN_LIVE_TESTS=1`.

Phase E is code-complete: the concurrent `MissionSupervisor` tick loop (`orchestrator/supervisor.py`) with a hook interface, plus three hooks/subsystems built on it — status reports + push adapters + user-message inbox (`orchestrator/status_report.py`, `push.py`, `inbox.py`), the budget guard (`orchestrator/budget.py` — bands at 50/80/100/150%, `mission_state.budget_mode`, scheduler honors "paused") + stuck-recovery triage (`recovery.py`), and resume/rollback + snapshot restore/GC + CLI (`orchestrator/checkpoint_store.py`, `mission_driver.resume`/`rollback`, `sandbox.restore_snapshot`, `maf-coder resume`/`rollback`). 459 unit tests pass; live tests gated by `RUN_LIVE_TESTS=1`.

Phase F is code-complete: cross-mission memory (`memory/` — per-repo `.maf-coder/memory.db` SQLite + global lessons, keyword/hybrid retrieval with time-decay + confidence, anti-poisoning `<historical_lesson>` framing, 50-item dedup), `mission_retro.md` assembly + the `save_retro` tool, retrieval injected into the Orchestrator's first message (cold-start-safe), and the PR workflow (`integrations/vcs.py` — `gh`/`glab` via sandbox, PR-description gen, artifact links, the existing `gitleaks_detect` reused as a pre-PR gate that refuses on findings, `create_pr` tool + `maf-coder pr` CLI). stdlib-only (no new deps). 512 unit tests pass; live tests gated by `RUN_LIVE_TESTS=1`.

What remains for Phase D/E/F Build Plan exit criteria (acceptance, not code) — these need real Rust projects + API keys and are run by the human operator:
- Behavior probes verified against a real Rust HTTP service / CLI tool / library mission; BehaviorValidator catching ≥2 logic bugs ReviewValidator missed
- A real 48h continuous mission exercising checkpoint-rollback-resume, a status push, an inbox injection, and an 80% budget cautious-mode switch
- Two related missions on one repo where the second's plan references the first's retro; ≥1 real GitHub PR a human calls "enough info"

The Build Plan §Phase D/E/F exit criteria are the source of truth for "done". Remaining phase is **G (real-world validation)** — largely acceptance (7-day mission, multi-project rotation). Its code deliverables are complete: the **G3 health-metric baseline harness** (`metrics/` + `maf-coder metrics` — first-pass / final-pass / cost / wall-clock / human-intervention / routing-savings, derived from each mission's `events.jsonl` + `mission_state.json`; PR-review pass rate is the one human-annotated input; see `docs/MAF_CODER_EXECUTION_PLAN.md §7`) and the **full Node WASM probe** (`validators/probes/wasm.py` — the Build-Plan-deferred upgrade from the PR-D1 minimal build-only version: `cargo build → wasm-pack build → wasm-pack test --node` with graceful no-wasm-tests degradation, plus a per-assertion node harness / default import-smoke). Phase G is now **all acceptance** (live runs); no Phase G code remains.

Memory-retrieval injection is wired into all three first-message builders — Orchestrator, Research, and Coder (the Coder filters to prior `handoff` records) — via the shared cold-start-safe `memory.retrieve_memory_block(store, query)` helper.

Real-mode mission bootstrap is now wired: `MissionDriver.start()` seeds one `Role.ORCHESTRATOR` task (`_orchestrator_bootstrap_task()`), so `maf-coder mission new --no-dry-run` actually runs the Orchestrator → plans → dispatches the DAG (previously a no-op). The remaining work is the **live shakedown run** itself (real Rust repo + API keys) — see `docs/FIRST_RUN_RUNBOOK.md`. `coder_provider_in_use` is now derived from the router's `coder_worker` primary (with a `maf-coder mission new --coder-provider` override), so both halves of the 异-provider rule engage on a real run. The Docker sandbox is now CLI-wired (`mission new`/`resume --sandbox docker|local`) and **secure by default**: a real run (`--no-dry-run`) defaults to `docker` (isolated; fails loud if Docker is down — build the image with `scripts/build_sandbox.sh`), while dry-runs default to `local` (no agent code executes). Explicit `--sandbox` always wins; see `_resolve_sandbox` in `cli.py`. Milestone re-invocation is now wired too: the Driver re-invokes the Orchestrator once per milestone (`_milestone_loop` in `mission_driver.py`), draining the dispatched DAG between turns, until the Orchestrator's `complete_mission` tool sets `mission_state.mission_complete`. `current_milestone` is reconciled with plan.md — the Driver derives it from `tasks.yaml`'s `parent_milestone` fields (first planned milestone not yet in `completed_milestones`), falling back to a synthetic index only for the bootstrap/planning turn.

### Production hardening (pre-G gap-scan)

A security gap-scan of the egress, sandbox, and PR-secret paths landed the following. Each has tests; don't weaken them without discussion.

- **CI security scanning (P1):** `bandit` (SAST, medium+/medium+) + `pip-audit` (`--skip-editable`) run in the gate (`ci.yml`) and locally. The aiohttp CVE is pinned out; `check_network_allowed` enforces an http/https **scheme allowlist** (blocks `file://`/`ftp://`/etc.).
- **Egress SSRF — redirect re-validation (H1):** `fetch_url` runs **host-side**, not inside the container's `network_mode=none`, so `check_network_allowed` is the *only* egress control. `urllib` auto-follows redirects, so `research_tools._ValidatingRedirectHandler` re-runs the network gate on **every** redirect hop (a permitted host can't 302 onto a denied/SSRF target). Blocked hops log `blocked_reason="redirect-denied"`.
- **Egress SSRF — resolved-host check + pin-and-connect (M2):** `permissions.check_resolved_host_safe` is the fast pre-check — resolves a host and rejects if **any** resolved IP is private/loopback/link-local/metadata (`ipaddress`-based `_ip_is_blocked`, covers IPv6 + IPv4-mapped), on the initial URL and every redirect hop (`resolver` injectable; real DNS in prod). The TOCTOU window is **closed** at the transport: `research_tools._safe_create_connection` (wired via `_PinnedHTTP(S)Connection`/`_PinnedHTTP(S)Handler`) resolves **once**, validates every address via `permissions.assert_addr_allowed`, then connects to that exact IP — no second DNS lookup, so a rebind can't be reached. TLS SNI + cert verification still run against the original hostname.
- **gitleaks pre-PR gate fails closed (H2):** if `gitleaks` is absent, `vcs.run_gitleaks_gate` raises `GitleaksUnavailableError` and `create_pull_request` **refuses** the PR (was silently treating "tool missing" as "clean").
- **Docker sandbox limits (M1):** every container runs with `mem_limit` (8g default), `pids_limit` (4096), `cap_drop=["ALL"]`, `security_opt=["no-new-privileges:true"]`, optional `nano_cpus` — via `DockerSandbox._hardening_kwargs()` / `_run_base_kwargs()` (shared by `start()` + `restore_snapshot()`; `network_mode=none` unchanged). Tunable through the constructor.
- **Sanitizer fence (L1):** `sanitizer._neutralize_fence` defangs embedded `<external>`/`</external>` tokens in fetched bodies so untrusted content can't close/forge the trust boundary.
- **Local sandbox env (L2):** `LocalShellSandbox.exec` strips secret-named host env vars (`_SECRET_ENV_RE`: API_KEY/_KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/PRIVATE) before running agent commands; explicit `env` still wins. (Docker path was already clean.)

## Implementation reading order (any phase)

When implementing a phase, follow this sequence:

1. Read `ARCHITECTURE.md §10` to confirm where the phase sits in the system, and `docs/MAF_CODER_EXECUTION_PLAN.md` for the current phase's per-PR breakdown.
2. Read the relevant Build Plan phase section (`MAF-Coder_v2_Build_Plan.md §Phase X`) for scope + exit criteria.
3. For each work item:
   - Read the relevant section of `AGENT_TOOLS_SPEC.md` for tool/component signatures.
   - Read the relevant section of `WORKED_EXAMPLE.md` for what the output should look like (use §12 cross-reference table to find it).
   - Read the relevant Pydantic schema in `src/maf_coder/schemas/` for data shape.
   - Read the relevant prompt in `prompts/` if the work involves an agent role's behavior.
   - Mirror the closest existing analog (e.g. a new validator mirrors `agents/review.py`; new tools mirror `agents/tools/review_tools.py`).
   - Implement + write tests.
4. Gate before every commit: `pytest && ruff check src tests && mypy src/maf_coder && bandit -r src/maf_coder --severity-level medium --confidence-level medium && pip-audit --skip-editable`. `main` must stay green. CI (`.github/workflows/ci.yml`) runs the same gate.

Respect phase boundaries — each phase has independent exit criteria. Don't pull Phase E/F/G work into the current phase; the Build Plan and execution plan define what's in scope now.

## Where things live

```
src/maf_coder/
├── schemas/      # Pydantic models — most stable layer; changes here ripple
├── models/       # ModelRouter + tier_router (LiteLLM wrapper, 异-provider constraints)
├── blackboard/   # ArtifactStore + EventLog — file-system substrate
├── agents/       # BaseAgent + roles (coder, research, security, review, behavior,
│                 #   orchestrator) and agents/tools/ (per-role @function_tool sets)
├── orchestrator/ # mission_driver, scheduler (DAG + dual-validator gate), project_profiler
├── validators/   # probes/ (5 behavior probe strategies), arbitration.py
├── sandbox/      # LocalShellSandbox + DockerSandbox
├── sanitizer/    # content + external-content sanitizer (Phase C)
└── cli.py        # `maf-coder mission` entry point

prompts/          # production agent system prompts; treat as code
config/           # droid_whispering.yaml + rust_sandbox.dockerfile
scripts/          # smoke_test.py, build_sandbox.sh
tests/            # mirror of src/maf_coder/ structure
missions/         # gitignored — created per-mission at runtime
```

## Project conventions

These are real conventions in this codebase, not aspirational rules:

- **Python 3.11+**. Use `from __future__ import annotations` at file top; use `X | Y` not `Optional[X]`; use `list[X]` not `List[X]`.
- **Pydantic v2 only**. `BaseModel`, `ConfigDict`, `Field`, `computed_field`. Not v1 `class Config` style.
- **All models reject extra fields**. Use `ConfigDict(extra="forbid")` on every BaseModel — typos in field names must fail loudly, not silently. There are 3 deliberate exceptions in `models/router.py` (`RoleConfig`, `RouterConfig`) where `extra="allow"` tolerates future yaml keys; do not copy that pattern elsewhere.
- **mypy strict**. Run `mypy` before committing. `Any` is a smell — justify it in a comment if you use it.
- **Atomic file writes**. Never `open(path, "w")` directly inside `ArtifactStore` — use `_atomic_write`. All single-file writes in this layer go through tmp-then-rename.
- **Path safety**. Inside `ArtifactStore`, every relpath goes through `_resolve()` which rejects escape attempts. Don't bypass it.
- **English code, Chinese 术语 in docstrings**. Match what's already in the file you're editing. Don't translate mid-file.

## Hard invariants — do NOT change without explicit discussion

Each of these has at least one test enforcing it. If you find yourself disabling such a test, stop and discuss instead.

1. **`Handoff.triggers_second_pass` returns True iff `incomplete`, `issues_discovered`, and `deviations_from_plan` are all empty.**
   (`schemas/handoff.py`, tested in `test_schemas.py::TestHandoffCompletenessRule`)
   This is the v3.1 rule that catches "too perfect" handoffs.

2. **`ModelRouter` blocks any model whose provider matches `coder_provider_in_use` when the role is `review_validator`, `behavior_validator`, or `adversarial_subagent`.**
   (`models/router.py::_VALIDATOR_ROLES`, tested in `test_router.py::TestDynamicCoderConstraint`)
   This is the 异-provider constraint protecting against shared-training-data blind spots. Smart Router tier overrides (`resolve_model`) pass through this same check — a tier can never route a validator onto the Coder's provider.

3. **`ArtifactStore.save_validation_contract` is write-once unless `allow_overwrite=True` is explicitly passed.**
   (`blackboard/artifact_store.py`, tested in `test_artifact_store.py::TestContractWriteOnce`)
   Coder must not modify a locked contract. Overwrite is gated by Human Gate.

4. **`ArtifactStore` rejects paths that resolve outside `mission_dir`.**
   (`PathEscapeError`, tested in `test_artifact_store.py::TestPathSafety`)
   This is the prompt-injection containment boundary.

5. **`prompts/coder_worker.md` mandates idempotent writes + `git checkout` at task start.**
   This isn't enforced by Python — it's enforced by the prompt. If you weaken this phrasing without compensating logic, multi-day resume breaks.

6. **`soul.md` versioning rules apply.** Edits to `agent_team_soul_v3.1.md` that change role boundaries, message contracts, or hard rules need a version bump (v3.1 → v3.2 for additions, → v4 for structural change) and a changelog entry in §15.

## Workflow patterns

### Adding a new schema

1. Add the Pydantic model in `src/maf_coder/schemas/<area>.py`
2. Re-export from `schemas/__init__.py`
3. Add round-trip test in `tests/test_schemas.py`
4. If it's persisted, add `save_*` / `load_*` to `ArtifactStore` and a test in `test_artifact_store.py`
5. If agents read/write it, add reference in the relevant prompt under `prompts/`

### Updating a prompt

1. Edit `prompts/<role>.md` (these are deliberately self-contained — no external refs)
2. If you changed a field name, check that the schema in `src/maf_coder/schemas/` matches
3. If you changed a behavioral rule, add a corresponding test if possible (most prompt behavior can't be unit-tested, but completeness rules and异-provider rules can)
4. Commit message: `prompts(<role>): <what changed>` — these have outsize impact on production behavior, the commit log shouldn't hide them

### Adding a new agent role

1. Add the role enum value in `schemas/common.py::Role`
2. Add the role config in `config/droid_whispering.yaml`
3. Add the prompt in `prompts/<role>.md`
4. If it's a validator, possibly add to `_VALIDATOR_ROLES` in `models/router.py`
5. Add the prompt-loading and dispatch glue in the orchestrator (Phase B)

### Running the smoke test gate

API keys go in env, NOT in `config/`:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...
python scripts/smoke_test.py --dry-run    # always do this first
python scripts/smoke_test.py              # the real gate
```

Interpret failures per `SMOKE_TEST.md` — three escalating mitigation paths. Do not silently mark a failing combo "fine" — adjust `config/droid_whispering.yaml` to reflect what's actually working.

## What to focus on right now

Phase B's next concrete deliverable is **`BaseAgent`** (per `AGENT_TOOLS_SPEC.md §2`). It's the foundation everything else builds on. Steps:

1. **Read** `AGENT_TOOLS_SPEC.md §2 + §3 + §4 + §5` end-to-end. These four sections together describe BaseAgent, TaskContext, the result types, and the permission layer that every tool depends on.
2. **Read** the existing `src/maf_coder/orchestrator/planner.py` to see the pattern used for the current minimal Orchestrator → planner Agent integration. BaseAgent generalizes that pattern.
3. **Create** `src/maf_coder/agents/__init__.py`, `src/maf_coder/agents/base.py`, `src/maf_coder/agents/errors.py`, `src/maf_coder/agents/results.py`, `src/maf_coder/agents/permissions.py` (these correspond to §2-§5 of the spec).
4. **Test** each component:
   - `tests/test_base_agent.py` — exercise `BaseAgent.run` with a stub Runner (no real LLM calls); verify it constructs the right `TaskContext`, calls the right router method, parses output via the subclass.
   - `tests/test_permissions.py` — exercise the four `check_*` functions on permission boundary cases.
   - `tests/test_agent_errors.py` — exercise that `PermissionDeniedError` and other tool errors propagate through the SDK correctly (use a fake tool that always raises).

After `BaseAgent` is solid, the next implementation steps follow §17 of `AGENT_TOOLS_SPEC.md` — permission layer → SandboxClient → Coder Worker tools → CoderWorkerAgent → ReviewValidator tools → etc.

Do NOT skip ahead to:
- Implementing Coder Worker tools before `BaseAgent` + `TaskContext` are solid (they all depend on it)
- BehaviorValidator (Phase D)
- Multi-day infrastructure: Checkpoint, Status Report timer, Budget Guard (Phase E)
- Cross-mission memory (Phase F)

Each phase's exit criteria are independent. Don't merge them.

## Notes that may be obvious but burn time when missed

- `pyproject.toml` already configures ruff, pytest, mypy. Don't pass conflicting CLI flags.
- `pytest` uses `pythonpath = ["src"]` from pyproject, so imports work without explicit PYTHONPATH.
- `pip install -e ".[dev]"` is required for the `[dev]` extras; without it `ruff` / `mypy` / `pytest-asyncio` aren't available.
- The Docker sandbox build (`scripts/build_sandbox.sh`) is **separate** from Python dev. You don't need Docker running to develop the framework. You need Docker only when actually launching missions in Phase B+.
- The smoke test directly calls LiteLLM, bypassing `ModelRouter`. That's intentional (separates "model stability" from "router logic"). Don't refactor the smoke test to go through the router — you'd lose this property.

## When in doubt

- Behavior question (what should this agent do?) → check `prompts/<role>.md` first, then `agent_team_soul_v3.1.md` §3
- Architecture question (what's the shape of X?) → check `ARCHITECTURE.md`; if it says nothing, the design didn't commit and you should ask before deciding
- Signature question (what's the Python type of X?) → check `AGENT_TOOLS_SPEC.md`
- Schema question (what's in this artifact?) → check `src/maf_coder/schemas/<file>.py` (these have docstrings)
- "What should the output look like?" → check `WORKED_EXAMPLE.md §12` cross-reference table → find the artifact → see the sample
- Roadmap question (when does X happen?) → check `MAF-Coder_v2_Build_Plan.md`
- Failure mode question → check `SMOKE_TEST.md` if it's gate-related, otherwise `agent_team_soul_v3.1.md` §5.4 Stuck Recovery
- Multiple sources disagree → soul.md > ARCHITECTURE.md > AGENT_TOOLS_SPEC.md > prompts/ > schemas/. If a lower source contradicts a higher one, the lower source is wrong; file an issue or fix it in place.
