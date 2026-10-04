"""跨网段反向 TCP 桥接：设备端主动连接中继，Agent 端使用本地代理。"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

HANDSHAKE_LIMIT = 512
DEFAULT_HANDSHAKE_TIMEOUT = 30.0


def _addr(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"地址必须是 host:port: {value}")
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError("端口必须位于 1..65535")
    return host or "127.0.0.1", number


def _validate_token(token: str) -> str:
    token = token.strip()
    if not token or len(token) > 128:
        raise ValueError("bridge token 长度必须位于 1..128")
    if any(character.isspace() or ord(character) < 33 or ord(character) > 126 for character in token):
        raise ValueError("bridge token 只能包含可打印 ASCII 且不能含空白")
    return token


def _recv_line(sock: socket.socket, limit: int = HANDSHAKE_LIMIT) -> bytes:
    data = bytearray()
    while len(data) < limit:
        chunk = sock.recv(1)
        if not chunk:
            break
        data.extend(chunk)
        if chunk == b"\n":
            return bytes(data).strip()
    if len(data) >= limit:
        raise ValueError("bridge 握手行超过长度上限")
    return bytes(data).strip()


def _close(sock: socket.socket | None) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _relay(left: socket.socket, right: socket.socket) -> None:
    def pump(source: socket.socket, target: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            try:
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    first = threading.Thread(target=pump, args=(left, right), daemon=True)
    second = threading.Thread(target=pump, args=(right, left), daemon=True)
    first.start()
    second.start()
    first.join()
    second.join()


@dataclass
class _Slot:
    device: socket.socket
    ready: threading.Event = field(default_factory=threading.Event)
    start: threading.Event = field(default_factory=threading.Event)
    peer: socket.socket | None = None
    claimed: bool = False


class BridgeServer:
    def __init__(self, host: str, port: int, handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT):
        if handshake_timeout <= 0:
            raise ValueError("handshake_timeout 必须大于 0")
        self.host = host
        self.port = int(port)
        self.handshake_timeout = float(handshake_timeout)
        self.slots: dict[str, _Slot] = {}
        self.lock = threading.Lock()

    def serve_forever(self) -> None:
        with socket.create_server((self.host, self.port)) as listener:
            print(f"bridge server listening on {self.host}:{self.port}", flush=True)
            while True:
                connection, _ = listener.accept()
                threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(self.handshake_timeout)
        try:
            first = _recv_line(connection).decode("ascii", "strict").split()
            if len(first) != 2 or first[0] not in {"REGISTER", "CONNECT"}:
                connection.sendall(b"ERROR invalid-handshake\n")
                return
            role, token = first[0], _validate_token(first[1])
            if role == "REGISTER":
                self._register(token, connection)
            else:
                self._connect(token, connection)
        except (OSError, UnicodeError, ValueError):
            _close(connection)

    def _register(self, token: str, connection: socket.socket) -> None:
        slot = _Slot(connection)
        with self.lock:
            previous = self.slots.get(token)
            self.slots[token] = slot
        if previous is not None:
            _close(previous.device)
            _close(previous.peer)
        try:
            while not slot.start.is_set():
                command = _recv_line(connection)
                if command == b"READY":
                    slot.ready.set()
                    if not slot.start.wait(self.handshake_timeout):
                        return
                    break
                if not command:
                    return
            connection.settimeout(None)
            if slot.peer is not None:
                slot.peer.settimeout(None)
                _relay(connection, slot.peer)
        finally:
            with self.lock:
                if self.slots.get(token) is slot:
                    self.slots.pop(token, None)
            _close(slot.peer)
            _close(connection)

    def _connect(self, token: str, connection: socket.socket) -> None:
        with self.lock:
            slot = self.slots.get(token)
            if slot is None:
                connection.sendall(b"ERROR no-device\n")
                _close(connection)
                return
            if slot.claimed:
                connection.sendall(b"ERROR device-busy\n")
                _close(connection)
                return
            slot.claimed = True
            slot.peer = connection
        try:
            slot.device.sendall(b"OPEN\n")
            if not slot.ready.wait(self.handshake_timeout):
                connection.sendall(b"ERROR device-not-ready\n")
                return
            slot.device.sendall(b"START\n")
            connection.sendall(b"START\n")
            slot.start.set()
        except OSError:
            _close(connection)


def run_expose(
    server: str,
    token: str,
    local: str,
    *,
    reconnect_delay: float = 1.0,
    stop_event: threading.Event | None = None,
    retry_wait: Callable[[float], None] = time.sleep,
) -> None:
    """持续注册设备；每个代理会话结束后自动重新注册。"""
    server_host, server_port = _addr(server)
    local_host, local_port = _addr(local)
    token = _validate_token(token)
    if reconnect_delay < 0:
        raise ValueError("reconnect_delay 不能为负数")
    while stop_event is None or not stop_event.is_set():
        retry_after_session = True
        try:
            with socket.create_connection((server_host, server_port), timeout=10) as upstream:
                upstream.settimeout(DEFAULT_HANDSHAKE_TIMEOUT)
                upstream.sendall(f"REGISTER {token}\n".encode("ascii"))
                if _recv_line(upstream) != b"OPEN":
                    continue
                device = socket.create_connection((local_host, local_port), timeout=10)
                try:
                    upstream.sendall(b"READY\n")
                    if _recv_line(upstream) != b"START":
                        continue
                    upstream.settimeout(None)
                    device.settimeout(None)
                    _relay(upstream, device)
                    retry_after_session = False
                finally:
                    _close(device)
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            # REGISTER/READY 的握手失败同样需要退避；否则服务端拒绝时会形成重连风暴。
            if (
                retry_after_session
                and reconnect_delay
                and (stop_event is None or not stop_event.is_set())
            ):
                retry_wait(reconnect_delay)


def run_connect(server: str, token: str, listen: str) -> None:
    server_host, server_port = _addr(server)
    listen_host, listen_port = _addr(listen)
    token = _validate_token(token)
    with socket.create_server((listen_host, listen_port)) as listener:
        print(f"bridge proxy listening on {listen_host}:{listen_port}", flush=True)
        while True:
            local, _ = listener.accept()
            threading.Thread(
                target=_connect_one,
                args=(local, server_host, server_port, token),
                daemon=True,
            ).start()


def _connect_one(local: socket.socket, host: str, port: int, token: str) -> None:
    upstream: socket.socket | None = None
    try:
        upstream = socket.create_connection((host, port), timeout=10)
        upstream.settimeout(DEFAULT_HANDSHAKE_TIMEOUT)
        upstream.sendall(f"CONNECT {token}\n".encode("ascii"))
        if _recv_line(upstream) != b"START":
            return
        local.settimeout(None)
        upstream.settimeout(None)
        _relay(local, upstream)
    except OSError:
        pass
    finally:
        _close(local)
        _close(upstream)


__all__ = ["BridgeServer", "run_connect", "run_expose"]
