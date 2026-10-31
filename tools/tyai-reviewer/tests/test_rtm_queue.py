from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import time
from threading import Barrier

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rtm_queue import ResourceQueue


@pytest.fixture
def queue(tmp_path):
    now = [1000.0]
    pids = {1, 2, 3, 4, 5, 6}
    instance = ResourceQueue(tmp_path / "nested" / "rtm.sqlite3",
                             clock=lambda: now[0], alive=lambda pid: pid in pids)
    instance.now = now
    instance.pids = pids
    return instance


def session(queue, owner, pid=1, deadline=2000.0):
    queue.register(owner, pid, deadline)
    return queue.bind(owner, f"opencode-{owner}")


def concurrent_request(path, owner, gate):
    queue = ResourceQueue(path)
    if not gate.wait(20):
        raise RuntimeError("request barrier timed out")
    return owner, queue.request(owner)


def acquire_in_child(path):
    queue = ResourceQueue(path)
    session(queue, "child", os.getpid(), time.time() + 120)
    return queue.request("child")


def test_four_concurrent_process_requests_fifo_and_durable_handoff(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = ResourceQueue(path)
    deadline = time.time() + 120
    session(queue, "holder", os.getpid(), deadline)
    held = queue.request("holder")
    for owner in ("a", "b", "c", "d"):
        session(queue, owner, os.getpid(), deadline)

    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        gate = manager.Event()
        with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
            futures = [pool.submit(concurrent_request, str(path), owner, gate)
                       for owner in ("a", "b", "c", "d")]
            gate.set()
            results = dict(future.result(timeout=30) for future in futures)

    assert {result["position"] for result in results.values()} == {1, 2, 3, 4}
    assert all(result["state"] == "WAITING" for result in results.values())
    assert len({result["ticket"] for result in results.values()}) == 4
    order = sorted(results, key=lambda owner: results[owner]["position"])
    # Reopen each time: lock state and FIFO must live in SQLite, not Python.
    queue.release("holder", held["ticket"])
    for index, owner in enumerate(order):
        queue = ResourceQueue(path)
        result = queue.status(owner)
        assert result["state"] == "ACQUIRED" and result["position"] == 0
        assert result["ticket"] == results[owner]["ticket"]
        for later in order[index + 1:]:
            assert queue.status(later)["state"] == "WAITING"
        queue.require_owner(owner, result["ticket"])
        queue.release(owner, result["ticket"])
    assert queue.status("holder")["resource_state"] == "AVAILABLE"


def test_simultaneous_requests_when_available_have_exactly_one_holder(queue):
    owners = ("a", "b", "c", "d")
    for owner in owners:
        session(queue, owner)
    barrier = Barrier(4)

    def request(owner):
        barrier.wait(timeout=10)
        return queue.request(owner)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, owners))
    assert [result["state"] for result in results].count("ACQUIRED") == 1
    assert sorted(result["position"] for result in results) == [0, 1, 2, 3]


