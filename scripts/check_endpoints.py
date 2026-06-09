#!/usr/bin/env python3
"""Connectivity probe for the models in a router config.

`maf-coder preflight` checks that each model's API key is *present*. It cannot
tell whether the endpoint actually answers — a wrong base_url, an invalid
model-id, an expired key, or an OpenAI-vs-Anthropic format mismatch only surfaces
on a real call. This script makes ONE tiny (~1-token) completion per distinct
model and reports OK/FAIL, so custom endpoints (MiMo / DeepSeek / proxies) are
proven reachable BEFORE a paid mission spends real money discovering they aren't.

It mirrors the agent path: same model string + api_base + api_key the router
would use. Cost: fractions of a cent (a handful of single-token replies).

Usage:
  python scripts/check_endpoints.py --router-config config/droid_whispering.test4.yaml
  python scripts/check_endpoints.py --router-config <cfg> --list   # no calls; just show what would be probed
"""

from __future__ import annotations

import argparse

from maf_coder.models.router import ModelConfig, ModelRouter


def _distinct_models(router: ModelRouter) -> list[ModelConfig]:
    """Every distinct (model, api_base) across all roles' primary + fallback."""
    seen: dict[tuple[str, str | None], ModelConfig] = {}
    for role in router.config.roles.values():
        for m in [role.primary, *role.fallback]:
            seen.setdefault((m.model, m.api_base), m)
    return list(seen.values())


def _label(m: ModelConfig) -> str:
    return m.model + (f"  @{m.api_base}" if m.api_base else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--router-config", required=True)
    ap.add_argument("--prompt", default="Reply with exactly: PONG")
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument(
        "--list",
        action="store_true",
        help="List the distinct models + whether their key env is set; make NO calls.",
    )
    args = ap.parse_args()

    router = ModelRouter(args.router_config)
    models = _distinct_models(router)
    print(f"{len(models)} distinct model(s) in {args.router_config}\n")

    if args.list:
        for m in models:
            key_state = "key set" if (not m.api_key_env or m.resolved_api_key()) else "KEY MISSING"
            env = f" [{m.api_key_env}: {key_state}]" if m.api_key_env else ""
            print(f"  • {_label(m)}{env}")
        return 0

    import litellm

    failures = 0
    for m in models:
        key = m.resolved_api_key()
        if m.api_key_env and not key:
            print(f"  ✗ {_label(m)}\n      env {m.api_key_env} is empty/unset")
            failures += 1
            continue
        try:
            resp = litellm.completion(
                model=m.model,
                messages=[{"role": "user", "content": args.prompt}],
                max_tokens=args.max_tokens,
                api_base=m.api_base,
                api_key=key,
                timeout=30,
            )
            text = (resp.choices[0].message.content or "").strip()
            print(f"  ✓ {_label(m)}\n      -> {text[:60]!r}")
        except Exception as e:  # report any provider error verbatim
            msg = str(e).splitlines()[0][:200] if str(e) else ""
            print(f"  ✗ {_label(m)}\n      {type(e).__name__}: {msg}")
            failures += 1

    print()
    if failures:
        print(f"✗ {failures}/{len(models)} model(s) failed — fix before launching a mission.")
        return 1
    print(f"✓ all {len(models)} model(s) answered — endpoints ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
