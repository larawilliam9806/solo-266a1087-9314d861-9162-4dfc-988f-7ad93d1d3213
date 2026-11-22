"""SQLite helpers and schema for the item-analysis app."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- One row per (form, printed question number, printed option letter).
-- std_code / std_option are the canonical question code and option.
CREATE TABLE IF NOT EXISTS form_mappings (
    form_id    INTEGER NOT NULL REFERENCES forms(id) ON DELETE CASCADE,
    q_no       TEXT NOT NULL,
    letter     TEXT NOT NULL,
    std_code   TEXT NOT NULL,
    std_option TEXT NOT NULL,
    PRIMARY KEY (form_id, q_no, letter)
);
CREATE INDEX IF NOT EXISTS idx_form_mappings_code
    ON form_mappings(form_id, std_code);

CREATE TABLE IF NOT EXISTS answer_rows (
    card_no      TEXT NOT NULL,
    q_no         TEXT NOT NULL,
    letter       TEXT NOT NULL DEFAULT '',
    scanned_code TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (card_no, q_no)
);
CREATE INDEX IF NOT EXISTS idx_answer_rows_card ON answer_rows(card_no);

CREATE TABLE IF NOT EXISTS review_decisions (
    card_no    TEXT NOT NULL,
    std_code   TEXT NOT NULL,
    resolution TEXT NOT NULL,          -- opt:<value> | blank | skip
    note       TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (card_no, std_code)
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS run_question_stats (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    std_code   TEXT NOT NULL,
    counted    INTEGER NOT NULL DEFAULT 0,
    blank      INTEGER NOT NULL DEFAULT 0,
    flagged    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, std_code)
);

CREATE TABLE IF NOT EXISTS run_option_counts (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    std_code   TEXT NOT NULL,
    std_option TEXT NOT NULL,
    cnt        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, std_code, std_option)
);

CREATE TABLE IF NOT EXISTS run_card_results (
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    card_no    TEXT NOT NULL,
    std_code   TEXT NOT NULL,
    status     TEXT NOT NULL,          -- counted | blank | flagged | skipped
    std_option TEXT,
    detail     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, card_no, std_code)
);
CREATE INDEX IF NOT EXISTS idx_run_card_results_status
    ON run_card_results(run_id, status);

CREATE TABLE IF NOT EXISTS run_unmatched (
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    card_no TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, card_no)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,          -- queued | running | succeeded | failed
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    run_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('active_run_id', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('state_version', '0')"
        )
        # Jobs interrupted by a restart are safe to replay: the payload is
        # persisted and each job's transaction either committed or rolled back.
        conn.execute(
            "UPDATE jobs SET status = 'queued', started_at = NULL "
            "WHERE status = 'running'"
        )
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "0") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def active_run_id(conn: sqlite3.Connection) -> int:
    try:
        return int(get_meta(conn, "active_run_id", "0"))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------
# Storage maintenance
# --------------------------------------------------------------------------
# Raw CSV text used to be embedded verbatim in ``jobs.payload``.  Payloads of
# finished jobs (which can never be replayed) are therefore stripped in small
# batches at startup: every batch is its own transaction, so a crash or a
# "database or disk is full" error simply leaves the remaining rows for the
# next startup.  The same goes for orphaned staging files on disk.

PAYLOAD_BATCH_SIZE = 25


def reclaim_storage(conn: sqlite3.Connection, staging_dir: str) -> None:
    """Best-effort, restart-safe reclamation of CSV staging space.

    Steps, each individually safe to interrupt and re-run:
      1. jobs interrupted mid-run go back to the queue (their staging files are
         retained so the same CSV can be processed after a restart);
      2. orphaned staging files not referenced by any queued/running job are
         removed (also reclaims space left by jobs finished in older versions);
      3. inline CSV text in finished-job payloads is stripped in batches;
      4. the WAL is checkpointed and the database is vacuumed so freed pages
         are returned to the data volume.

    VACUUM needs roughly one database-file worth of temporary space.  When the
    volume is nearly full it may fail; the stripping itself has already been
    committed, so the next startup simply retries the shrink.
    """
    conn.execute(
        "UPDATE jobs SET status = 'queued', started_at = NULL "
        "WHERE status = 'running'"
    )
    sweep_staging_files(conn, staging_dir)
    stripped_any = strip_finished_payloads(conn)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass
    if stripped_any:
        try:
            conn.execute("VACUUM")
        except sqlite3.DatabaseError:
            # Disk too full to rebuild the file; payloads are already slim,
            # the shrink is retried on the next startup.
            pass


def sweep_staging_files(conn: sqlite3.Connection, staging_dir: str) -> None:
    """Delete staging files that no queued/running job still needs."""
    if not staging_dir or not os.path.isdir(staging_dir):
        return
    kept: set[str] = set()
    for row in conn.execute(
        "SELECT payload FROM jobs WHERE status IN ('queued', 'running')"
    ):
        try:
            payload = json.loads(row["payload"])
        except (ValueError, TypeError):
            continue
        name = payload.get("csv_file")
        if isinstance(name, str) and name:
            kept.add(os.path.basename(name))
    try:
        entries = os.listdir(staging_dir)
    except OSError:
        return
    for name in entries:
        if name in kept:
            continue
        path = os.path.join(staging_dir, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            # A locked/permissioned file is retried on the next startup.
            pass


def strip_finished_payloads(conn: sqlite3.Connection) -> bool:
    """Remove raw CSV references from finished job payloads in small batches.

    Returns True when at least one payload was rewritten, so the caller can
    decide whether a VACUUM is worthwhile.
    """
    stripped_any = False
    while True:
        rows = conn.execute(
            "SELECT id, payload FROM jobs "
            "WHERE status IN ('succeeded', 'failed') "
            "AND (payload LIKE '%\"csv\"%' OR payload LIKE '%\"csv_file\"%') "
            "LIMIT ?",
            (PAYLOAD_BATCH_SIZE,),
        ).fetchall()
        if not rows:
            return stripped_any
        updates: list[tuple[str, int]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
            payload.pop("csv", None)
            payload.pop("csv_file", None)
            updates.append((json.dumps(payload, ensure_ascii=False), row["id"]))
        if not updates:
            # Nothing parseable left in this batch; do not spin on it again.
            return stripped_any
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "UPDATE jobs SET payload = ? WHERE id = ?", updates
            )
            conn.commit()
            stripped_any = True
        except Exception:
            conn.rollback()
            # Disk full mid-batch: leave everything for the next startup
            # rather than looping on the same un-processable batch.
            return stripped_any
