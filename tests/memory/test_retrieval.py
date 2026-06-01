"""Retrieval scoring + anti-poisoning tests (Phase F — F2, F3).

WHY each test matters:
- overlap ranking: a query must surface the record that shares the most terms,
  else injected context is irrelevant noise.
- time decay: two equally-relevant records must be separated by age, so stale
  lessons can't outrank fresh ones — confidence MUST drop with age.
- carried metadata: confidence / age_days / source_mission_id must ride along
  so the anti-poisoning framing can weight + attribute each lesson.
- cold start: an empty / None store returns [] without raising (a fresh repo
  has no db yet and the mission must still start).
- anti-poisoning render: results render as <historical_lesson> blocks with an
  explicit NON-binding framing; quarantined rows never appear.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from maf_coder.memory.retrieval import (
    HALF_LIFE_DAYS,
    rank,
    render_results,
    retrieve,
)
from maf_coder.memory.store import ProjectMemory
from maf_coder.schemas import MemoryRecord

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _rec(
    rid: str,
    text: str,
    *,
    mission: str = "m1",
    age_days: float = 0.0,
    kind: str = "retro",
    **kw: object,
) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid,
        mission_id=mission,
        kind=kind,
        text=text,
        created_at=NOW - timedelta(days=age_days),
        **kw,  # type: ignore[arg-type]
    )


def test_ranks_by_token_overlap() -> None:
    records = [
        _rec("r1", "use parameterized sql queries to prevent injection"),
        _rec("r2", "frontend react component styling tips"),
        _rec("r3", "sql connection pooling configuration"),
    ]
    results = rank("sql injection prevention", records, now=NOW)
    assert results[0].record.record_id == "r1"
    # r2 (zero overlap) must not appear at all
    assert "r2" not in {r.record.record_id for r in results}


def test_time_decay_lowers_confidence_for_older_records() -> None:
    fresh = _rec("fresh", "lock the validation contract first", age_days=0.0)
    stale = _rec("stale", "lock the validation contract first", age_days=HALF_LIFE_DAYS)
    results = rank("lock validation contract", [fresh, stale], now=NOW)
    by_id = {r.record.record_id: r for r in results}
    # equal overlap -> fresher must win, and the stale one ~half confidence
    assert results[0].record.record_id == "fresh"
    assert by_id["stale"].confidence < by_id["fresh"].confidence
    assert by_id["stale"].confidence == abs(by_id["fresh"].confidence * 0.5)


def test_results_carry_confidence_age_and_mission() -> None:
    rec = _rec("r1", "rotate exposed secrets", mission="m-42", age_days=3.0)
    (result,) = rank("rotate secrets", [rec], now=NOW)
    assert result.source_mission_id == "m-42"
    assert result.age_days == 3.0
    assert 0.0 < result.confidence <= 1.0


def test_module_filter_restricts_candidates() -> None:
    records = [
        _rec("db1", "index the orders table", module="db"),
        _rec("ui1", "index the orders table", module="ui"),
    ]
    results = rank("index orders", records, module="db", now=NOW)
    assert [r.record.record_id for r in results] == ["db1"]


def test_top_k_caps_results() -> None:
    records = [_rec(f"r{i}", "shared overlap token here") for i in range(10)]
    results = rank("shared overlap token", records, top_k=3, now=NOW)
    assert len(results) == 3


def test_quarantined_records_excluded_from_rank() -> None:
    records = [
        _rec("good", "safe lesson about caching"),
        _rec("poisoned", "safe lesson about caching", quarantined=True),
    ]
    results = rank("caching lesson", records, now=NOW)
    assert [r.record.record_id for r in results] == ["good"]


def test_injected_embedding_blends_into_score() -> None:
    # A trivial deterministic embedder: bag-of-2-dims keyed on presence of a term.
    def embed(text: str) -> list[float]:
        t = text.lower()
        return [1.0 if "alpha" in t else 0.0, 1.0 if "beta" in t else 0.0]

    records = [
        _rec("a", "alpha keyword doc"),
        _rec("b", "beta keyword doc"),
    ]
    results = rank("alpha", records, embed=embed, now=NOW)
    assert results[0].record.record_id == "a"
    # embedding score is recorded when an embedder is injected
    assert results[0].embedding_score is not None


def test_cold_start_none_store_returns_empty() -> None:
    assert retrieve("anything", None) == []


def test_cold_start_empty_store_returns_empty(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    assert retrieve("anything", pm) == []
    pm.close()


def test_retrieve_kind_filter(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    pm.insert(_rec("r1", "shared token alpha", kind="retro"))
    pm.insert(_rec("r2", "shared token alpha", kind="contract"))
    results = retrieve("shared token", pm, kind="contract")
    assert [r.record.record_id for r in results] == ["r2"]
    pm.close()


def test_render_results_has_nonbinding_framing_and_attributes() -> None:
    rec = _rec("r1", "prefer parameterized queries", mission="m-7", age_days=2.0)
    (result,) = rank("parameterized queries", [rec], now=NOW)
    rendered = render_results([result])
    assert "<historical_lessons>" in rendered
    assert "<historical_lesson " in rendered
    assert 'mission_id="m-7"' in rendered
    assert "age_days=" in rendered
    assert "confidence=" in rendered
    # explicit non-binding framing (anti-poisoning F3)
    assert "NON-BINDING" in rendered
    assert "prefer parameterized queries" in rendered


def test_render_results_empty_is_empty_string() -> None:
    assert render_results([]) == ""
