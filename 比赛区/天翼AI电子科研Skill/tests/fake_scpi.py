"""假 SCPI 服务器：按脚本回放命令响应，用于完全离线的协议测试。

只依赖标准库；支持二进制确定块、跨包发送和可脚本化的错误队列。
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterable

Response = bytes | str | Callable[[str], bytes | str | None]


def encode_block(payload: bytes, *, digits: int | None = None) -> bytes:
    """把 payload 包成 IEEE 488.2 确定块（`#<n><len><payload>`）。"""
    length = str(len(payload)).encode("ascii")
    width = digits if digits is not None else len(length)
    if width != len(length):
        raise ValueError("requested digit width does not match payload length")
    return b"#" + str(width).encode("ascii") + length + payload


class FakeScpiServer:
    """按命令回放的 TCP 服务器。

    - `responses`：命令（不区分大小写）到响应的映射；响应可以是 bytes/str 或 `callable(command)`。
    - `errors`：`SYST:ERR?` 的队列，按顺序消费；耗尽后返回 `0,"No error"`。
    - `chunks`：把每个响应拆成几段发送，用于模拟跨包读取。
    """

    def __init__(
        self,
        responses: dict[str, Response] | None = None,
        *,
        errors: Iterable[str] = (),
        command_errors: dict[str, str] | None = None,
        default_response: Response = "",
        chunks: int = 1,
        idle_timeout: float = 2.0,
    ) -> None:
        self.responses = {key.upper(): value for key, value in (responses or {}).items()}
        self.errors = list(errors)
        self.command_errors = {key.upper(): value for key, value in (command_errors or {}).items()}
        self.default_response = default_response
        self.chunks = max(1, int(chunks))
        self.idle_timeout = float(idle_timeout)
        self.received: list[str] = []
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._received_event = threading.Event()

    @property
    def address(self) -> tuple[str, int]:
        if self._socket is None:
            raise RuntimeError("server is not started")
        host, port = self._socket.getsockname()[:2]
        return str(host), int(port)

    def __enter__(self) -> FakeScpiServer:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        server.settimeout(0.2)
        self._socket = server
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def wait_for_commands(self, count: int, timeout: float = 1.0) -> bool:
        """等待服务端实际解析指定数量命令，供只写命令测试消除关闭竞态。"""
        deadline = time.monotonic() + timeout
        while len(self.received) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._received_event.wait(remaining)
            self._received_event.clear()
        return True

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except (TimeoutError, OSError):
                continue
            with connection:
                self._handle(connection)

    def _handle(self, connection: socket.socket) -> None:
        # 连接在客户端关闭前保持存活：设备"无响应"不应导致服务端主动断开。
        connection.settimeout(self.idle_timeout)
        buffer = b""
        while not self._stop.is_set():
            try:
                data = connection.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not data:
                return
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                command = line.decode("ascii", "replace").strip()
                if not command:
                    continue
                self.received.append(command)
                self._received_event.set()
                self._queue_command_error(command)
                payload = self._response_for(command)
                if not payload:
                    continue
                for piece in self._pieces(payload):
                    try:
                        connection.sendall(piece)
                    except OSError:
                        return

    def _queue_command_error(self, command: str) -> None:
        """真实仪器：被拒绝的命令会往错误队列压一条；这里按命令脚本化同一行为。"""
        upper = command.upper()
        if upper.startswith("SYST:ERR"):
            return
        for pattern, error in self.command_errors.items():
            if pattern.endswith("*"):
                if upper.startswith(pattern[:-1]):
                    self.errors.append(error)
                    return
            elif upper == pattern:
                self.errors.append(error)
                return

    def _response_for(self, command: str) -> bytes:
        if command.upper().startswith("SYST:ERR"):
            raw: Response = self.errors.pop(0) if self.errors else '0,"No error"'
        else:
            raw = self.responses.get(command.upper(), self.default_response)
        if callable(raw):
            raw = raw(command)
        if raw is None:
            return b""
        if isinstance(raw, bytes):
            return raw
        # 文本响应按 SCPI 惯例补 LF；空串表示"该命令无响应"。
        return (raw + "\n").encode("utf-8") if raw else b""

    def _pieces(self, payload: bytes) -> list[bytes]:
        if self.chunks <= 1 or len(payload) < 2:
            return [payload]
        size = max(1, len(payload) // self.chunks)
        return [payload[index : index + size] for index in range(0, len(payload), size)]


__all__ = ["FakeScpiServer", "Response", "encode_block"]
