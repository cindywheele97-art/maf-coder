"""Probe strategy registry — strategy name -> class (PR-D1).

Maps the `BehaviorProbeSpec.strategy` string (and the matching contract
`verification_target` prefix) to a concrete `ProbeStrategy`. The runner in
`behavior_tools.py` resolves the strategy through `get_probe_strategy`.
"""

from __future__ import annotations

from .backend import BackendServiceHealthProbe
from .base import ProbeStrategy
from .cli import CliAssertCmdProbe
from .embedded import EmbeddedHostTestProbe
from .library import LibraryExampleProbe
from .wasm import WasmNodeProbe

# Single source of truth for the five strategies. Keyed by registry name,
# which equals the `BehaviorProbeSpec.strategy` value.
_REGISTRY: dict[str, type[ProbeStrategy]] = {
    CliAssertCmdProbe.name: CliAssertCmdProbe,
    BackendServiceHealthProbe.name: BackendServiceHealthProbe,
    LibraryExampleProbe.name: LibraryExampleProbe,
    EmbeddedHostTestProbe.name: EmbeddedHostTestProbe,
    WasmNodeProbe.name: WasmNodeProbe,
}


def get_probe_strategy(strategy: str) -> ProbeStrategy:
    """Instantiate the probe strategy registered under `strategy`.

    Raises KeyError with the known names when the strategy is unknown — the
    caller turns this into a tool-level error.
    """
    cls = _REGISTRY.get(strategy)
    if cls is None:
        raise KeyError(
            f"unknown probe strategy {strategy!r}; known: {sorted(_REGISTRY)}"
        )
    return cls()


def known_strategies() -> list[str]:
    return sorted(_REGISTRY)


__all__ = ["get_probe_strategy", "known_strategies"]
