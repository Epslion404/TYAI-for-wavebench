from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from threading import Barrier

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import queue_store
from queue_store import QueueStore


def event(key="pr:1", fingerprint="sha-1", **fields):
    return {"key": key, "fingerprint": fingerprint, "kind": "pr", "sha": "abc", **fields}


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "nested" / "reviews.sqlite3")


def test_schema_initialization_and_durable_baseline(store):
    assert store.path.exists()
    assert store.get_snapshot() is None
    store.initialize_schema()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    snapshot = {"entries": {"pr:1": {"title": "中文 baseline"}}}
    store.ingest(snapshot, [])
    reopened = QueueStore(store.path)
    assert reopened.get_snapshot() == snapshot
    assert reopened.claim_next() is None
    assert sum(reopened.status()["counts"].values()) == 0


def test_ingest_atomic_baseline_jobs_and_deduplication(store):
    old, new = {"entries": {}}, {"entries": {"pr:1": "updated"}}
    store.ingest(old, [])
    first, second = event(), event("issue:2")
    with pytest.raises(ValueError, match="fingerprint"):
        store.ingest(new, [first, {"key": "missing-fingerprint"}])
    assert store.get_snapshot() == old
    assert store.claim_next() is None
    # Serialization failure after the first insert must roll it back too.
    with pytest.raises(TypeError):
        store.ingest(new, [first, event("invalid", body=object())])
    assert store.get_snapshot() == old
    assert store.claim_next() is None
    store.ingest(new, [first, second, first])
    store.ingest(new, [first, second])
    assert store.get_snapshot() == new
    assert store.status()["counts"]["pending"] == 2
    assert {store.claim_next()["event"]["key"] for _ in range(2)} == {"pr:1", "issue:2"}
    assert store.claim_next() is None


def test_identity_payload_and_no_overwrite(store):
    original = event(body="原始正文", priority="P1")
    job_id = store.enqueue(original)
    expected = hashlib.sha256(json.dumps(["pr:1", "sha-1"], separators=(",", ":")).encode()).hexdigest()
    assert job_id == expected
    assert QueueStore(store.path).enqueue(event(body="changed", priority="P0")) == job_id
    assert store.get_job(job_id)["event"] == original
    assert store.enqueue(event(fingerprint="sha-2")) != job_id
    assert store.enqueue(event("ab", "c")) != store.enqueue(event("a", "bc"))
    assert store.get_job("missing") is None


