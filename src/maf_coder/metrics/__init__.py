"""Metrics harness (Build Plan §Phase G · G3).

Health-metric baseline over completed missions: a small, pure analysis layer
that reads each mission's `events.jsonl` + `mission_state.json` and derives the
metrics you can use to say "this version is better than the last one".
"""

from __future__ import annotations

from .baseline import (
    BaselineReport,
    MissionMetrics,
    compute_baseline,
    compute_mission_metrics,
    discover_missions,
    render_baseline_markdown,
)

__all__ = [
    "BaselineReport",
    "MissionMetrics",
    "compute_baseline",
    "compute_mission_metrics",
    "discover_missions",
    "render_baseline_markdown",
]
