# MAF-Coder

Multi-Agent Framework for Coder — production-grade Rust coding agent team.

> **Status: Phase A scaffolding.**
> Schema layer + ModelRouter only. Workers, validators, sandbox, CLI come in Phase B–G.
> See `MAF-Coder_v2_Build_Plan.md` for the full roadmap and
> `agent_team_soul_v3.1.md` for the framework constitution.

## For AI coding agents (Cursor, Claude Code, etc.)

This project has both `AGENTS.md` and `CLAUDE.md` at root with project-specific
instructions, plus `.claude/settings.json` and `.cursorignore` with conservative
permission/ignore rules. AI agents will load these automatically when started in
the project directory.

`AGENTS.md` and `CLAUDE.md` are kept in sync — they have identical content; the
two filenames exist because Cursor reads `AGENTS.md` natively while Claude Code
prefers `CLAUDE.md`. Edit either; remember to mirror to the other.

For setup, conventions, hard invariants, and the suggested next concrete
implementation task, **read `AGENTS.md` first** — it is the single source of
truth for "how do I start working on this codebase today."

The full design package is the trio:
- `ARCHITECTURE.md` (830 lines) — system shape: components, lifecycles, design decisions
- `AGENT_TOOLS_SPEC.md` (1641 lines) — formal signatures: BaseAgent, 51 tools, Scheduler, Mission Driver
- `WORKED_EXAMPLE.md` (1433 lines) — sample artifacts at every step of a complete mission

Plus the foundation docs:
- `agent_team_soul_v3.1.md` — framework constitution
- `MAF-Coder_v2_Build_Plan.md` — phased roadmap
- `prompts/*.md` — agent behavior contracts

## What's here (Phase A delivery — complete)

- `pyproject.toml` — Python 3.11+ project setup, all dependencies pinned
- `src/maf_coder/schemas/` — Pydantic v2 models for all soul.md artifacts:
  - `Message` (§11.1 inter-agent envelope)
  - `Task` (§16 task template)
  - `Handoff` (§11.3 with v3.1 完备性规则 baked in)
  - `ValidationContract` (§11.4 — locked at planning)
  - `ProjectProfile` (§6.1 — auto-detected per mission)
  - `ReviewVerdict` / `BehaviorVerdict` / `SecurityVerdict` (§3.4–3.6)
  - `StatusReport` / `Checkpoint` / `MissionState` (§5.2–5.3 multi-day lifecycle)
- `src/maf_coder/models/router.py` — `ModelRouter` that reads
  `config/droid_whispering.yaml` and enforces:
  - Static `forbidden_providers` constraints
  - **Dynamic异-provider constraint**: validators ≠ Coder's provider this mission
- `src/maf_coder/blackboard/` — A5 artifact blackboard:
  - `ArtifactStore` — type-safe mission artifact read/write with atomic writes,
    path-traversal protection, **write-once contract enforcement**, StatusReport
    auto-renders both .json and .md
  - `EventLog` — append-only jsonl with typed event kinds + cost/token/outcome
    aggregations + **v3.1 second-pass event** for handoff completeness rule
- `prompts/` — agent system prompts (~1000 lines total, drop-in for Phase B):
  - `orchestrator.md` (437 lines) — planning, validation contract drafting,
    task DAG routing, status reports, checkpoints, escalation, PR creation
  - `coder_worker.md` (250 lines) — Rust discipline + v3.1 universal coding
    discipline (explicit conflicts, follow conventions, idempotent writes,
    fail loudly) + structured handoff schema
  - `review_validator.md` (306 lines) — adversarial review, cargo gate
    sequence, v3.1 handoff completeness check, hardcoded-test detection
    sub-agent, verdict decision tree
- `tests/` — 4 test modules, covering schema round-trips, router constraints,
  artifact store path safety + write-once enforcement, event log aggregations

## Setup

```bash
cd maf-coder
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest                   # full suite
pytest tests/test_schemas.py -v
pytest tests/test_router.py -v
pytest tests/test_artifact_store.py -v
pytest tests/test_event_log.py -v
ruff check src tests
mypy
```

## Running the Phase A smoke gates

The unit tests above don't cover two Phase A exit criteria: the actual Docker
sandbox build and live three-provider API stability. Those have their own
runners + dedicated doc:

```bash
# Three-provider stability gate (host-side, needs API keys)
python scripts/smoke_test.py --dry-run    # preview the test plan
python scripts/smoke_test.py              # run full gate
python scripts/smoke_test.py --roles coder_worker --output results.json

# Docker sandbox build gate (needs Docker)
bash scripts/build_sandbox.sh
```

See **`SMOKE_TEST.md`** for what each gate validates, how to interpret results,
the three escalating mitigation paths if something fails, and known-good
baselines for May 2026.

## Phase A 退出门槛

From `MAF-Coder_v2_Build_Plan.md` §Phase A:

- [ ] Docker 镜像构建成功 (separate, see `config/rust_sandbox.dockerfile`)
- [ ] 三供应商 smoke test 全过 (live API call, separate from these unit tests)
- [x] Pydantic schemas defined and round-trip cleanly
- [x] ModelRouter loads yaml + enforces all constraints
- [x] ArtifactStore enforces directory layout + write-once contract + path safety
- [x] EventLog supports append + iterate + aggregate (cost / tokens / outcomes)
- [x] `pytest tests/` 全过

## Key invariants this scaffolding guarantees

1. **Handoff v3.1 完备性规则** — `Handoff.triggers_second_pass` returns `True` iff
   `incomplete`, `issues_discovered`, and `deviations_from_plan` are all empty.
   Tested in `test_schemas.py::TestHandoffCompletenessRule`.
2. **Validator异-provider** — Router enforces this dynamically per call via
   `coder_provider_in_use`. Tested in `test_router.py::TestDynamicCoderConstraint`.
3. **Schemas reject extra fields** — typos in field names fail loudly, not silently.
   Tested in `test_schemas.py::TestMessage::test_extra_fields_rejected`.
4. **Validation contract is write-once** — `ArtifactStore.save_validation_contract`
   raises `ContractAlreadyLockedError` on the second write. Tested in
   `test_artifact_store.py::TestContractWriteOnce`.
5. **Mission directory is sealed** — `..` and absolute paths are rejected with
   `PathEscapeError`. Tested in `test_artifact_store.py::TestPathSafety`.
6. **EventLog supports the Status Report data shape** — `total_cost_usd`,
   `total_tokens`, `cost_by_actor`, `task_outcomes` all roll up from a single
   append-only file. Tested in `test_event_log.py::TestAggregations`.

## Next: Phase B

- Real `cli.py` entrypoint
- Orchestrator that produces `plan.md` + `validation_contract.yaml` + `tasks.yaml`
- Coder Worker wrapping OpenAI Agents SDK sandbox agent
- ReviewValidator with adversarial sub-agent
- jsonl event log + artifact blackboard

See `MAF-Coder_v2_Build_Plan.md` §Phase B for exit criteria.
