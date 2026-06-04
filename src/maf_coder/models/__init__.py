"""Model layer — routes role-based agent calls through LiteLLM."""

from .router import (
    CallResult,
    ModelConfig,
    ModelRouter,
    ProviderForbiddenError,
    RoleConfig,
    RoleNotConfiguredError,
    RouterConfig,
    SmartRouterConfig,
    SmartRouterRoleFlag,
    estimate_cost_usd,
    resolve_cost_usd,
)

__all__ = [
    "CallResult",
    "ModelConfig",
    "ModelRouter",
    "ProviderForbiddenError",
    "RoleConfig",
    "RoleNotConfiguredError",
    "RouterConfig",
    "SmartRouterConfig",
    "SmartRouterRoleFlag",
    "estimate_cost_usd",
    "resolve_cost_usd",
]
