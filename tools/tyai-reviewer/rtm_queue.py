"""Persistent, session-bound FIFO admission for the single RTM2032 resource.

This module is a trusted server API, not a client authorization boundary. The
server generates an opaque owner key, registers its process/deadline, binds the
actual OpenCode session ID, and supplies that owner from authenticated context
on every call. Never accept owner, session ID, PID, deadline, safe, or recovery
instructions from tool arguments. ``recover`` is a server-only acknowledgement
that hardware is safe; admission alone cannot fence a hardware operation. The
server must call ``require_owner`` immediately before hardware access and fence
operations against concurrent release, close, expiry, and recovery.

Deadlines are absolute clock timestamps, not renewable leases. Session identities
and registration metadata cannot be changed or reused, even after closure.
Requests return immediately after a short SQLite transaction; they never wait
for the resource. Polling ``status`` also performs stale-session maintenance.
"""

from contextlib import contextmanager
import math
from pathlib import Path
import secrets
import sqlite3
import time

from process_options import process_is_running


class ResourceQueue:
    """All successful calls return JSON-serializable dictionaries.

    Snapshots contain ``resource`` (always rtm2032), ``resource_state``
    (AVAILABLE/HELD/QUARANTINED), ``resource_reason``, the caller's ``session_id``,
    ``session_state`` (LIVE/DEAD/EXPIRED/CLOSED), ``deadline``, ``state``, ``ticket``,
    ``position`` (0 acquired, 1-based waiting, otherwise None), and ``reason``.
    Request states are WAITING/ACQUIRED/RELEASED/CANCELLED/EXPIRED/DEAD/CLOSED/
    QUARANTINED; sessions without a request report UNBOUND or IDLE.

    Unknown owners raise KeyError; a foreign/unknown ticket raises
    PermissionError; invalid input, rebinding, or invalid transitions raise
    ValueError. These errors still commit stale-owner maintenance.
    """

    RESOURCE = "rtm2032"

    def __init__(self, path, clock=time.time, alive=process_is_running):
        self.path = Path(path)
        self.clock = clock
        self.alive = alive
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS rtm_sessions (
                    owner TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    deadline REAL NOT NULL,
                    session_id TEXT UNIQUE,
                    state TEXT NOT NULL DEFAULT 'LIVE',
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS rtm_requests (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL REFERENCES rtm_sessions(owner),
                    state TEXT NOT NULL,
                    reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS rtm_one_request_per_owner
                    ON rtm_requests(owner) WHERE state IN ('WAITING', 'ACQUIRED');
                CREATE INDEX IF NOT EXISTS rtm_owner_history
                    ON rtm_requests(owner, sequence);
                CREATE TABLE IF NOT EXISTS rtm_resource (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL,
                    ticket TEXT REFERENCES rtm_requests(ticket),
                    reason TEXT
                );
                INSERT OR IGNORE INTO rtm_resource VALUES (1, 'AVAILABLE', NULL, NULL);
                COMMIT;
            """)
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._sweep(conn)
            self._promote(conn)
            # Failed authorization must not roll back safety maintenance.
            conn.execute("SAVEPOINT operation")
            try:
                yield conn
            except (KeyError, ValueError, PermissionError):
                conn.execute("ROLLBACK TO operation")
                conn.commit()
                raise
            else:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string")
        return value

    def _session(self, conn, owner):
        self._text(owner, "owner")
        row = conn.execute("SELECT * FROM rtm_sessions WHERE owner = ?", (owner,)).fetchone()
        if row is None:
            raise KeyError("unknown owner")
        return row

    def _live_session(self, conn, owner, bound=True):
        session = self._session(conn, owner)
        if session["state"] != "LIVE":
            raise ValueError(f"session is {session['state']}")
        if bound and session["session_id"] is None:
            raise ValueError("session must be bound before requesting the resource")
        return session

    def _sweep(self, conn):
        now = self.clock()
        for session in conn.execute("SELECT * FROM rtm_sessions WHERE state = 'LIVE'").fetchall():
            if session["deadline"] <= now:
                state, reason = "EXPIRED", "owner deadline expired"
            elif not self.alive(session["pid"]):
                state, reason = "DEAD", "owner process exited"
            else:
                continue
            conn.execute("UPDATE rtm_sessions SET state = ?, reason = ? WHERE owner = ?",
                         (state, reason, session["owner"]))
            conn.execute("UPDATE rtm_requests SET state = ?, reason = ? "
                         "WHERE owner = ? AND state = 'WAITING'", (state, reason, session["owner"]))
            active = conn.execute("SELECT ticket FROM rtm_requests WHERE owner = ? "
                                  "AND state = 'ACQUIRED'", (session["owner"],)).fetchone()
            if active:
                self._mark_quarantine(conn, active["ticket"], reason)

    @staticmethod
    def _mark_quarantine(conn, ticket, reason):
        conn.execute("UPDATE rtm_requests SET state = 'QUARANTINED', reason = ? WHERE ticket = ?",
                     (reason, ticket))
        conn.execute("UPDATE rtm_resource SET state = 'QUARANTINED', reason = ? WHERE id = 1",
                     (reason,))

    @staticmethod
    def _promote(conn):
        resource = conn.execute("SELECT state FROM rtm_resource WHERE id = 1").fetchone()
        if resource["state"] != "AVAILABLE":
            return
        first = conn.execute("SELECT r.ticket FROM rtm_requests r JOIN rtm_sessions s "
                             "ON s.owner = r.owner WHERE r.state = 'WAITING' "
                             "AND s.state = 'LIVE' AND s.session_id IS NOT NULL "
                             "ORDER BY r.sequence LIMIT 1").fetchone()
        if first:
            conn.execute("UPDATE rtm_requests SET state = 'ACQUIRED' WHERE ticket = ?",
                         (first["ticket"],))
            conn.execute("UPDATE rtm_resource SET state = 'HELD', ticket = ?, reason = NULL "
                         "WHERE id = 1", (first["ticket"],))

    def _snapshot(self, conn, owner):
        session = self._session(conn, owner)
        request = conn.execute("SELECT * FROM rtm_requests WHERE owner = ? "
                               "ORDER BY sequence DESC LIMIT 1", (owner,)).fetchone()
        resource = conn.execute("SELECT * FROM rtm_resource WHERE id = 1").fetchone()
        position = None
        if request:
            state, ticket, reason = request["state"], request["ticket"], request["reason"]
            if state == "ACQUIRED":
                position = 0
            elif state == "WAITING":
                position = conn.execute("SELECT COUNT(*) FROM rtm_requests WHERE state = 'WAITING' "
                                        "AND sequence <= ?", (request["sequence"],)).fetchone()[0]
        else:
            state = session["state"] if session["state"] != "LIVE" else (
                "IDLE" if session["session_id"] else "UNBOUND")
            ticket, reason = None, session["reason"]
        return {"resource": self.RESOURCE, "resource_state": resource["state"],
                "resource_reason": resource["reason"], "session_id": session["session_id"],
                "session_state": session["state"], "deadline": session["deadline"],
                "state": state, "ticket": ticket, "position": position, "reason": reason}

    def register(self, owner, pid, deadline):
        """Register an immutable server-generated owner; identical repeats are harmless."""
        self._text(owner, "owner")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive integer")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            raise ValueError("deadline must be a finite absolute timestamp")
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM rtm_sessions WHERE owner = ?", (owner,)).fetchone()
            if existing:
                if existing["pid"] != pid or existing["deadline"] != deadline:
                    raise ValueError("owner registration is immutable")
            else:
                conn.execute("INSERT INTO rtm_sessions(owner, pid, deadline) VALUES (?, ?, ?)",
                             (owner, pid, deadline))
                self._sweep(conn)
            return self._snapshot(conn, owner)

    def bind(self, owner, session_id):
        """Bind the actual OpenCode session once; the same binding is idempotent."""
        self._text(session_id, "session_id")
        with self._transaction() as conn:
            session = self._live_session(conn, owner, bound=False)
            if session["session_id"] is not None and session["session_id"] != session_id:
                raise ValueError("session binding is immutable")
            other = conn.execute("SELECT owner FROM rtm_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if other and other["owner"] != owner:
                raise ValueError("session is already bound to another owner")
            conn.execute("UPDATE rtm_sessions SET session_id = ? WHERE owner = ?", (session_id, owner))
            return self._snapshot(conn, owner)

    def request(self, owner):
        """Enqueue once per owner and claim only the oldest valid waiting request."""
        with self._transaction() as conn:
            self._live_session(conn, owner)
            existing = conn.execute("SELECT ticket FROM rtm_requests WHERE owner = ? "
                                    "AND state IN ('WAITING', 'ACQUIRED', 'QUARANTINED')", (owner,)).fetchone()
            if existing is None:
                conn.execute("INSERT INTO rtm_requests(ticket, owner, state) VALUES (?, ?, 'WAITING')",
                             (secrets.token_urlsafe(32), owner))
            self._promote(conn)
            return self._snapshot(conn, owner)

    def status(self, owner):
        """Poll one's own request, cleaning up stale owners and advancing FIFO."""
        with self._transaction() as conn:
            return self._snapshot(conn, owner)

    def _ticket(self, conn, owner, ticket):
        self._session(conn, owner)
        if not isinstance(ticket, str) or not ticket:
            raise PermissionError("ticket does not belong to owner")
        row = conn.execute("SELECT * FROM rtm_requests WHERE ticket = ? AND owner = ?",
                           (ticket, owner)).fetchone()
        if row is None:
            raise PermissionError("ticket does not belong to owner")
        return row

    def _require_owner(self, conn, owner, ticket):
        request = self._ticket(conn, owner, ticket)
        self._live_session(conn, owner)
        resource = conn.execute("SELECT * FROM rtm_resource WHERE id = 1").fetchone()
        if request["state"] != "ACQUIRED" or resource["state"] != "HELD" or resource["ticket"] != ticket:
            raise ValueError("ticket does not hold the resource")

    def require_owner(self, owner, ticket):
        """Validate live, bound ownership before a separately fenced hardware operation."""
        with self._transaction() as conn:
            self._require_owner(conn, owner, ticket)
            return self._snapshot(conn, owner)

    def release(self, owner, ticket):
        """Acknowledge safe completion and hand off FIFO; released repeats are harmless."""
        with self._transaction() as conn:
            request = self._ticket(conn, owner, ticket)
            if request["state"] != "RELEASED":
                self._require_owner(conn, owner, ticket)
                conn.execute("UPDATE rtm_requests SET state = 'RELEASED' WHERE ticket = ?", (ticket,))
                conn.execute("UPDATE rtm_resource SET state = 'AVAILABLE', ticket = NULL, reason = NULL WHERE id = 1")
                self._promote(conn)
            return self._snapshot(conn, owner)

    def cancel(self, owner):
        """Cancel a waiting request; active ownership must be released or closed."""
        with self._transaction() as conn:
            self._session(conn, owner)
            active = conn.execute("SELECT state FROM rtm_requests WHERE owner = ? "
                                  "AND state IN ('ACQUIRED', 'QUARANTINED')", (owner,)).fetchone()
            if active:
                raise ValueError("active ownership cannot be cancelled; release or close the session")
            conn.execute("UPDATE rtm_requests SET state = 'CANCELLED' WHERE owner = ? AND state = 'WAITING'", (owner,))
            self._promote(conn)
            return self._snapshot(conn, owner)

    def close_session(self, owner, safe=True):
        """Close permanently; safe=True asserts hardware completion for a live holder.

        An existing quarantine is never cleared by this call, even with safe=True.
        """
        if not isinstance(safe, bool):
            raise ValueError("safe must be a boolean")
        with self._transaction() as conn:
            self._session(conn, owner)
            active = conn.execute("SELECT ticket FROM rtm_requests WHERE owner = ? AND state = 'ACQUIRED'",
                                  (owner,)).fetchone()
            if active:
                if safe:
                    conn.execute("UPDATE rtm_requests SET state = 'RELEASED' WHERE ticket = ?", (active["ticket"],))
                    conn.execute("UPDATE rtm_resource SET state = 'AVAILABLE', ticket = NULL, reason = NULL WHERE id = 1")
                else:
                    self._mark_quarantine(conn, active["ticket"], "session closed without safe completion")
            conn.execute("UPDATE rtm_requests SET state = 'CLOSED', reason = 'session closed' "
                         "WHERE owner = ? AND state = 'WAITING'", (owner,))
            conn.execute("UPDATE rtm_sessions SET state = 'CLOSED', reason = 'session closed' WHERE owner = ?", (owner,))
            self._promote(conn)
            return self._snapshot(conn, owner)

    def quarantine(self, owner, reason):
        """Server reports uncertainty about this owner's active hardware operation."""
        self._text(reason, "reason")
        with self._transaction() as conn:
            self._session(conn, owner)
            resource = conn.execute("SELECT * FROM rtm_resource WHERE id = 1").fetchone()
            if resource["ticket"] is None:
                raise ValueError("owner does not hold the resource")
            self._ticket(conn, owner, resource["ticket"])
            self._mark_quarantine(conn, resource["ticket"], reason)
            return self._snapshot(conn, owner)

    def recover(self, reason):
        """Server-only: assert hardware is safe, invalidate the old ticket, resume FIFO."""
        self._text(reason, "reason")
        with self._transaction() as conn:
            resource = conn.execute("SELECT * FROM rtm_resource WHERE id = 1").fetchone()
            if resource["state"] != "QUARANTINED":
                raise ValueError("resource is not quarantined")
            conn.execute("UPDATE rtm_requests SET state = 'CANCELLED', reason = ? WHERE ticket = ?",
                         (f"recovered: {reason}", resource["ticket"]))
            conn.execute("UPDATE rtm_resource SET state = 'AVAILABLE', ticket = NULL, reason = NULL WHERE id = 1")
            self._promote(conn)
            state = conn.execute("SELECT state FROM rtm_resource WHERE id = 1").fetchone()[0]
            return {"resource": self.RESOURCE, "resource_state": state, "reason": reason}
