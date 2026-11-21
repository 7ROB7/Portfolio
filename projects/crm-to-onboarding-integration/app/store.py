"""The integration's own small database (SQLite).

* events     - every webhook received: lets us de-duplicate (idempotency), audit, and
               keep a dead-letter list of rejected/failed events to fix and replay.
* deal_links - CRM deal ID -> Odoo record IDs (the cross-system ID map every integration needs).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    deal_id      TEXT,
    status       TEXT NOT NULL,        -- processing | success | failed | rejected
    attempts     INTEGER NOT NULL DEFAULT 1,
    received_at  TEXT NOT NULL,
    finished_at  TEXT,
    error        TEXT,
    result       TEXT,
    payload      TEXT
);
CREATE TABLE IF NOT EXISTS deal_links (
    deal_id      TEXT PRIMARY KEY,
    company_id   INTEGER,
    contact_id   INTEGER,
    project_id   INTEGER,
    task_count   INTEGER,
    updated_at   TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def begin_event(self, event_id: str, deal_id: str, payload: str) -> str:
        """Returns 'new', 'retry' (previous attempt failed), 'duplicate' (already succeeded)
        or 'in_progress' (same event is being processed right now)."""
        with self._lock:
            row = self._db.execute("SELECT status FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO events (event_id, deal_id, status, received_at, payload) VALUES (?,?,?,?,?)",
                    (event_id, deal_id, "processing", _now(), payload))
                self._db.commit()
                return "new"
            if row["status"] == "success":
                return "duplicate"
            if row["status"] == "processing":
                return "in_progress"
            self._db.execute(
                "UPDATE events SET status='processing', attempts=attempts+1, error=NULL, payload=? WHERE event_id=?",
                (payload, event_id))
            self._db.commit()
            return "retry"

    def finish_event(self, event_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE events SET status=?, finished_at=?, result=?, error=? WHERE event_id=?",
                (status, _now(), json.dumps(result, default=str) if result else None, error, event_id))
            self._db.commit()

    def record_rejected(self, event_id: str, deal_id: str | None, payload: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO events (event_id, deal_id, status, received_at, finished_at, error, payload)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET status='rejected', attempts=attempts+1,
                       finished_at=excluded.finished_at, error=excluded.error, payload=excluded.payload""",
                (event_id, deal_id, "rejected", _now(), _now(), error, payload))
            self._db.commit()

    def upsert_link(self, deal_id: str, company_id: int, contact_id: int, project_id: int, task_count: int) -> None:
        with self._lock:
            self._db.execute(
                """INSERT INTO deal_links VALUES (?,?,?,?,?,?)
                   ON CONFLICT(deal_id) DO UPDATE SET company_id=excluded.company_id, contact_id=excluded.contact_id,
                       project_id=excluded.project_id, task_count=excluded.task_count, updated_at=excluded.updated_at""",
                (deal_id, company_id, contact_id, project_id, task_count, _now()))
            self._db.commit()

    def list_events(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT event_id, deal_id, status, attempts, received_at, finished_at, error, result "
            "FROM events ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["result"] = json.loads(d["result"]) if d["result"] else None
            out.append(d)
        return out
