"""Behavior probe strategies (Phase D PR-D1).

Five strategies dispatched by project type, all sharing the `ProbeStrategy`
ABC and emitting `ProbeResult`s consumed by the probe runner in
`agents/tools/behavior_tools.py`:

- cli_assert_cmd_probe       (cli.py)
- backend_service_health_probe (backend.py)
- library_example_probe      (library.py)
- embedded_host_test_probe   (embedded.py, minimal)
- wasm_node_probe            (wasm.py, minimal)
"""

from __future__ import annotations

from .backend import BackendServiceHealthProbe
from .base import ProbeResult, ProbeStrategy
from .cli import CliAssertCmdProbe
from .embedded import EmbeddedHostTestProbe
from .library import LibraryExampleProbe
from .registry import get_probe_strategy, known_strategies
from .wasm import WasmNodeProbe

__all__ = [
    "BackendServiceHealthProbe",
    "CliAssertCmdProbe",
    "EmbeddedHostTestProbe",
    "LibraryExampleProbe",
    "ProbeResult",
    "ProbeStrategy",
    "WasmNodeProbe",
    "get_probe_strategy",
    "known_strategies",
]
