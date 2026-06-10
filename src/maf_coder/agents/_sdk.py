"""OpenAI Agents SDK shim.

Centralizes import of the SDK so the rest of the codebase doesn't care which
package name is installed (`openai-agents` exposes both `openai_agents` and
`agents` in different versions) and so unit tests can run without the SDK
present at all.

When the SDK is missing, `SDK_AVAILABLE` is False and the public names below
become inert placeholders. `BaseAgent.run` will refuse to call into the SDK
unless `SDK_AVAILABLE` is True OR a subclass overrides `_execute_sdk`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

SDK_AVAILABLE: bool = False
SDK_PACKAGE: str | None = None

Agent: Any = None
Runner: Any = None
ModelSettings: Any = None
LitellmModel: Any = None

_F = TypeVar("_F", bound=Callable[..., Any])


def function_tool(fn: _F) -> _F:
    """No-op decorator standing in for `@function_tool` at factory time.

    Tool factories declare `@function_tool` so their docstring + signature
    become the SDK contract, but we deliberately keep the decorated object as
    a plain callable so unit tests can invoke it directly without faking the
    SDK Runner. The real SDK wrapper is applied at the SDK boundary
    (`BaseAgent._execute_sdk` calls `_sdk.wrap_for_sdk(tool)`).

    This signature uses a TypeVar so mypy preserves the original callable's
    return type — necessary because `make_*` factories type-annotate their
    nested function and the outer factory advertises it as `Callable[..., Any]`.
    """
    return fn


# The real SDK `@function_tool` decorator, bound by `_try_import` when present.

sdk_function_tool: Callable[..., Any] | None = None


def wrap_for_sdk(tool: Callable[..., Any]) -> Any:
    """Decorate a tool callable with the real SDK `@function_tool` if available.

    Used by `BaseAgent._execute_sdk` immediately before handing the tools to
    `agents.Agent(...)`. If the SDK is absent, returns the bare callable.

    `strict_mode=False` is deliberate. Our tool params are Pydantic models with
    `ConfigDict(extra="forbid")` (a hard project convention), which emits
    `additionalProperties: false`; the SDK's strict-schema enforcement rejects
    that inside a union/`anyOf` ("additionalProperties should not be set"). It
    also keeps tool schemas compatible with non-OpenAI providers (MiMo / DeepSeek
    / OpenAI-compatible proxies) whose function-calling doesn't honor OpenAI
    strict mode. Validation still happens server-side in the tool body.
    """
    if sdk_function_tool is None:
        return tool
    return sdk_function_tool(tool, strict_mode=False)


def _try_import() -> None:
    """Detect whichever SDK package is installed and bind names lazily.

    We prefer `agents` (current package name on PyPI as of openai-agents>=0.3)
    and fall back to `openai_agents` (older releases). The Phase B spec uses
    the `openai_agents` import path; this shim tolerates either.
    """
    global SDK_AVAILABLE, SDK_PACKAGE, Agent, Runner, ModelSettings, LitellmModel
    global sdk_function_tool

    for pkg in ("agents", "openai_agents"):
        try:
            mod = __import__(pkg, fromlist=["Agent", "Runner", "function_tool"])
        except Exception:
            continue
        try:
            Agent = mod.Agent
            Runner = mod.Runner
            sdk_function_tool = getattr(mod, "function_tool", None)
        except AttributeError:
            continue
        # ModelSettings + LitellmModel are optional — they may live in submodules
        try:
            settings_mod = __import__(f"{pkg}.model_settings", fromlist=["ModelSettings"])
            ModelSettings = getattr(settings_mod, "ModelSettings", None)
        except Exception:
            ModelSettings = getattr(mod, "ModelSettings", None)
        # LitellmModel ships in the optional `litellm` extra and its import path
        # has moved between releases: current `agents` exposes it at
        # `agents.extensions.models.litellm_model`; older layouts used
        # `{pkg}.models`. Try the candidates in order — the wrong path silently
        # leaving this None makes every role fall back to the SDK's native OpenAI
        # provider (a confusing "missing OPENAI_API_KEY" crash mid-mission).
        LitellmModel = None
        for sub in (f"{pkg}.extensions.models.litellm_model", f"{pkg}.models"):
            try:
                lm_mod = __import__(sub, fromlist=["LitellmModel"])
            except Exception:
                continue
            LitellmModel = getattr(lm_mod, "LitellmModel", None)
            if LitellmModel is not None:
                break
        if LitellmModel is None:
            LitellmModel = getattr(mod, "LitellmModel", None)
        SDK_AVAILABLE = True
        SDK_PACKAGE = pkg
        return


_try_import()