def test_concurrent_enqueue_and_claim_once_per_event(store):
    barrier = Barrier(8)

    def enqueue(_):
        barrier.wait(timeout=10)
        return store.enqueue(event())

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(enqueue, range(8)))
    assert len(set(ids)) == 1
    barrier = Barrier(8)

    def claim(_):
        barrier.wait(timeout=10)
        claimed = []
        while (job := store.claim_next()) is not None:
            claimed.append(job)
        return claimed

    for index in range(23):
        store.enqueue(event(f"issue:{index + 100}"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [job for group in pool.map(claim, range(8)) for job in group]
    assert len(claimed) == 24
    assert len({job["id"] for job in claimed}) == 24
    assert all(job["attempts"] == 1 and job["status"] == "running" for job in claimed)
    assert store.status()["counts"]["running"] == 24


def test_priority_fifo_and_due_time(store, monkeypatch):
    monkeypatch.setattr(queue_store.time, "time", lambda: 1000.0)
    default = store.enqueue(event("default"))
    p2 = store.enqueue(event("p2", priority="P2"))
    p1 = store.enqueue(event("p1", priority="P1"))
    p0 = store.enqueue(event("p0", priority="P0"))
    unknown = store.enqueue(event("unknown", priority="invalid"))
    assert store.claim_next(now=999) is None
    assert [store.claim_next(now=1000)["id"] for _ in range(5)] == [p0, p1, default, p2, unknown]
    assert store.claim_next(now=1000) is None


def test_bounded_backoff_and_final_failure(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(queue_store.time, "time", lambda: clock[0])
    job_id = store.enqueue(event(priority="P0"))
    for attempt in range(1, 5):
        job = store.claim_next()
        assert job["attempts"] == attempt
        store.fail(job_id, f"attempt {attempt} failed")
        failed = store.get_job(job_id)
        assert failed["error"] == f"attempt {attempt} failed"
        if attempt < 4:
            assert failed["status"] == "retry"
            assert 120 <= failed["next_at"] - clock[0] <= 900
            assert store.claim_next(now=failed["next_at"] - 0.01) is None
            # A delayed P0 retry must not block another due event.
            other = store.enqueue(event(f"other:{attempt}", priority="P2"))
            assert store.claim_next()["id"] == other
            store.finish(other, {"status": "complete"})
            clock[0] = failed["next_at"]
        else:
            assert failed["status"] == "failed"
            assert store.claim_next(now=clock[0] + 10000) is None


def test_failure_without_retry_is_never_claimed(store):
    job_id = store.enqueue(event())
    store.claim_next()
    store.fail(job_id, "Worker may have written to GitHub", retry=False)
    assert store.get_job(job_id)["status"] == "failed"
    assert store.get_job(job_id)["attempts"] == 1
    assert store.claim_next(now=10**12) is None


def test_worker_interruption_preserves_session_and_never_replays(store):
    job_id = store.enqueue(event())
    store.claim_next()
    store.set_session(job_id, "session-123")
    # A new store models process restart, without requiring any GitHub access.
    restarted = QueueStore(store.path)
    assert restarted.recover_interrupted() == 1
    assert restarted.recover_interrupted() == 0
    job = restarted.get_job(job_id)
    assert job["status"] == "interrupted"
    assert job["session_id"] == "session-123"
    assert job["attempts"] == 1
    assert restarted.claim_next(now=10**12) is None
    assert restarted.enqueue(event()) == job_id
    assert restarted.claim_next(now=10**12) is None
    with pytest.raises(ValueError, match="not running"):
        restarted.fail(job_id, "stale worker")


def test_finish_persists_result_and_session_and_clears_error(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(queue_store.time, "time", lambda: clock[0])
    job_id = store.enqueue(event())
    job = store.claim_next()
    assert job["id"] == job_id and job["session_id"] is None
    store.fail(job_id, "temporary")
    clock[0] = store.get_job(job_id)["next_at"]
    store.claim_next()
    store.set_session(job_id, "session-complete")
    result = {"status": "complete", "summary": "完成", "urls": ["evidence"], "tests": []}
    store.finish(job_id, result)
    reopened = QueueStore(store.path)
    job = reopened.get_job(job_id)
    assert job["result"] == result
    assert job["session_id"] == "session-complete"
    assert job["status"] == "done" and job["attempts"] == 2 and job["error"] is None
    assert reopened.claim_next(now=10**12) is None
    assert reopened.enqueue(event()) == job_id
    assert reopened.get_job(job_id)["status"] == "done"


def test_status_limits_recent_jobs_and_omits_sensitive_payload(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(queue_store.time, "time", lambda: clock[0])
    for index in range(25):
        clock[0] += 1
        job_id = store.enqueue(event(str(index), body="sensitive-body", title="sensitive-title"))
        store.claim_next()
        if index == 0:
            store.fail(job_id, "sensitive-error", retry=False)
        else:
            store.finish(job_id, {"summary": "sensitive-result"})
    report = store.status()
    assert report["counts"] == {"pending": 0, "running": 0, "retry": 0,
                                "done": 24, "failed": 1, "interrupted": 0}
    assert len(report["recent_jobs"]) == 20
    assert [job["event"]["key"] for job in report["recent_jobs"]] == [str(i) for i in range(24, 4, -1)]
    assert "sensitive" not in json.dumps(report)
    assert set(report["recent_jobs"][0]["event"]) == {"key", "kind", "fingerprint", "sha"}


def test_connections_close_even_after_transaction_failure(store, monkeypatch):
    connections = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(queue_store.sqlite3, "connect", tracked_connect)
    store.initialize_schema()
    store.get_snapshot()
    with pytest.raises(ValueError):
        store.ingest({}, [{"key": "bad"}])
    job_id = store.enqueue(event())
    store.claim_next()
    store.set_session(job_id, "session")
    store.finish(job_id, {})
    store.get_job(job_id)
    store.status()
    store.recover_interrupted()
    with pytest.raises(KeyError):
        store.fail("missing", "error")
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_eight_parallel_claims_enforce_global_capacity(store):
    for number in range(24):
        store.enqueue(event(f"issue:{number + 100}", number=number + 100))
    barrier = Barrier(16)
    def claim(_):
        barrier.wait(timeout=10)
        return store.claim_next(max_running=8)
    with ThreadPoolExecutor(max_workers=16) as pool:
        jobs = [job for job in pool.map(claim, range(16)) if job]
    assert len(jobs) == 8
    assert len({job["id"] for job in jobs}) == 8
    assert store.status()["counts"]["running"] == 8
    assert store.claim_next(max_running=8) is None
    store.finish(jobs[0]["id"], {"status": "complete"})
    assert store.claim_next(max_running=8) is not None


def test_same_issue_comments_and_recovery_are_serialized(store):
    first = store.enqueue(event("issue:43", number=43, kind="issue"))
    comment = store.enqueue(event("issue-comment:987", number=43, kind="issue-comment", priority="P0"))
    store.enqueue(event("recovery:issue43", number=43, kind="issue", fingerprint="recovered"))
    other = store.enqueue(event("issue:44", number=44, kind="issue"))
    claimed = store.claim_next(max_running=8)
    assert claimed["id"] == comment
    assert store.claim_next(max_running=8)["id"] == other
    assert store.claim_next(max_running=8) is None
    store.finish(comment, {"status": "complete"})
    assert store.claim_next(max_running=8)["id"] == first


def test_primary_restart_preserves_live_assistant_jobs(store):
    job_id = store.enqueue(event("issue:100", number=100))
    job = store.claim_next(max_running=8)
    assert store.recover_interrupted(preserve_pids=(job["worker_pid"],)) == 0
    assert store.get_job(job_id)["status"] == "running"
    assert store.recover_interrupted(preserve_pids=(-1,)) == 1
