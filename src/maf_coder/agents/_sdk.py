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

from typing import Any, Callable

SDK_AVAILABLE: bool = False
SDK_PACKAGE: str | None = None

Agent: Any = None
Runner: Any = None
ModelSettings: Any = None
LitellmModel: Any = None


def _identity(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Fallback no-op decorator standing in for `@function_tool`."""
    return fn


function_tool: Callable[..., Any] = _identity


def _try_import() -> None:
    """Detect whichever SDK package is installed and bind names lazily.

    We prefer `agents` (current package name on PyPI as of openai-agents>=0.3)
    and fall back to `openai_agents` (older releases). The Phase B spec uses
    the `openai_agents` import path; this shim tolerates either.
    """
    global SDK_AVAILABLE, SDK_PACKAGE, Agent, Runner, ModelSettings, LitellmModel, function_tool

    for pkg in ("agents", "openai_agents"):
        try:
            mod = __import__(pkg, fromlist=["Agent", "Runner", "function_tool"])
        except Exception:
            continue
        try:
            Agent = getattr(mod, "Agent")
            Runner = getattr(mod, "Runner")
            function_tool = getattr(mod, "function_tool", _identity)
        except AttributeError:
            continue
        # ModelSettings + LitellmModel are optional — they may live in submodules
        try:
            settings_mod = __import__(
                f"{pkg}.model_settings", fromlist=["ModelSettings"]
            )
            ModelSettings = getattr(settings_mod, "ModelSettings", None)
        except Exception:
            ModelSettings = getattr(mod, "ModelSettings", None)
        try:
            models_mod = __import__(f"{pkg}.models", fromlist=["LitellmModel"])
            LitellmModel = getattr(models_mod, "LitellmModel", None)
        except Exception:
            LitellmModel = getattr(mod, "LitellmModel", None)
        SDK_AVAILABLE = True
        SDK_PACKAGE = pkg
        return


_try_import()
