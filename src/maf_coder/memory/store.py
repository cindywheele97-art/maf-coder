"""Memory stores — SQLite-backed persistence (Phase F — F1, F4).

Two stores, both stdlib `sqlite3` only (no third-party vector DB — see the
RISK note in MAF-Coder_v2_Build_Plan §Phase F: start with the simplest keyword
+ optional simple embedding hybrid):

- `ProjectMemory`: per-repo store at `<repo>/.maf-coder/memory.db`. Holds every
  retro / contract / handoff / profile row a mission produces.
- `GlobalLessons`: cross-repo store at a CONFIGURABLE path. Tests inject a temp
  path; production injects a real one. NEVER defaulted to the real home dir
  implicitly — the path is a required constructor argument.

Both expose insert + query. Ranking / time-decay / hybrid scoring lives in
`retrieval.py`; the stores stay dumb (rows in, rows out) so retrieval is unit-
testable against an in-memory list as well.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..schemas import Lesson, MemoryRecord

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS records (
    record_id     TEXT PRIMARY KEY,
    mission_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    text          TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    module        TEXT,
    global_lesson INTEGER NOT NULL DEFAULT 0,
    quarantined   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
"""

_LESSONS_DDL = """
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id         TEXT PRIMARY KEY,
    source_mission_id TEXT NOT NULL,
    text              TEXT NOT NULL,
    tags              TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
"""

_TAG_SEP = "\x1f"  # unit-separator — won't collide with tag text


def _encode_tags(tags: Sequence[str]) -> str:
    return _TAG_SEP.join(t.strip().lower() for t in tags if t.strip())


def _decode_tags(raw: str) -> list[str]:
    return [t for t in raw.split(_TAG_SEP) if t] if raw else []


def _parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# ProjectMemory
# ---------------------------------------------------------------------------


class ProjectMemory:
    """Per-repo SQLite memory store.

    Construct with the repo root; the db lives at `<repo>/.maf-coder/memory.db`.
    The directory is created lazily. The connection is opened per-instance and
    closed via `close()` (or used as a context manager).
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root)
        self.db_path = self.repo_root / ".maf-coder" / "memory.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_RECORDS_DDL)
        self._conn.commit()

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ProjectMemory:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -----------------------------------------------------------

    def insert(self, record: MemoryRecord) -> None:
        """Insert (or replace) one record. record_id is the primary key."""
        self._conn.execute(
            "INSERT OR REPLACE INTO records "
            "(record_id, mission_id, kind, text, tags, module, "
            " global_lesson, quarantined, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.mission_id,
                record.kind,
                record.text,
                _encode_tags(record.tags),
                record.module,
                int(record.global_lesson),
                int(record.quarantined),
                record.created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def insert_many(self, records: Iterable[MemoryRecord]) -> None:
        for r in records:
            self.insert(r)

    def quarantine(self, record_id: str) -> bool:
        """Mark a record quarantined (anti-poisoning, F3). Returns True if a row changed."""
        cur = self._conn.execute(
            "UPDATE records SET quarantined = 1 WHERE record_id = ?", (record_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    # -- reads ------------------------------------------------------------

    def all_records(self, *, include_quarantined: bool = False) -> list[MemoryRecord]:
        sql = "SELECT * FROM records"
        if not include_quarantined:
            sql += " WHERE quarantined = 0"
        return [_row_to_record(r) for r in self._conn.execute(sql)]

    def get(self, record_id: str) -> MemoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def global_lesson_records(self) -> list[MemoryRecord]:
        """Records flagged global_lesson and not quarantined (F4 ingest source)."""
        rows = self._conn.execute(
            "SELECT * FROM records WHERE global_lesson = 1 AND quarantined = 0"
        )
        return [_row_to_record(r) for r in rows]


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        record_id=row["record_id"],
        mission_id=row["mission_id"],
        kind=row["kind"],
        text=row["text"],
        tags=_decode_tags(row["tags"]),
        module=row["module"],
        global_lesson=bool(row["global_lesson"]),
        quarantined=bool(row["quarantined"]),
        created_at=_parse_dt(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# GlobalLessons
# ---------------------------------------------------------------------------

# F4: when the lessons count crosses this threshold, run keyword/token-set
# dedup so near-duplicate lessons collapse rather than accumulate forever.
DEDUP_THRESHOLD = 50
# Jaccard token-overlap above which two lessons are treated as near-duplicates.
DEDUP_SIMILARITY = 0.8


class GlobalLessons:
    """Cross-repo lessons store at a CONFIGURABLE path.

    The path is a required argument — there is NO implicit home-dir default, so
    tests can never accidentally write to a real `~`. Production callers pass an
    explicit path (e.g. `~/.maf-coder/global_lessons.db` resolved by config).
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_LESSONS_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> GlobalLessons:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes -----------------------------------------------------------

    def insert(self, lesson: Lesson) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO lessons "
            "(lesson_id, source_mission_id, text, tags, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                lesson.lesson_id,
                lesson.source_mission_id,
                lesson.text,
                _encode_tags(lesson.tags),
                lesson.created_at.isoformat(),
            ),
        )
        self._conn.commit()

    def ingest_record(self, record: MemoryRecord) -> bool:
        """Ingest a project record as a global lesson — only if flagged (F4).

        Returns True if a lesson was inserted, False if the record was not
        flagged global (caller need not pre-filter). Dedup runs opportunistically
        after the count crosses DEDUP_THRESHOLD.
        """
        if not record.global_lesson:
            return False
        self.insert(
            Lesson(
                lesson_id=f"gl-{record.record_id}",
                source_mission_id=record.mission_id,
                text=record.text,
                tags=list(record.tags),
                created_at=record.created_at,
            )
        )
        if self.count() > DEDUP_THRESHOLD:
            self.dedup()
        return True

    def dedup(self, *, similarity: float = DEDUP_SIMILARITY) -> int:
        """Collapse near-duplicate lessons by token-set Jaccard overlap.

        Keeps the oldest lesson in each duplicate cluster (it has seniority /
        first-discovery provenance). Returns the number of lessons removed.
        """
        rows = list(self._conn.execute("SELECT * FROM lessons ORDER BY created_at ASC"))
        kept: list[tuple[str, set[str]]] = []
        to_delete: list[str] = []
        for row in rows:
            tokens = _token_set(row["text"]) | set(_decode_tags(row["tags"]))
            if any(_jaccard(tokens, kt) >= similarity for _, kt in kept):
                to_delete.append(row["lesson_id"])
            else:
                kept.append((row["lesson_id"], tokens))
        if to_delete:
            self._conn.executemany(
                "DELETE FROM lessons WHERE lesson_id = ?", [(lid,) for lid in to_delete]
            )
            self._conn.commit()
        return len(to_delete)

    # -- reads ------------------------------------------------------------

    def all_lessons(self) -> list[Lesson]:
        return [_row_to_lesson(r) for r in self._conn.execute("SELECT * FROM lessons")]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0])


def _row_to_lesson(row: sqlite3.Row) -> Lesson:
    return Lesson(
        lesson_id=row["lesson_id"],
        source_mission_id=row["source_mission_id"],
        text=row["text"],
        tags=_decode_tags(row["tags"]),
        created_at=_parse_dt(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# Tokenization helpers (shared with retrieval scoring)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _token_set(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


__all__ = [
    "DEDUP_SIMILARITY",
    "DEDUP_THRESHOLD",
    "GlobalLessons",
    "ProjectMemory",
]
