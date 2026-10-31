import io
import json
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import rtm_mcp


def make_session(tmp_path):
    owner = "owner-test"
    session = rtm_mcp.SessionQueue(owner, tmp_path / "rtm.sqlite3",
                                   clock=lambda: 1000.0)
    session.queue.register(owner, os.getpid(), 2000.0)
    session.queue.bind(owner, "session-test")
    return session


def test_protocol_advertises_session_scoped_tools(tmp_path):
    session = make_session(tmp_path)
    response = rtm_mcp.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, session)
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "rtm2032_acquire" in names
    assert "rtm2032_release" in names
    assert "rtm2032_environment" in names


def test_acquire_and_release_are_bound_to_server_session(tmp_path):
    session = make_session(tmp_path)
    acquired = rtm_mcp.call_tool("rtm2032_acquire", {}, session)
    body = json.loads(acquired["content"][0]["text"])
    assert body["session"]["state"] == "ACQUIRED"
    released = rtm_mcp.call_tool("rtm2032_release", {}, session)
    body = json.loads(released["content"][0]["text"])
    assert body["session"]["state"] == "RELEASED"


def test_stdio_server_requires_trusted_session_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("TYAI_RTM_OWNER", raising=False)
    monkeypatch.delenv("TYAI_RTM_STATE_DIR", raising=False)
    assert rtm_mcp.main() == 1


def test_stdio_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("TYAI_RTM_OWNER", "stdio-owner")
    monkeypatch.setenv("TYAI_RTM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TYAI_RTM_PID", str(os.getpid()))
    monkeypatch.setenv("TYAI_RTM_DEADLINE", "2000")
    queue = rtm_mcp.ResourceQueue(rtm_mcp.queue_path(str(tmp_path)))
    queue.register("stdio-owner", os.getpid(), time.time() + 120)
    incoming = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    output = io.StringIO()
    assert rtm_mcp.serve(io.StringIO(incoming), output) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert messages[0]["result"]["serverInfo"]["name"] == rtm_mcp.SERVER_NAME
    assert any(message.get("id") == 2 for message in messages)


def test_mcp_rejects_foreign_session_arguments(tmp_path):
    session = make_session(tmp_path)
    with pytest.raises(ValueError, match="禁止指定"):
        rtm_mcp.call_tool("rtm2032_acquire", {"owner": "foreign-session"}, session)
    assert session.status()["state"] == "IDLE"
