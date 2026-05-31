# MAF-Coder — Execution Plan (Opus 4.8, authoritative)

> **Status of this doc.** This is the single source of truth for near-term build execution.
> It reconciles the canonical `MAF-Coder_v2_Build_Plan.md` (Phases A–G) with the two
> handoff docs (`docs/MAF_CODER_MASTER_PLAN.md`, `docs/PILOTDECK_SMART_ROUTER_FUSION.md`).
> Where they disagree, **this doc wins** and says why.
>
> Baseline audited at `190e7fe`: **294 tests green** (ran, not assumed). Phases A→C complete.

---

## 0. Re-review verdict (what changed vs. the handoff docs)

| Claim in handoff docs | Verdict after grounded audit | Action |
|---|---|---|
| Phase C closed, 294 tests = trustworthy baseline | **Confirmed** (ran suite: 288+6=294 pass) | Use as baseline |
| BehaviorValidator is "zero code, full scaffolding" | **Confirmed** — `BehaviorVerdict`/`BehaviorObservation` (verdict.py), `BehaviorProbeSpec` (profile.py), `Role.BEHAVIOR_VALIDATOR` + `verification_method=behavior_probe` (common.py), §11 sketches all present | Phase D = fill the socket |
| Phase D is the only P0 | **Confirmed** — without it `WORKED_EXAMPLE` t5 is a stub; soul.md's dual-validator promise is unmet | D is critical path |
| Insert "Phase D+ Smart Router" before E | **Corrected** — not in canonical Build Plan; it's a *cost* optimization. Optimize cost only once a measurable multi-day loop (E) exists. | SR → **independent parallel track**, not a phase |
| Docs lag one phase | **Worse than stated** — `CLAUDE.md` says "Phase B in progress, 88 tests"; off by a phase and 206 tests | Fix in Wave 0 (WS-DOCS) |

**Roadmap shape (locked):**

```
Critical path:   D ──→ E ──→ F ──→ G        (canonical capability ladder)
Parallel track:     └─ SR (models/ only) ──┘ starts once D-core framework merged;
                                              file-disjoint from D, merges before/with E
```

---

## 1. Hard invariants (every workstream obeys; CI-enforceable)

1. **294+ tests stay green.** No PR merges red. New code ships with tests.
2. **Pydantic `extra="forbid"`** on every new model (matches existing schemas).
3. **`@function_tool` identity-shim pattern unchanged.** SDK wrap happens only in `BaseAgent._execute_sdk`. New tools follow `make_<tool>(ctx)` → inner `async def` exactly as in `review_tools.py`.
4. **Every tool routes through `permissions.check_tool_allowed`.**
5. **Different-provider rule:** any validator/adversarial agent ≠ the Coder's provider. BehaviorValidator must honor `forbidden_providers` exactly as ReviewValidator does.
6. **`validation_contract` is write-once.** Validators read it, never mutate.
7. **`ArtifactStore` path-escape rejection** holds for all new evidence/verdict writes.
8. **`Handoff.triggers_second_pass` three-empty rule** untouched.
9. Gate before every merge: `pytest && ruff check src tests && mypy src/maf_coder`.

---

## 2. PHASE D — BehaviorValidator + dual-validator chain (P0, immediate)

**Goal:** make "is the behavior correct?" real via headless probes dispatched by project type.
**Exit (Build Plan §Phase D):** dual chain gates at runtime; ≥3 probe strategies unit-tested
(cli/backend/library; embedded/wasm minimal); evidence written on every fail path; BehaviorValidator
catches ≥2 logic bugs ReviewValidator misses (validated later in live mission, not blocking code merge).

The five PRs below are an intrinsically **serial value chain** (each consumes the previous), with two
exceptions that parallelize: **D5-docs** (independent) and the **SR track** (file-disjoint). See §3.

### PR-D1 — Probe framework + behavior tools  *(long pole; foundation)*

**New files (no edits to existing code):**
```
src/maf_coder/validators/__init__.py
src/maf_coder/validators/probes/__init__.py
src/maf_coder/validators/probes/base.py        # ProbeStrategy ABC, ProbeResult dataclass
src/maf_coder/validators/probes/cli.py          # cli_assert_cmd_probe
src/maf_coder/validators/probes/backend.py      # backend_service_health_probe
src/maf_coder/validators/probes/library.py      # library_example_probe
src/maf_coder/validators/probes/embedded.py     # embedded_host_test_probe (minimal)
src/maf_coder/validators/probes/wasm.py         # wasm_node_probe (minimal: cargo build wasm32 + wasm-pack)
src/maf_coder/validators/probes/registry.py     # strategy name → class
src/maf_coder/agents/tools/behavior_tools.py    # build_behavior_tools(ctx)
tests/validators/__init__.py
tests/validators/test_probes.py
tests/agents/test_behavior_tools.py
```

