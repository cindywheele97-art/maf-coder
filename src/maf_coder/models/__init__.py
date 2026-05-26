"""Model layer — routes role-based agent calls through LiteLLM."""

from .router import (
    CallResult,
    ModelConfig,
    ModelRouter,
    ProviderForbiddenError,
    RoleConfig,
    RoleNotConfiguredError,
    RouterConfig,
)

__all__ = [
    "ModelRouter",
    "ModelConfig",
    "RoleConfig",
    "RouterConfig",
    "CallResult",
    "RoleNotConfiguredError",
    "ProviderForbiddenError",
]
