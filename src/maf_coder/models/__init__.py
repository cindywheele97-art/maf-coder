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
]