**Tool signatures — copy from `AGENT_TOOLS_SPEC.md §11` verbatim** (do not redesign):
`make_start_service(command, ready_check, timeout_sec=300) -> {service_id, started_at, log_path}`,
`make_stop_service(service_id)`, `make_probe_http(url, method="GET", body=None, expected_status=None)`,
`make_probe_cli(binary, args=[], stdin=None, expected_exit_code=None)`,
`make_save_behavior_verdict(task_id, result, probe_strategy, observations=[], evidence_path="", failure_reason=None) -> str`,
`make_save_behavior_evidence(task_id, name, content: bytes) -> str`.

**Probe runner design (the part not in §11):**
- Reads `ctx.store.load_project_profile().behavior_probe` → `BehaviorProbeSpec` (`strategy`, `start_command`, `ready_check`, `endpoints_to_probe`, `timeout_sec=300`).
- Reads `load_validation_contract()`, filters assertions where `verification_method == behavior_probe`.
- Each probe emits `BehaviorObservation(assertion_id, observed, expected, matched)` — **one per assertion, 1:1**.
- **On failure: MUST write evidence** (stdout/stderr/log path) before returning. Non-negotiable (exit gate).
- All process exec goes through the existing `LocalShellSandbox` / sandbox client — never host shell.

**Save targets:** `save_behavior_verdict` → `missions/<id>/verdicts/<task_id>.behavior.json` (validate against `BehaviorVerdict`); `save_behavior_evidence` → `missions/<id>/behavior_evidence/<task_id>/<name>`.

**Tests:** each of cli/backend/library has a pass-path and a fail-path test against `LocalShellSandbox` + a mock service/binary; assert evidence file exists on fail; `BehaviorVerdict` round-trips. embedded/wasm: one smoke test each (build-only).

**Gate:** `pytest tests/validators tests/agents/test_behavior_tools.py -v` green + full suite + ruff + mypy.

### PR-D2 — BehaviorValidatorAgent + prompt  *(depends: D1 merged)*

**New files:**
```
src/maf_coder/agents/behavior.py            # BehaviorValidatorAgent(BaseAgent) — mirror review.py (88 lines)
prompts/behavior_validator.md               # mirror prompts/review_validator.md structure
tests/agents/test_behavior_agent.py
```
**Edit:** `src/maf_coder/agents/__init__.py` (export `BehaviorValidatorAgent`).

**Agent contract (mirror `review.py`):**
- `role = Role.BEHAVIOR_VALIDATOR`
- `build_tools` → `build_behavior_tools(ctx)`
- `build_first_user_message`: emphasize **read-only on source, run probes, write verdict**; inputs include `verdicts/<t_review>.review.json` (must be PASS), the contract, the profile.
- `parse_output` → `BehaviorRunSummary(verdict_path=...)` (add to `agents/results.py` if absent; mirror `ReviewRunSummary`).

**Prompt requirements (`prompts/behavior_validator.md`):** identity + **never edit code**; the 5 probe-strategy selection rules keyed off `profile.behavior_probe.strategy`; `assertion_id ↔ observation` 1:1 rule; mandatory evidence dir on fail.

**Gate:** stubbed-`_execute_sdk` end-to-end test, verdict JSON round-trip, full suite green.

### PR-D3 — Dual-validator chain wiring  *(depends: D2 merged)*

**Edits:**
```
src/maf_coder/orchestrator/mission_driver.py        # agent_factory += Role.BEHAVIOR_VALIDATOR: lambda: behavior
src/maf_coder/agents/tools/orchestrator_tools.py    # dispatch_task gate (see below)
src/maf_coder/orchestrator/scheduler.py             # (optional) _is_ready honors verdict gate
tests/orchestrator/test_validator_chain.py          # new
```

**Gating rule (generic — do NOT hardcode t4/t5 IDs):** when `dispatch_task` targets a `behavior_validator` task, verify its `depends_on` includes a `review_validator` task **and** that task's `*.review.json` verdict is `PASS` (read via store). If not → refuse dispatch / mark `blocked` + emit event. Use task dependency + verdict files, never literal IDs.

**Tests (the gate is the point):** Review FAIL ⇒ Behavior task never executes (blocked/refused); Review PASS ⇒ Behavior dispatchable; Behavior FAIL ⇒ event carries `implementation_path_issue`.

### PR-D4 — Validator conflict arbitration  *(depends: D3 merged)*

**Logic (Build Plan §D3 + soul.md):**