def test_simultaneous_same_owner_requests_create_one_ticket(queue):
    session(queue, "a")
    barrier = Barrier(4)

    def request(_):
        barrier.wait(timeout=10)
        return queue.request("a")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(4)))
    assert all(result == results[0] for result in results)
    assert results[0]["state"] == "ACQUIRED"
    with sqlite3.connect(queue.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rtm_requests").fetchone()[0] == 1


def test_real_owner_process_exit_uses_default_liveness_and_quarantines(tmp_path):
    path = tmp_path / "queue.sqlite3"
    queue = ResourceQueue(path)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        active = pool.submit(acquire_in_child, str(path)).result(timeout=30)
        assert active["state"] == "ACQUIRED"
    # Executor shutdown has joined the child. Its active ticket survives its exit.
    session(queue, "next", os.getpid(), time.time() + 120)
    waiting = queue.request("next")
    assert waiting["state"] == "WAITING"
    assert waiting["resource_state"] == "QUARANTINED"
    assert queue.status("child")["session_state"] == "DEAD"
    queue.recover("server confirmed hardware stopped")
    assert queue.require_owner("next", waiting["ticket"])["state"] == "ACQUIRED"


def test_request_idempotent_and_ticket_not_transferable(queue):
    session(queue, "a")
    session(queue, "b", pid=2)
    acquired = queue.request("a")
    waiting = queue.request("b")
    assert queue.request("a") == acquired
    assert queue.request("b") == waiting
    for operation in (queue.require_owner, queue.release):
        for owner, ticket in (("b", acquired["ticket"]), ("a", waiting["ticket"]), ("a", "invented")):
            with pytest.raises(PermissionError):
                operation(owner, ticket)
        with pytest.raises(ValueError, match="does not hold"):
            operation("b", waiting["ticket"])
    assert queue.status("a")["state"] == "ACQUIRED"
    assert acquired["ticket"] not in json.dumps(queue.status("b"))
    assert "opencode-a" not in json.dumps(queue.status("b"))
    assert queue.require_owner("a", acquired["ticket"]) == acquired
    queue.release("a", acquired["ticket"])
    # A duplicate release cannot free b's hardware or a newer ticket for a.
    queue.release("a", acquired["ticket"])
    assert queue.status("b")["state"] == "ACQUIRED"
    newer = queue.request("a")
    assert newer["ticket"] != acquired["ticket"]
    queue.release("a", acquired["ticket"])
    assert queue.status("a")["ticket"] == newer["ticket"]
    with pytest.raises(ValueError):
        queue.require_owner("a", acquired["ticket"])


def test_immutable_binding_and_registration_no_claim_before_bind(queue):
    result = queue.register("a", 1, 2000)
    assert result["state"] == "UNBOUND" and result["ticket"] is None
    assert queue.register("a", 1, 2000) == result
    with pytest.raises(ValueError, match="bound"):
        queue.request("a")
    assert queue.status("a")["resource_state"] == "AVAILABLE"
    bound = queue.bind("a", "actual-session")
    assert queue.bind("a", "actual-session") == bound
    with pytest.raises(ValueError, match="immutable"):
        queue.bind("a", "replacement-session")
    queue.register("b", 2, 2000)
    with pytest.raises(ValueError, match="another owner"):
        queue.bind("b", "actual-session")
    for pid, deadline in ((2, 2000), (1, 3000)):
        with pytest.raises(ValueError, match="immutable"):
            queue.register("a", pid, deadline)
    queue.close_session("a")
    with pytest.raises(ValueError, match="CLOSED"):
        queue.bind("a", "actual-session")
    with pytest.raises(ValueError, match="CLOSED"):
        queue.request("a")
    assert queue.register("a", 1, 2000)["session_state"] == "CLOSED"
    with pytest.raises(ValueError, match="another owner"):
        queue.bind("b", "actual-session")


@pytest.mark.parametrize("stale", ["death", "deadline"])
def test_active_stale_owner_quarantines_until_explicit_recovery(queue, stale):
    session(queue, "a", pid=1, deadline=1100)
    session(queue, "b", pid=2)
    active = queue.request("a")
    waiting = queue.request("b")
    if stale == "death":
        queue.pids.remove(1)
    else:
        queue.now[0] = 1100  # A live PID cannot override the absolute deadline.
    with pytest.raises(ValueError):
        queue.require_owner("a", active["ticket"])
    # Failed validation must persist quarantine rather than roll maintenance back.
    with sqlite3.connect(queue.path) as conn:
        assert conn.execute("SELECT state FROM rtm_resource").fetchone()[0] == "QUARANTINED"
    result = queue.status("b")
    assert result["resource_state"] == "QUARANTINED" and result["state"] == "WAITING"
    assert queue.status("a")["session_state"] == ("DEAD" if stale == "death" else "EXPIRED")
    with pytest.raises(ValueError):
        queue.release("a", active["ticket"])
    queue.close_session("a", safe=True)
    assert queue.status("b")["resource_state"] == "QUARANTINED"
    recovered = queue.recover("server stopped the hardware and verified idle")
    assert recovered["resource_state"] == "HELD"
    assert queue.require_owner("b", waiting["ticket"])["state"] == "ACQUIRED"
    assert queue.status("a")["state"] == "CANCELLED"
    with pytest.raises(ValueError):
        queue.require_owner("a", active["ticket"])
    with pytest.raises(ValueError, match="not quarantined"):
        queue.recover("repeat")


@pytest.mark.parametrize("stale", ["death", "deadline"])
def test_waiting_stale_session_removed_without_quarantining(queue, stale):
    session(queue, "holder", pid=1)
    held = queue.request("holder")
    session(queue, "stale", pid=2, deadline=1100)
    queue.request("stale")
    session(queue, "next", pid=3)
    next_request = queue.request("next")
    if stale == "death":
        queue.pids.remove(2)
    else:
        queue.now[0] = 1100
    assert queue.status("next")["position"] == 1
    assert queue.status("stale")["state"] == ("DEAD" if stale == "death" else "EXPIRED")
    assert queue.status("holder")["resource_state"] == "HELD"
    queue.release("holder", held["ticket"])
    assert queue.require_owner("next", next_request["ticket"])["state"] == "ACQUIRED"
    with pytest.raises(ValueError):
        queue.request("stale")


def test_cancel_waiter_idempotent_requeue_goes_to_tail(queue):
    for owner in ("holder", "a", "b"):
        session(queue, owner)
    held = queue.request("holder")
    old = queue.request("a")
    second = queue.request("b")
    assert queue.cancel("a")["state"] == "CANCELLED"
    assert queue.cancel("a")["ticket"] == old["ticket"]
    assert queue.status("b")["position"] == 1
    new = queue.request("a")
    assert new["position"] == 2 and new["ticket"] != old["ticket"]
    with pytest.raises(ValueError, match="cannot be cancelled"):
        queue.cancel("holder")
    queue.release("holder", held["ticket"])
    assert queue.require_owner("b", second["ticket"])["state"] == "ACQUIRED"
    queue.release("b", second["ticket"])
    assert queue.require_owner("a", new["ticket"])["state"] == "ACQUIRED"
    with pytest.raises(ValueError):
        queue.release("a", old["ticket"])


def test_safe_close_releases_unsafe_close_quarantines_and_waiting_close_cancels(queue):
    for owner in ("a", "b", "c"):
        session(queue, owner)
    queue.request("a")
    queue.request("b")
    queue.request("c")
    assert queue.close_session("b")["state"] == "CLOSED"
    assert queue.status("c")["position"] == 1
    assert queue.close_session("a")["session_state"] == "CLOSED"
    assert queue.status("c")["state"] == "ACQUIRED"
    assert queue.close_session("c", safe=False)["resource_state"] == "QUARANTINED"
    queue.close_session("c", safe=True)
    assert queue.status("a")["resource_state"] == "QUARANTINED"
    assert queue.recover("hardware verified safe")["resource_state"] == "AVAILABLE"


def test_explicit_quarantine_cannot_be_cleared_by_release_cancel_or_new_request(queue):
    session(queue, "a")
    session(queue, "b", pid=2)
    active = queue.request("a")
    queue.request("b")
    with pytest.raises(PermissionError):
        queue.quarantine("b", "foreign owner")
    result = queue.quarantine("a", "hardware timeout")
    assert result["state"] == "QUARANTINED" and result["reason"] == "hardware timeout"
    assert queue.request("a")["ticket"] == active["ticket"]
    with pytest.raises(ValueError):
        queue.cancel("a")
    with pytest.raises(ValueError):
        queue.release("a", active["ticket"])
    queue.recover("hardware reset")
    with pytest.raises(ValueError):
        queue.require_owner("a", active["ticket"])
    assert queue.request("a")["position"] == 1


def test_dead_and_expired_registration_never_acquire_even_after_pid_returns(queue):
    assert queue.register("dead", 999, 2000)["state"] == "DEAD"
    assert queue.register("expired", 1, 1000)["state"] == "EXPIRED"
    queue.pids.add(999)
    queue.now[0] = 900
    for owner in ("dead", "expired"):
        with pytest.raises(ValueError):
            queue.bind(owner, f"session-{owner}")
        assert queue.status(owner)["resource_state"] == "AVAILABLE"


def test_results_json_serializable_and_invalid_inputs(queue):
    for pid in (0, -1, True, "1"):
        with pytest.raises(ValueError):
            queue.register("a", pid, 2000)
    for deadline in (float("inf"), float("nan"), True, "2000"):
        with pytest.raises(ValueError):
            queue.register("a", 1, deadline)
    with pytest.raises(KeyError):
        queue.status("unknown")
    with pytest.raises(ValueError):
        queue.register("", 1, 2000)
    session(queue, "a")
    with pytest.raises(ValueError):
        queue.bind("a", "")
    with pytest.raises(ValueError):
        queue.close_session("a", safe="true")
    result = queue.request("a")
    assert json.loads(json.dumps(result)) == result
    assert result["resource"] == "rtm2032"
    assert len(result["ticket"]) >= 32
