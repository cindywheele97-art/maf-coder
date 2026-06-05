# First Real Mission — Runbook

> **Purpose:** get MAF-Coder from "532 unit tests green" to "ran one real mission
> with real LLM calls on a real Rust repo". This is a **shakedown run** — expect
> to hit integration bugs the unit tests (which stub the SDK and mock the sandbox)
> could never catch. Start as small and cheap as possible.

---

## 0a. One-command readiness gate — `maf-coder preflight`

Before anything else, run the readiness gate. It's **inspect-only** (no LLM
calls, no spend) and answers "can this go straight to a real run?" in one pass —
router config valid + 异-provider rule intact, every provider API key present,
the target repo profilable, and (for `--sandbox docker`) the daemon reachable +
the sandbox image built. Each failing check prints the exact remediation; the
command exits non-zero on NO-GO.

```bash
maf-coder preflight --repo <throwaway-clone>            # checks docker backend
maf-coder preflight --repo <throwaway-clone> --sandbox local   # skip docker checks
```

Resolve every ✗ until it prints **"✓ Preflight GO"**, then proceed.

---

## 0. ✅ Orchestrator bootstrap (now wired)

Real-mode `start()` now **seeds one `Role.ORCHESTRATOR` task** (`task_id="orchestrate"`,
goal = `config.goal`) via `scheduler.add_task(...)` before the scheduler loop
(`mission_driver._orchestrator_bootstrap_task()`). When it runs, the Orchestrator
plans, locks `validation_contract.yaml`, and dispatches the worker/validator DAG
via `dispatch_task` — which extends the same loop. So `--no-dry-run` now actually
runs the mission instead of no-op'ing. Covered by
`tests/orchestrator/test_mission_driver.py::test_real_mode_seeds_and_runs_orchestrator`
(stub Orchestrator, no LLM).

The Coder provider is now wired too: `MissionDriver` derives
`coder_provider_in_use` from the router's `coder_worker` primary model (or use
the `maf-coder mission new --coder-provider <p>` override), so **both** the
static (`forbidden_providers`) and dynamic halves of the 异-provider rule are
active on a real run.

> Note for run #1: the Driver now **re-invokes the Orchestrator once per
> milestone** (`_milestone_loop`): it sets `current_milestone`, runs the
> Orchestrator turn, drains the dispatched DAG, and repeats until the Orchestrator
> calls `complete_mission` (or a turn dispatches no work). The single-milestone
> task below still exercises the full Coder → Review → Behavior chain end to end —
> it just completes after one milestone instead of being capped at one turn.

---

## 1. Prerequisites

- **Host tooling:** Python 3.11, Rust toolchain (`cargo`/`rustc` — present),
  `git`, optionally `gh` (already authed) for the PR step, optionally `gitleaks`
  (the PR gate shells out to it).
- **API keys for all THREE providers** — the config spans them and validators are
  forced onto a *different* provider than the Coder, with cross-provider
  fallbacks:
  - `anthropic/claude-opus-4-7`, `anthropic/claude-sonnet-4-6` → **`ANTHROPIC_API_KEY`**
  - `openai/gpt-5` → **`OPENAI_API_KEY`**
  - `google/gemini-2.5-pro`, `google/gemini-2.5-flash` (Smart Router judge) → **`GEMINI_API_KEY`**
  (LiteLLM reads these from the environment. Missing one → that role/fallback fails mid-mission.)
- **Sandbox: secure by default.** A **real run** (`--no-dry-run`) defaults to
  **`--sandbox docker`** (`DockerSandbox`, image `maf-coder:rust-sandbox`) so the
  autonomous agents run `cargo`/shell **inside an isolated container**, not on your
  host. It **fails loud** if the daemon is down — so **build the image first**:
  `bash scripts/build_sandbox.sh` (first build is 30–60 min). Dry-runs / `mission
  profile` default to **`local`** (they execute no agent code, so no Docker needed).
  - **Resource + privilege limits (M1).** Every container runs with `network_mode=none`
    plus bounded resources: `mem_limit` **8g**, `pids_limit` **4096**, `cap_drop=["ALL"]`,
    and `no-new-privileges` — so a runaway or hostile `cargo build` can't OOM the host,
    fork-bomb it, or gain privileges. Defaults are generous enough for normal Rust
    builds; tune via the `DockerSandbox(..., mem_limit=, pids_limit=, nano_cpus=)`
    constructor (`nano_cpus` CPU cap is off by default). If a legitimate build gets
    OOM-killed inside the container, raise `mem_limit`.