| Review | Behavior | Action |
|---|---|---|
| PASS | FAIL | Orchestrator re-plans — "implementation path issue", `risk=medium` (stuck-recovery signal) |
| FAIL | — | Behavior not run (already enforced by D3) |
| FAIL | PASS | Human Gate (should be near-impossible) |
| PASS | PASS | milestone checkpoint candidate |

**Impl:** helper `check_validator_preconditions(task_id)` + `escalate_to_human_gate` path in the dispatch layer (or a small `validators/arbitration.py`). **Tests:** table-driven over all four rows.

### PR-D5 — Doc alignment  *(independent — can run in Wave 0 as WS-DOCS)*

- `prompts/behavior_validator.md` cross-check vs `BehaviorVerdict`/`BehaviorObservation` field names.
- `ARCHITECTURE.md §10` status table → Phase C complete / D in progress.
- `CLAUDE.md` "Current phase" → **Phase C complete, Phase D in progress; 294 tests** (currently wrongly says Phase B / 88 tests).

---

## 3. Parallel execution design (worktree-per-workstream)

**Honest parallelism note.** Phase D's code chain D1→D2→D3→D4 is serial by construction.
Real concurrency comes from two file-disjoint tracks: **WS-DOCS** (immediate) and **SR** (after D-core).
I will not fake parallelism by splitting a serial chain.

**Worktrees** (all off `main`, isolated; `~/Projects/maf-coder` is the root):
```
maf/docs-phase-sync   → WS-DOCS  (CLAUDE.md, ARCHITECTURE.md §10)         [Wave 0]
maf/d1-probes         → PR-D1    (validators/probes/*, behavior_tools.py)  [Wave 0]
maf/d2-agent          → PR-D2    (behavior.py, prompt)                     [Wave 1]
maf/sr-tier-router    → SR-1/2/3 (models/*, schemas/routing.py, yaml)      [Wave 1, parallel]
maf/d3-chain          → PR-D3    (mission_driver, orchestrator_tools)      [Wave 2]
maf/d4-arbitration    → PR-D4    (arbitration helper)                      [Wave 3]
```

**Wave schedule & merge order:**

| Wave | Parallel workstreams | Merge gate before next wave |
|---|---|---|
| 0 | **WS-DOCS** ‖ **PR-D1** | D1 green (pytest+ruff+mypy) → merge to main; DOCS merges anytime |
| 1 | **PR-D2** ‖ **SR-1** (tier_router + schema + yaml + tests) | D2 green → merge |
| 2 | **PR-D3** ‖ **SR-2** (router.resolve_model + base.py hook) | D3 green → merge |
| 3 | **PR-D4** ‖ **SR-3** (EventLog.log_route_decision) | D4 green → merge; SR merges before E |

**Conflict map (why this is safe):** D-track owns `validators/`, `agents/behavior.py`, `agents/tools/behavior_tools.py`, `orchestrator/*`. SR-track owns `models/*`, `schemas/routing.py`, `config/*.yaml`, and a **single hook line** in `agents/base.py::run`. Only contended file = `agents/__init__.py` (both add an export) and `base.py` (SR adds one line) — sequence SR-2's `base.py` edit after D-track touches base, or rebase. Everything else is disjoint.

**Orchestration loop (me, this thread):** for each workstream → create worktree, spawn a focused Claude agent with the PR spec above + invariants §1, agent runs gate locally, I review diff, merge to main in dependency order, re-run full gate on main, advance wave.

---

## 4. SR track — Smart Router (PilotDeck fusion), parallel after D-core

Detail lives in `docs/PILOTDECK_SMART_ROUTER_FUSION.md`; this is the execution slice.

- **SR-1** `models/tier_router.py` + `schemas/routing.py` (`RouteDecision`, optional `TierName`) + `config/droid_whispering.yaml` `smart_router:` block + `tests/test_tier_router.py`. Port PilotDeck's Judge prompt: `<tier>…</tier>` parse + continuation sticky + `defaultTier` fallback. Four tiers `simple/medium/reasoning/complex`; **`complex` ⇒ Orchestrator splits a DAG task, never SDK sub-agent spawn.**
- **SR-2** `ModelRouter.resolve_model(role, *, task, coder_provider_in_use)` → applies tier over primary, **still passes `forbidden_providers`**. Per-role enable flags in yaml (`coder_worker: on`, `review_validator: off`, `behavior_validator: on`). One hook line in `BaseAgent.run`.
- **SR-3** `EventLog.log_route_decision(mission_id, task_id, tier, model, saved_vs_baseline_usd)`; optional CLI `mission stats --routing`.

**SR invariants:** never overrides §1.5 different-provider rule; never turns Orchestrator's static DAG into turn-level auto-orchestration (Scheduler already owns that).

