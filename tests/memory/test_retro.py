"""Mission retro assembler + ingest tests (Phase F — F6).

WHY: the retro is the ONLY mechanism that turns a finished mission into context
for the next one. So we verify (a) the assembler mechanically extracts failures
+ completions + surprises from the event stream (not just echoes the caller),
(b) ingest writes one retrievable row per bullet, and (c) flagged global lessons
— and only those — reach the global store.
"""

from __future__ import annotations

from pathlib import Path

from maf_coder.blackboard.event_log import EventLog
from maf_coder.memory.retro import (
    assemble_retro,
    ingest_retro,
    render_retro_markdown,
)
from maf_coder.memory.store import GlobalLessons, ProjectMemory
from maf_coder.schemas import RetroEntry


def _event_log(tmp_path: Path) -> EventLog:
    log = EventLog(tmp_path / "events.jsonl")
    log.log_task_complete(mission_id="m1", task_id="t-impl", actor="coder_worker", duration_sec=12.0)
    log.log_task_failed(
        mission_id="m1",
        task_id="t-flaky",
        actor="coder_worker",
        reason="cargo test timeout",
        will_retry=True,
    )
    log.log_second_pass_triggered(mission_id="m1", task_id="t-impl", reason="too-clean handoff")
    return log


def test_assemble_extracts_from_event_stream(tmp_path: Path) -> None:
    log = _event_log(tmp_path)
    entry = assemble_retro(mission_id="m1", goal="add /health endpoint", event_log=log)

    # mechanical extraction — failures + completions + surprises pulled from events
    assert any("t-impl" in w for w in entry.what_worked)
    assert any("t-flaky" in f and "timeout" in f for f in entry.what_failed)
    assert any("second-pass" in s for s in entry.surprises)
    assert entry.goal == "add /health endpoint"


def test_assemble_merges_caller_narrative(tmp_path: Path) -> None:
    log = _event_log(tmp_path)
    entry = assemble_retro(
        mission_id="m1",
        goal="g",
        event_log=log,
        extra_worked=["clean DI boundary"],
        extra_failed=["underestimated test setup"],
        global_lessons=["lock the contract before coding"],
        modules=["api"],
    )
    assert "clean DI boundary" in entry.what_worked
    assert "underestimated test setup" in entry.what_failed
    assert entry.global_lessons == ["lock the contract before coding"]
    assert entry.modules == ["api"]


def test_render_template_has_all_sections() -> None:
    entry = RetroEntry(
        mission_id="m1",
        goal="add health",
        what_worked=["a"],
        what_failed=["b"],
        surprises=["c"],
        global_lessons=["d"],
        modules=["api"],
    )
    md = render_retro_markdown(entry)
    for heading in ("## Goal", "## What Worked", "## What Failed", "## Surprises", "## Global Lessons"):
        assert heading in md
    assert "add health" in md


def test_render_empty_sections_show_none() -> None:
    entry = RetroEntry(mission_id="m1", goal="g")
    md = render_retro_markdown(entry)
    assert "(none recorded)" in md


def test_ingest_writes_one_record_per_bullet_and_is_retrievable(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    entry = RetroEntry(
        mission_id="m1",
        goal="add health endpoint",
        what_worked=["clean DI"],
        what_failed=["flaky test"],
        surprises=["surprise x"],
        global_lessons=["lock the contract first"],
        modules=["api"],
    )
    records = ingest_retro(entry, pm)
    assert len(records) == 4  # 1 worked + 1 failed + 1 surprise + 1 lesson
    assert pm.count() == 4

    # the global lesson row is flagged; the rest are project-local
    flagged = [r for r in records if r.global_lesson]
    assert len(flagged) == 1
    assert flagged[0].text == "lock the contract first"
    pm.close()


def test_ingest_promotes_only_flagged_to_global(tmp_path: Path) -> None:
    pm = ProjectMemory(tmp_path)
    gl = GlobalLessons(tmp_path / "global.db")
    entry = RetroEntry(
        mission_id="m1",
        goal="g",
        what_worked=["local only worked note"],
        global_lessons=["a cross-repo lesson worth keeping"],
    )
    ingest_retro(entry, pm, global_lessons=gl)

    lessons = gl.all_lessons()
    assert len(lessons) == 1
    assert lessons[0].text == "a cross-repo lesson worth keeping"
    # the project-local "worked" note did NOT leak into the global store
    assert all("local only" not in lsn.text for lsn in lessons)
    pm.close()
    gl.close()