- **A throwaway target repo.** Even with Docker, point `--repo` at a fresh
  disposable clone or worktree. If you deliberately use **`--sandbox local`** (host
  shell, **no isolation** — only app-level network policy), a disposable `--repo` is
  mandatory: the Coder edits it in place on the host.
- **Budget mindset.** A real Opus/GPT-5 multi-agent mission burns real money.
  Keep the first task trivial.

---

## 2. Environment

```bash
cd ~/Projects/maf-coder
source .venv/bin/activate            # or: pip install -e ".[dev]"

export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=...

export MAF_MISSIONS_ROOT="$PWD/missions"     # where mission artifacts land (gitignored)
export MAF_GLOBAL_LESSONS_DB="$HOME/.maf-coder/global_lessons.db"  # optional (F4)

# Prepare a disposable target (example):
git clone <your-small-rust-repo> /tmp/maf-target
# or for an in-repo experiment, a worktree of a small crate you own.
```

Sanity: `maf-coder --help` should list `mission`, `resume`, `rollback`, `pr`, `metrics`.

---

## 3. Smoke checks that work TODAY (no mission loop needed)

These validate config, keys, profiling, and artifact plumbing cheaply:

```bash
# (a) Project profiler — no LLM, no cost. Confirms the target repo is readable.
maf-coder mission profile --repo /tmp/maf-target

# (b) Dry-run mission — profiles + writes mission_state.json, NO agents/LLM calls.
maf-coder mission new "add a --version flag to the CLI" --repo /tmp/maf-target
#   (dry_run defaults to TRUE) → result: dry_run_complete
maf-coder mission status <mission_id>          # inspect mission_state.json

# (c) Metrics harness over whatever missions exist (empty/dry runs = zeros).
maf-coder metrics --markdown
```

To prove **real LLM routing + the agent stack** before spending on a full mission:

```bash
python scripts/live_smoke.py --keys-only          # which provider keys are set (no call)
python scripts/live_smoke.py                       # one cheap real call through BaseAgent.run
python scripts/live_smoke.py --role coder_worker --max-tokens 8
```

It runs a minimal no-tool agent end to end (`BaseAgent.run` → router model
resolution → OpenAI Agents SDK `Runner` + `LitellmModel` → `parse_output`), so a
failure points at the wiring, not a role's prompt/tools. Complements
`scripts/smoke_test.py`, which validates the model layer (every model via
LiteLLM: completion / tool-calling / JSON).

---

## 4. (Done) Bootstrap is wired

§0 is implemented and tested — no action needed here. Proceed to §5.

---

## 5. First real mission (after §4)

**Pick the smallest possible task.** Recommended shakedown goal (mirrors
`WORKED_EXAMPLE.md`): *"add a `/version` endpoint returning the crate version"*
on a tiny axum service, or *"add a `--version` subcommand"* on a tiny CLI crate.

```bash
# Fresh disposable clone, on a branch:
git -C /tmp/maf-target checkout -b maf/first-mission

maf-coder mission new "add a /version endpoint returning the crate version" \
  --repo /tmp/maf-target \
  --no-dry-run \
  --budget-usd 5 \
  --id first-real-1
```

**Watch it (in another terminal):**

```bash
tail -f "$MAF_MISSIONS_ROOT/first-real-1/events.jsonl"        # every LLM/tool/verdict event
ls    "$MAF_MISSIONS_ROOT/first-real-1/"                       # plan.md, validation_contract.yaml, patches/, verdicts/, handoff*, status_reports/
maf-coder mission status first-real-1                          # cost / milestone / budget_mode
maf-coder mission stats first-real-1 --routing                 # Smart Router tier decisions + savings
```

**Steer / intervene if needed:**
- Drop a note for the Orchestrator: write a file into
  `"$MAF_MISSIONS_ROOT/first-real-1/user_messages/"` (prefix `!urgent` to be read
  at the next task boundary instead of the next milestone).
- If it stalls or goes off the rails: `Ctrl-C`, then `maf-coder rollback
  first-real-1 --to <milestone> --repo /tmp/maf-target` or `maf-coder resume
  first-real-1 --repo /tmp/maf-target`.