---

## 5. PHASE E — multi-day capability  *(after D exit gates pass)*

Source of truth: `MAF-Coder_v2_Build_Plan.md §Phase E`. Tool sketches in `AGENT_TOOLS_SPEC.md §12`.
Six work items, **naturally parallelizable into 3 disjoint workstreams** for the team:

| Workstream | Items | Primary files |
|---|---|---|
| **E-state** | E1 Checkpoint (git tag + container commit + `mission_state.json` + `resume`/`--from`/`rollback`), E6 resilience tests | `orchestrator/mission_driver.py`, new `orchestrator/checkpoint.py`, sandbox snapshot GC |
| **E-comms** | E2 Status Report protocol (4–8h timer, push adapters), E3 `user_messages/` reverse channel (`!urgent`) | new `orchestrator/status_report.py`, `orchestrator/inbox.py`, `agents/tools` §12 |
| **E-guard** | E4 Stuck Recovery 3-tier triage, E5 budget gate (50/80/100/150% thresholds, cautious mode) | new `orchestrator/budget.py`, `orchestrator/recovery.py`, EventLog |

**Exit (Build Plan §E):** one real 48h mission with ≥1 checkpoint-rollback-retry, ≥1 status push, ≥1 `user_messages` injection, ≥1 budget-80% cautious-mode switch; a second 48h mission on a different project. Live-mission gates — design + unit tests land in code PRs; the 48h runs are acceptance, run by you.

**Dependency:** E-state is the spine (checkpoint/resume); E-comms and E-guard depend on `mission_state.json` shape from E-state — so E-state lands first, then E-comms ‖ E-guard parallel.

---

## 6. PHASE F — cross-mission memory + PR workflow  *(after E)*

Source: `MAF-Coder_v2_Build_Plan.md §Phase F`. **Two disjoint workstreams:**

| Workstream | Items | Files |
|---|---|---|
| **F-memory** | F1 per-repo `.maf-coder/memory.db` (SQLite) + vector index (lancedb/chromadb), F2 retrieval at Orchestrator/Research/Coder entry, F3 anti-poisoning (`<historical_lesson confidence age_days mission_id>`), F4 global lessons + 50-item semantic dedup, F6 `mission_retro.md` | new `memory/` package |
| **F-pr** | F5 `gh`/`glab` wrappers, PR description gen (§9.2), auto-link to mission artifacts, **gitleaks final scan** before PR | new `integrations/vcs.py` |

These two are fully independent → parallel from F's start. **Exit (Build Plan §F):** 2nd mission's plan references 1st's retro; ≥20 reusable global lessons; ≥1 real GitHub PR a human calls "enough info"; retrieval helpful in ≥3/5 spot-checks; anti-poisoning passes a constructed-conflict test.

---

## 7. PHASE G — real-world validation  *(after F)*

Source: `MAF-Coder_v2_Build_Plan.md §Phase G`. Mostly **acceptance, not code** — run by you:
G1 a real 7-day mission (5+ crate refactor/feature, merged PR), G2 rotate across lib/CLI/service in one week,
G3 health-metric baseline (first-pass ≥60% median, final ≥90%, human-intervention ≤20%), G4 weekly retro →
promote lessons + write failure modes into Coder/Validator prompts, G5 onboarding README + maintained soul.md.
Only code work here: the **metrics harness** (G3) — a small `metrics/` reporter over EventLog/mission_state.

---

## 8. Kickoff sequence (this thread, on your go)

1. Sync stale docs awareness → spin **WS-DOCS** + **PR-D1** worktrees (Wave 0).
2. Spawn one focused Claude agent per worktree with: the PR spec (§2), invariants (§1), "gate green before done."
3. On D1 green + reviewed → merge to main, re-run full gate, open Wave 1 (D2 ‖ SR-1).
4. Repeat through Wave 3; SR merges before E begins.
5. Re-evaluate E workstream split once D is fully merged and cost data starts existing.

**First agent instruction (Wave 0, PR-D1), ready to dispatch:**
> In worktree `maf/d1-probes`, implement Phase D PR-D1 per `docs/MAF_CODER_EXECUTION_PLAN.md §2 PR-D1`:
> `src/maf_coder/validators/probes/` framework + `src/maf_coder/agents/tools/behavior_tools.py`
> (signatures verbatim from `AGENT_TOOLS_SPEC.md §11`). Mirror `review_tools.py`. Tests on
> `LocalShellSandbox`. Obey invariants §1 (extra=forbid, function_tool identity shim, permissions
> check, different-provider, evidence-on-fail). Finish only when `pytest && ruff check src tests &&
> mypy src/maf_coder` is all green.
