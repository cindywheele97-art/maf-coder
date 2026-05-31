"""Validators package — probe framework backing the BehaviorValidator (Phase D).

The `probes/` subpackage holds the headless probe strategies dispatched by
project type (cli / backend / library / embedded / wasm). Each strategy runs
entirely through the sandbox (never the host shell) and emits one
`BehaviorObservation` per `behavior_probe` assertion in the validation
contract.
"""

from __future__ import annotations

__all__: list[str] = []
