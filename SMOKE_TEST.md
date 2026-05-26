# Phase A Smoke Test

This document covers the two Phase A 退出门槛 that can only be checked outside
the unit-test sandbox:

1. **Docker sandbox image builds cleanly** — every cargo-* tool, C toolchain,
   wasm-pack, gitleaks, etc. installs without error.
2. **三供应商 smoke test 全过** — for every (role × model × test_case) declared in
   `config/droid_whispering.yaml`'s `smoke_test:` section, the call path through
   LiteLLM to the underlying provider works *now*, including:
   - Simple text completion
   - Tool / function calling
   - Structured JSON output

The smoke test is the most important Phase A gate because every Worker / Validator
built in later phases inherits the underlying provider's reliability. Finding a
flaky tool-calling path now saves days of debugging in Phase B+.

## Prerequisites

- Python 3.11+ (already required by `pyproject.toml`)
- `pip install -e ".[dev]"` already done (gives litellm + pyyaml)
- API keys exported in environment:
  - `ANTHROPIC_API_KEY`
  - `OPENAI_API_KEY`
  - `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)
- Docker (for the sandbox build gate only — smoke test itself runs on host)

The smoke test header will tell you which keys it detected:

```
Provider keys in environment:
  ✓ anthropic
  ✓ openai
  ✗ google     ← if missing, google/* calls will auth-fail
```

## Running

### Smoke test (host-side, no Docker needed)

```bash
# Full run — all roles × all models × all test cases × 5 attempts
python scripts/smoke_test.py

# Dry run — show the test plan, no API calls
python scripts/smoke_test.py --dry-run

# Subset of roles
python scripts/smoke_test.py --roles orchestrator,coder_worker

# Persist results for later analysis
python scripts/smoke_test.py --output smoke_$(date +%Y%m%d).json

# Tune attempts / timeout
python scripts/smoke_test.py --attempts 10 --timeout 60

# Lower concurrency if you're being rate-limited
python scripts/smoke_test.py --concurrency 2
```

Cost: at default settings (~32 combos × 5 attempts × ≤ 500 tokens each)
expect under **$1 total** across providers. If you set `--attempts 10`
double that.

### Docker sandbox build gate

```bash
bash scripts/build_sandbox.sh
```

What the script does, in order:
1. Creates `maf-cargo-cache`, `maf-target-cache`, `maf-sccache` Docker volumes
   (idempotent — skipped if they exist)
2. Builds the image from `config/rust_sandbox.dockerfile` (tag:
   `maf-coder:rust-sandbox`)
3. Runs a smoke check inside the built container, verifying every required
   tool responds (rustc / cargo / clippy / fmt / audit / deny / geiger /
   nextest / wasm-pack / sccache / gitleaks / gh / git / protoc)

Expected first-time build: **30–60 minutes** (mostly cargo install of cargo-*
helper tools — they each compile from source). Subsequent builds: 1–5 minutes
(Docker layer cache).

Override flags:
```bash
bash scripts/build_sandbox.sh --tag custom:v1
bash scripts/build_sandbox.sh --no-cache               # force full rebuild
bash scripts/build_sandbox.sh --build-arg RUST_VERSION=1.90  # if Dockerfile uses ARG
```

## Interpreting smoke test results

### All pass

```
============================================================================
Summary
============================================================================
Total combos:       24
Passing:            24
Failing:             0
Elapsed:            38.4s

✓ Phase A smoke test gate is OPEN.
```

Proceed to Phase B.

### Some combos fail

```
✗ Phase A smoke test gate is BLOCKED. Failing combos:
   - google/gemini-2.5-pro × tool_calling: 2/5
   - google/gemini-2.5-pro × structured_output: 1/5
```

Three escalating mitigation paths:

**Cheap — swap primary/fallback in `config/droid_whispering.yaml`.**
If a fallback is healthy and the primary is flaky, promote it:
```yaml
adversarial_subagent:
  primary:
    model: openai/gpt-5         # was google/gemini-2.5-pro
  fallback:
    - model: google/gemini-2.5-pro   # demoted
```

**Medium — drop the unreliable model entirely.**
```yaml
adversarial_subagent:
  primary:
    model: openai/gpt-5
  # fallback: []   # no fallback if you really don't trust alternatives
```
Caution: `review_validator` and `adversarial_subagent` are 异-provider roles —
they must have an option that's *not* Anthropic AND *not* whatever Coder is
running. If you drop google here, and Coder happens to be on openai, the
router will raise `ProviderForbiddenError`.

**Expensive — bisect the LiteLLM version.**
Tool-calling bugs are often version-specific. Try one version newer
(`pip install -U 'litellm>=1.55.0'`) or one older (`pip install
'litellm<1.50.0'`) to bracket. File an issue at https://github.com/BerriAI/litellm
if you can pinpoint a regression.

### Auth errors on a whole provider

```
   - anthropic/claude-opus-4-7 × simple_completion: 0/5
     #1: AuthenticationError: invalid x-api-key
```

Check the header — it tells you which keys it found. Missing key → set it
in env. Wrong key → fix in your env / secrets manager.

## What the smoke test does NOT cover

- **Cost** — the smoke test is short (≤500 tokens × ~120 calls). Real Coder
  Worker calls in Phase B+ may use 32k input + 16k output and run hundreds of
  times. The cost-tracking infrastructure is in EventLog (`total_cost_usd`,
  `cost_by_actor`) and gets exercised in Phase E.
- **Streaming** — only non-streaming completion is tested. Phase B may add
  streaming tests when Worker LLM calls switch to streaming for early
  cancellation.
- **Long context** — only short prompts here. Long-context behavior is tested
  in Phase C when Research Worker hits real codebases.
- **Real concurrency under load** — smoke test caps at `--concurrency 4`.
  Production might hit 8-16 concurrent calls; that's a Phase E concern.
- **Cost-per-token accuracy** — `litellm` reports `_response_cost` from its
  static price table, which can lag actual provider pricing by weeks. The
  ratio is what matters for budget routing, not absolute numbers.

## Troubleshooting

### Docker build fails on `cargo install <tool>`

Most likely cause: a cargo tool moved its release artifact or requires a newer
Rust. Two fixes:

1. Bump Rust: `bash scripts/build_sandbox.sh --build-arg RUST_VERSION=1.90`
   (only works if the Dockerfile uses `ARG RUST_VERSION=...`)
2. Pin the failing tool to a known-good version inside the Dockerfile:
   `cargo install cargo-audit --locked --version 0.20.0`

### Docker build fails on `gitleaks` or `trufflehog` install

The release-asset URL pattern occasionally changes upstream. Inspect the line
in `config/rust_sandbox.dockerfile` and update the download URL to match the
current pattern. As of writing (May 2026), the patterns are:
- gitleaks: `https://github.com/gitleaks/gitleaks/releases/download/v${VER}/gitleaks_${VER}_linux_x64.tar.gz`
- trufflehog: install script at `https://raw.githubusercontent.com/trufflesecurity/trufflehog/main/scripts/install.sh`

### Smoke test fails with `ModuleNotFoundError: No module named 'litellm'`

You forgot `pip install -e ".[dev]"`. Run it from the project root.

### `Timeout after 30.0s` on every call

Either the provider is having an outage (check their status page) or your
network is being slow to reach them. Bump `--timeout 60` or `--timeout 120`.

### `cost_usd = 0.0` in results

LiteLLM's `_response_cost` field returns 0 when its pricing table doesn't know
the model. Not a smoke-test failure — but if you rely on this for budget
tracking later, install a newer LiteLLM where your models are priced.

### Anthropic responses say "I cannot do that" / "I'm not able to..."

You may have an old model alias. Verify your `droid_whispering.yaml` uses
current LiteLLM-supported model strings. As of May 2026:
- `anthropic/claude-opus-4-7`
- `anthropic/claude-sonnet-4-6`
- `openai/gpt-5`
- `google/gemini-2.5-pro`

Run `litellm --models` or check https://docs.litellm.ai/docs/providers for
the current canonical names.

## Known-good baseline (May 2026)

These should pass 5/5 on all three test cases on a healthy day. If they don't,
it's almost certainly a provider outage rather than a config problem:

| Model | simple_completion | tool_calling | structured_output |
|---|---|---|---|
| `anthropic/claude-opus-4-7` | 5/5 | 5/5 | 5/5 |
| `anthropic/claude-sonnet-4-6` | 5/5 | 5/5 | 5/5 |
| `openai/gpt-5` | 5/5 | 5/5 | 5/5 |
| `google/gemini-2.5-pro` | 5/5 | 4-5/5 | 4-5/5 |

Note that historically Google's tool-calling through LiteLLM had occasional
hiccups in 2024-25. If you see ≤3/5 on Gemini, that's the kind of signal to
not just retry but consider Mitigation Path 1 or 2 above.
