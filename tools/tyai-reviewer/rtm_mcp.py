"""Session-scoped RTM2032 admission queue exposed as a local MCP server (stdio).

Each OpenCode review session runs its own copy of this server. The trusted runner
registers an opaque owner key and injects it through ``TYAI_RTM_OWNER``; the tool
arguments can never name another owner, session, PID, ticket, or recovery action.
That is what keeps the instrument from being allocated across sessions.

This server only arbitrates access and reports status. It never performs SCPI or
waveform I/O, so it cannot leave hardware in an unknown state on its own. Real
operations stay with the WaveBench CLI under the lease described in the policy
file. A live holder must still call ``rtm2032_release`` before the session ends;
otherwise the runner quarantines the resource until an operator confirms safety.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

from rtm_queue import ResourceQueue

SERVER_NAME = "tyai_rtm2032_queue"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
MAX_WAIT_SECONDS = 120

NOTICE = (
    "本机所在局域网内有一台可用的 RTM2032 示波器。自动评审通过本 MCP 服务器"
    "做会话级串行分配：同一时刻只允许一个评审会话占用，凭据不可跨会话转让。"
    "只有事件确实需要实机证据时才申请；申请成功后按 WaveBench 技能先核对身份和"
    "接线、记录写前状态、保持高阻策略，完成后必须调用 rtm2032_release 释放。"
    "真实地址、序列号和实验数据只留本地，禁止写入 GitHub。"
)

TOOLS = [
    {
        "name": "rtm2032_environment",
        "description": (
            "说明局域网 RTM2032 示波器的可用性、会话级分配规则和实机安全边界。"
            "任何会话都可随时调用，不占用资源。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rtm2032_status",
        "description": (
            "查询本会话当前的资源状态、队列位置和资源是否被占用或隔离。"
            "不占用资源，可在申请后轮询。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rtm2032_acquire",
        "description": (
            "为本评审会话申请 RTM2032，按先来后到排队。可传 wait_seconds 等待最多 120 秒。"
            "返回 state=ACQUIRED 才表示拿到独占权；拿不到时保持 WAITING 并稍后再查。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "rtm2032_release",
        "description": (
            "在实机操作完成并确认设备状态已恢复后释放 RTM2032，把资源交给下一个会话。"
            "未持有时调用是安全的空操作。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rtm2032_cancel",
        "description": (
            "取消本会话仍在排队的申请。已持有时不能取消，必须先 release。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def queue_path(state_dir: str) -> Path:
    return Path(state_dir) / "rtm-resource.sqlite3"


class SessionQueue:
    """Wraps ResourceQueue with the server-side owner identity for one session."""

    def __init__(self, owner: str, path: Path, clock=time.time):
        self.owner = owner
        self.queue = ResourceQueue(path, clock=clock)

    def status(self) -> dict:
        return self.queue.status(self.owner)

    def acquire(self, wait_seconds: int) -> dict:
        snapshot = self.queue.request(self.owner)
        deadline = time.monotonic() + wait_seconds
        while snapshot["state"] == "WAITING" and time.monotonic() < deadline:
            time.sleep(1)
            snapshot = self.queue.status(self.owner)
        return snapshot

    def release(self) -> dict:
        snapshot = self.queue.status(self.owner)
        if snapshot["state"] == "ACQUIRED" and snapshot["ticket"]:
            self.queue.release(self.owner, snapshot["ticket"])
        return self.queue.status(self.owner)

    def cancel(self) -> dict:
        snapshot = self.queue.status(self.owner)
        if snapshot["state"] in {"WAITING", "ACQUIRED", "QUARANTINED"}:
            self.queue.cancel(self.owner)
            return self.queue.status(self.owner)
        return snapshot


def _session_from_environment() -> SessionQueue:
    owner = os.environ.get("TYAI_RTM_OWNER", "").strip()
    state_dir = os.environ.get("TYAI_RTM_STATE_DIR", "").strip()
    if not owner or not state_dir:
        raise RuntimeError("缺少会话身份：TYAI_RTM_OWNER / TYAI_RTM_STATE_DIR 未设置")
    session = SessionQueue(owner, queue_path(state_dir))
    # The runner registers the actual OpenCode process immediately after spawn.
    # A server cannot register itself or invent another session's identity.
    for attempt in range(50):
        try:
            session.status()
            break
        except KeyError:
            if attempt == 49:
                raise RuntimeError("评审进程尚未登记，拒绝分配") from None
            time.sleep(0.05)
    return session


def _tool_result(payload: dict, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def call_tool(name: str, arguments: dict, session: SessionQueue) -> dict:
    allowed = {"wait_seconds"} if name == "rtm2032_acquire" else set()
    if set(arguments) - allowed:
        raise ValueError("禁止指定 owner、session、ticket 或其他非公开参数")
    if name == "rtm2032_environment":
        try:
            snapshot = session.status()
        except (KeyError, ValueError) as exc:
            return _tool_result({"status": "unavailable", "reason": str(exc), "notice": NOTICE}, True)
        return _tool_result({"notice": NOTICE, "session": snapshot})
    if name == "rtm2032_status":
        return _tool_result(session.status())
    if name == "rtm2032_acquire":
        wait_seconds = arguments.get("wait_seconds", 0)
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int):
            return _tool_result({"status": "invalid", "reason": "wait_seconds 必须是 0..120 的整数"}, True)
        if not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise ValueError("wait_seconds 必须是 0..120 的整数")
        snapshot = session.acquire(wait_seconds)
        return _tool_result({"notice": NOTICE, "session": snapshot})
    if name == "rtm2032_release":
        return _tool_result({"notice": NOTICE, "session": session.release()})
    if name == "rtm2032_cancel":
        return _tool_result({"notice": NOTICE, "session": session.cancel()})
    return _tool_result({"status": "unknown_tool", "reason": name}, True)


def handle_message(message: dict, session: SessionQueue) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested or DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "arguments must be an object")
        try:
            result = call_tool(name, arguments, session)
        except (KeyError, ValueError, PermissionError) as exc:
            result = _tool_result({"status": "rejected", "reason": str(exc)}, True)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method == "resources/list" else "prompts"
        return {"jsonrpc": "2.0", "id": request_id, "result": {key: []}}
    return _error(request_id, -32601, f"method not found: {method}")


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> int:
    session = _session_from_environment()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            stdout.write(json.dumps(_error(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        if not isinstance(message, dict):
            continue
        response = handle_message(message, session)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        return serve()
    except Exception as exc:  # Surface a protocol error instead of a silent exit.
        sys.stderr.write(f"rtm_mcp failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
