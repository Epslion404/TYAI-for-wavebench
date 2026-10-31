"""Durable, one-event-per-job queue; the runner owns repository validation.

Connections are local to each operation. An external runner lock is still
required: SQLite claiming does not serialize a runner's external side effects.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class QueueStore:
    MAX_RETRIES = 3
    MAX_ATTEMPTS = 1 + MAX_RETRIES
    BUSY_TIMEOUT_MS = 30_000
    STATUSES = ("pending", "running", "retry", "done", "failed", "interrupted")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize_schema()

    @contextmanager
    def _connection(self, *, write: bool = False, initialize: bool = False):
        conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
            if initialize:
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize_schema(self) -> None:
        with self._connection(write=True, initialize=True) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    event_json TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    session_id TEXT,
                    result_json TEXT,
                    error TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS jobs_claim
                ON jobs (status, next_at, priority, created_at)
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "worker_pid" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN worker_pid INTEGER")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL
                )
            """)

    @staticmethod
    def _json(value: dict) -> str:
        if not isinstance(value, dict):
            raise TypeError("Expected a dictionary")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def _enqueue(cls, conn: sqlite3.Connection, event: dict, now: float) -> str:
        payload = cls._json(event)
        key, fingerprint = event.get("key"), event.get("fingerprint")
        if not isinstance(key, str) or not key or not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("Event requires nonempty string key and fingerprint")
        # Encode the pair unambiguously: ('ab', 'c') differs from ('a', 'bc').
        identity = json.dumps([key, fingerprint], ensure_ascii=False, separators=(",", ":"))
        job_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        priority = event.get("priority")
        if priority not in ("P0", "P1", "P2"):
            priority = "P2"
        conn.execute("""
            INSERT INTO jobs
                (id, event_json, priority, status, attempts, next_at, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
        """, (job_id, payload, priority, now, now, now))
        return job_id

    def ingest(self, snapshot: dict, events: list[dict]) -> None:
        """Commit the new baseline and all its event jobs together, or neither."""
        state = self._json(snapshot)
        now = time.time()
        with self._connection(write=True) as conn:
            conn.execute("""
                INSERT INTO snapshot (id, state) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET state = excluded.state
            """, (state,))
            for event in events:
                self._enqueue(conn, event, now)

    def get_snapshot(self) -> dict | None:
        with self._connection() as conn:
            row = conn.execute("SELECT state FROM snapshot WHERE id = 1").fetchone()
            return json.loads(row["state"]) if row else None

    def enqueue(self, event: dict) -> str:
        with self._connection(write=True) as conn:
            return self._enqueue(conn, event, time.time())

    @staticmethod
    def _job(row: sqlite3.Row) -> dict:
        job = dict(row)
        job["event"] = json.loads(job.pop("event_json"))
        result = job.pop("result_json")
        job["result"] = json.loads(result) if result is not None else None
        return job

    def get_job(self, job_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._job(row) if row else None

    @staticmethod
    def target_key(event: dict) -> str:
        number = event.get("number")
        if not number and event.get("kind") in ("issue", "pr"):
            suffix = event.get("key", "").partition(":")[2]
            number = int(suffix) if suffix.isdecimal() else None
        # Issue comments and PR reviews share the repository's issue numbering.
        return f"item:{number}" if number else event.get("key", "")

    def claim_next(self, now: float | None = None, *, max_running: int | None = None) -> dict | None:
        now = time.time() if now is None else now
        with self._connection(write=True) as conn:
            running = conn.execute("SELECT event_json FROM jobs WHERE status = 'running'").fetchall()
            if max_running is not None and len(running) >= max_running:
                return None
            targets = {self.target_key(json.loads(row["event_json"])) for row in running}
            candidates = conn.execute("""
                SELECT id, event_json FROM jobs
                WHERE status IN ('pending', 'retry') AND next_at <= ?
                ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
                         created_at, rowid
            """, (now,)).fetchall()
            row = next((row for row in candidates
                        if self.target_key(json.loads(row["event_json"])) not in targets), None)
            if row is None:
                return None
            conn.execute("""
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                                 updated_at = ?, worker_pid = ?
                WHERE id = ?
            """, (now, os.getpid(), row["id"]))
            return self._job(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())

    @staticmethod
    def _require_running(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        if row["status"] != "running":
            raise ValueError(f"Job {job_id} is not running")
        return row

    def set_session(self, job_id: str, session_id: str) -> None:
        with self._connection(write=True) as conn:
            self._require_running(conn, job_id)
            conn.execute("UPDATE jobs SET session_id = ?, updated_at = ? WHERE id = ?",
                         (session_id, time.time(), job_id))

    def finish(self, job_id: str, result: dict) -> None:
        payload = self._json(result)
        with self._connection(write=True) as conn:
            self._require_running(conn, job_id)
            conn.execute("""
                UPDATE jobs SET status = 'done', result_json = ?, error = NULL,
                                updated_at = ? WHERE id = ?
            """, (payload, time.time(), job_id))

    def fail(self, job_id: str, error: str, retry: bool = True) -> None:
        with self._connection(write=True) as conn:
            row = self._require_running(conn, job_id)
            now = time.time()
            will_retry = retry and row["attempts"] < self.MAX_ATTEMPTS
            delay = min(900, 120 * 2 ** (row["attempts"] - 1)) if will_retry else 0
            conn.execute("""
                UPDATE jobs SET status = ?, error = ?, next_at = ?, updated_at = ?
                WHERE id = ?
            """, ("retry" if will_retry else "failed", error, now + delay, now, job_id))

    def recover_interrupted(self, *, preserve_pids: tuple[int, ...] = ()) -> int:
        """Quarantine jobs whose worker stopped; never replay possible external writes."""
        with self._connection(write=True) as conn:
            preserved = ",".join("?" for _ in preserve_pids) or "NULL"
            return conn.execute("""
                UPDATE jobs SET status = 'interrupted', updated_at = ?
                WHERE status = 'running'
            """ + (f" AND (worker_pid IS NULL OR worker_pid NOT IN ({preserved}))"
                    if preserve_pids else ""), (time.time(), *preserve_pids)).rowcount

    def status(self) -> dict:
        """Return counts and 20 recently updated jobs, omitting bodies, results and errors."""
        with self._connection() as conn:
            counts = dict.fromkeys(self.STATUSES, 0)
            counts.update({row["status"]: row["count"] for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")})
            recent = []
            for row in conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC, rowid DESC LIMIT 20"):
                job = dict(row)
                event = json.loads(job.pop("event_json"))
                job.pop("result_json")
                job.pop("error")
                job["event"] = {key: event[key] for key in ("key", "kind", "fingerprint", "sha")
                                if key in event}
                recent.append(job)
            return {"counts": counts, "recent_jobs": recent}
