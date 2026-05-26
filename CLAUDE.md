# CLAUDE.md

This file is loaded by Claude Code at the start of every session for this project.

## What this project is

**MAF-Coder** is a multi-agent framework for autonomous Rust coding missions. It is currently in Phase A→B transition. See `MAF-Coder_v2_Build_Plan.md` for the roadmap and `agent_team_soul_v3.1.md` for the framework's organizational constitution.

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
python scripts/smoke_test.py --dry-run  # plan check, no API calls
```

`pytest` should always pass on `main`. If you commit while red, you've broken the bar.

## Current phase: A complete + Tier 1 design done, Phase B implementation in progress

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

Phase B is in progress. The Orchestrator package and `planner.py` skeleton already exist (`src/maf_coder/orchestrator/__init__.py`, `src/maf_coder/orchestrator/planner.py`) with a working OpenAI Agents SDK integration that produces `plan.md` from a goal + ProjectProfile. 88 unit tests pass; 1 live test gated by `RUN_LIVE_TESTS=1`.

What's NOT yet built (the rest of Phase B per `AGENT_TOOLS_SPEC.md §17`):
- `BaseAgent` class + `TaskContext` + tool factory pattern
- Permission enforcement layer
- `SandboxClient` (Docker integration)
- Coder Worker (full implementation with all tools)
- ReviewValidator + adversarial sub-agent
- Project Profiler (currently uses hand-mocked profiles)
- Scheduler with DAG execution
- MissionDriver
- CLI entry point (`maf-coder mission` command)

The Build Plan §Phase B exit criteria are the source of truth for what "Phase B done" means.

## Phase B implementation reading order

When working on Phase B, follow this sequence (per `AGENT_TOOLS_SPEC.md §17`):

1. Read `ARCHITECTURE.md §10` to confirm where Phase B sits in the system.
2. Read `AGENT_TOOLS_SPEC.md §17` for the 16-step implementation order.
3. For each step in §17:
   - Read the relevant section of `AGENT_TOOLS_SPEC.md` for signatures (typically §2-§8 for Phase B work).
   - Read the relevant section of `WORKED_EXAMPLE.md` for what the output should look like (use §12 cross-reference table to find it).
   - Read the relevant Pydantic schema in `src/maf_coder/schemas/` for data shape.
   - Read the relevant prompt in `prompts/` if the work involves an agent role's behavior.
   - Implement + write tests.
4. End-to-end validation: once all Phase B steps are done, exercise the full mission flow using the scenario in `WORKED_EXAMPLE.md §0` (add /version endpoint to an axum service). The artifacts you produce should approximate those shown in §1-§8.

Do NOT skip ahead to Phase C (Research Worker, Security Worker), Phase D (BehaviorValidator), Phase E (multi-day infra), or Phase F (memory). Each phase has independent exit criteria.

## Where things live

```
src/maf_coder/
├── schemas/      # Pydantic models — most stable layer; changes here ripple
├── models/       # ModelRouter (LiteLLM wrapper, 异-provider constraints)
├── blackboard/   # ArtifactStore + EventLog — file-system substrate
├── orchestrator/ # (empty — Phase B will create planner.py, scheduler.py, etc.)
├── workers/      # (empty — Phase B+ will create coder.py, research.py, security.py)
├── validators/   # (empty — Phase B will create review.py, then Phase D adds behavior.py)
└── ...

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

2. **`ModelRouter` blocks any model whose provider matches `coder_provider_in_use` when the role is `review_validator` or `adversarial_subagent`.**
   (`models/router.py::_VALIDATOR_ROLES`, tested in `test_router.py::TestDynamicCoderConstraint`)
   This is the 异-provider constraint protecting against shared-training-data blind spots.

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