---

## 6. After the run

```bash
# Inspect the diff the Coder produced (it edited /tmp/maf-target directly):
git -C /tmp/maf-target diff

# Validation evidence:
cat "$MAF_MISSIONS_ROOT/first-real-1/verdicts/"*.review.json
cat "$MAF_MISSIONS_ROOT/first-real-1/verdicts/"*.behavior.json   # if a behavior task ran

# Open a PR (runs the gitleaks pre-PR gate; refuses on secrets):
maf-coder pr first-real-1 --repo /tmp/maf-target --head maf/first-mission

# Record the baseline + a retro for cross-mission memory:
maf-coder metrics --markdown
#   (save_retro is an Orchestrator tool; it should run at mission end. Verify
#    mission_retro.md exists and rows landed in /tmp/maf-target/.maf-coder/memory.db)
```

---

## 7. Cost & safety guardrails (read before the first `--no-dry-run`)

1. **Trivial task + tiny repo** for run #1. You're testing the *machinery*, not building a feature.
2. **Disposable `--repo`** — even in Docker (the default), keep `--repo` throwaway; with `--sandbox local` it's mandatory, since the Coder edits it in place on the host. **Build the sandbox image first** (`bash scripts/build_sandbox.sh`) so the default `docker` run doesn't fail loud.
3. **Set a budget** with `--budget-usd` on `mission new` (or edit the auto-seeded `budget.yaml`). Start low, e.g. `--budget-usd 5`. The budget guard acts at four bands of the budget (read live from `mission_state.budget_mode` each tick):
   - **50%** — annotate only (a `BUDGET_ALERT` event; no behavior change).
   - **80%** — `budget_mode → cost_conscious`, which is now *enforced*: the Scheduler serializes every role (no parallel research/security/review) and caps each task's retries to 1, and validators switch to their **fallback** model. ⚠️ For that model swap to actually save money, order each validator's `fallback:` in `config/droid_whispering.yaml` so the first entry is the cheaper model (the 异-provider rule is preserved regardless).
   - **100%** — `budget_mode → paused`: the Scheduler refuses to launch NEW tasks (in-flight tasks drain) + escalates to the Human Gate.
   - **150%** — force-pause regardless of approval.
   To watch the throttle engage, grep `events.jsonl` for `budget_mode_changed` / `budget_alert`, or check `maf-coder mission status` (`budget_mode`).
   - **Custom/self-hosted models:** the band math is driven by cumulative cost. When LiteLLM can't price a model (custom endpoints like MiMo / DeepSeek / self-hosted), the cost is **estimated from tokens** (≈$1/Mtok floor, or the approx table for known families) so the guard is never blinded by a $0 cost — see `models/router.estimate_cost_usd`. It's an *estimate*, so treat the bands as approximate on those models; tune the floor if your real pricing differs.
4. **Watch `events.jsonl` live** — kill it the moment it loops or thrashes; cost is real per LLM call.
5. **Expect failure on run #1.** This is the first time the full loop executes against real models. Capture what breaks (the EventLog is your forensic record) and iterate.

---

## Known gaps this run will expose (track them)

- **Milestone re-invocation is wired** — the Driver re-invokes the Orchestrator
  once per milestone (sets `current_milestone`, drains the dispatched DAG, repeats)
  until the Orchestrator calls `complete_mission`. A turn that dispatches no work
  and doesn't declare completion ends the loop; `_MAX_MILESTONES` (50) is the
  backstop. `current_milestone` uses plan.md's milestone names (derived from
  `tasks.yaml`'s `parent_milestone` fields — the first planned milestone not yet
  in `completed_milestones`), falling back to a synthetic index only for the
  bootstrap/planning turn before the plan exists.
- **Docker is the default for real runs** — `mission new --no-dry-run` / `resume`
  default to `--sandbox docker` (isolated container; fails loud if the daemon is
  down, so `bash scripts/build_sandbox.sh` first). Dry-runs default to `local` (no
  agent code runs). Use `--sandbox local` to deliberately run a real mission on the
  unisolated host shell.
- **`save_retro` / `create_pr` are Orchestrator *tools*** — they fire only if the
  Orchestrator's prompt/plan actually calls them at mission end; verify it does,
  or invoke `maf-coder pr` manually (above).
