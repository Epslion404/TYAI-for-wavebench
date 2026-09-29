"""SCPI over TCP 会话、IEEE 488.2 确定块解析与错误队列读取（只用标准库）。"""

from __future__ import annotations

import socket
from collections.abc import Callable

MAX_TEXT_BYTES = 64 * 1024
DEFAULT_MAX_BLOCK_BYTES = 8 * 1024 * 1024

Reader = Callable[[int], bytes]


def encode_command(command: str) -> bytes:
    """把命令编码为设备可接受的一行（统一 LF 结尾）。"""
    return (command.rstrip() + "\n").encode("ascii", "replace")


def parse_first_float(text: str) -> float | None:
    """取响应首个逗号分隔字段的浮点值；空串或非数值返回 None。"""
    candidate = (text or "").strip().split(",")[0].strip()
    if not candidate:
        return None
    try:
        return float(candidate)
    except ValueError:
        return None


def parse_block_header(header: bytes) -> tuple[int, int]:
    """解析块头 `#<n><len>`，返回 (payload 长度, 头部字节数)。"""
    if not header.startswith(b"#"):
        raise ValueError(f"响应不是 IEEE 488.2 确定块: {header[:16]!r}")
    if len(header) < 2:
        raise ValueError("块头长度不足")
    digit = header[1:2]
    if not digit.isdigit():
        raise ValueError(f"块头缺少数字位: {header[:16]!r}")
    count = int(digit)
    if count == 0:
        raise ValueError("不支持长度未定的块（#0）")
    if len(header) < 2 + count:
        raise ValueError("块长度字段不完整")
    field = header[2 : 2 + count]
    if not field.isdigit():
        raise ValueError(f"块长度字段非法: {field!r}")
    return int(field), 2 + count


def read_definite_block(reader: Reader, max_bytes: int = DEFAULT_MAX_BLOCK_BYTES) -> bytes:
    """按 `reader(size)` 读取完整确定块；reader 必须返回恰好 size 字节。

    分包、payload 内含 `#`/`\\n` 都不影响结果；声明长度超限直接失败，
    而不是读到一个"看起来可用"的截断波形。
    """
    first = reader(2)
    if not first.startswith(b"#"):
        raise ValueError(f"响应不是 IEEE 488.2 确定块: {first!r}")
    digit = first[1:2]
    if not digit.isdigit():
        raise ValueError(f"块头缺少数字位: {first!r}")
    count = int(digit)
    if count == 0:
        raise ValueError("不支持长度未定的块（#0）")
    length_field = reader(count)
    if not length_field.isdigit():
        raise ValueError(f"块长度字段非法: {length_field!r}")
    length = int(length_field)
    if length > max_bytes:
        raise ValueError(f"块长度 {length} 超过上限 {max_bytes}")
    return reader(length)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """读满 size 字节；连接提前关闭抛 ConnectionError，超时抛 TimeoutError。"""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        data = sock.recv(min(remaining, 65536))
        if not data:
            raise ConnectionError("SCPI 连接在读取完成前关闭")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def query_once(host: str, port: int, command: str, timeout: float = 1.5) -> str:
    """一次性文本查询（短连接）；仅用于 `*IDN?` 这类单行响应。"""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(encode_command(command))
        buffer = b""
        while b"\n" not in buffer and len(buffer) < MAX_TEXT_BYTES:
            try:
                data = sock.recv(4096)
            except TimeoutError:
                break
            if not data:
                break
            buffer += data
        return buffer.decode("utf-8", "replace").strip()


class ScpiSession:
    """单条 TCP 连接内复用查询，避免每条命令重新握手。"""

    def __init__(self, host: str, port: int = 5025, timeout: float = 2.0) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._socket: socket.socket | None = None

    def open(self) -> ScpiSession:
        if self._socket is None:
            self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._socket.settimeout(self.timeout)
        return self

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def __enter__(self) -> ScpiSession:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require(self) -> socket.socket:
        if self._socket is None:
            raise ConnectionError("SCPI 会话尚未打开")
        return self._socket

    def write(self, command: str) -> None:
        """发送无响应命令（例如 `FORM REAL`）。"""
        self._require().sendall(encode_command(command))

    def query(self, command: str) -> str:
        """发送命令并读取单行文本响应。

        设备对该命令无响应时返回空串（真实仪器常用这种方式表示命令被拒绝）；
        响应读到一半才超时则抛错，不把不完整数据当结果。
        """
        sock = self._require()
        sock.sendall(encode_command(command))
        buffer = b""
        while b"\n" not in buffer and len(buffer) < MAX_TEXT_BYTES:
            try:
                data = sock.recv(4096)
            except TimeoutError:
                if not buffer:
                    return ""
                raise
            if not data:
                break
            buffer += data
        return buffer.decode("utf-8", "replace").strip()

    def query_block(self, command: str, max_bytes: int = DEFAULT_MAX_BLOCK_BYTES) -> bytes:
        """发送命令并读取 IEEE 488.2 确定块。"""
        sock = self._require()
        sock.sendall(encode_command(command))
        return read_definite_block(lambda size: recv_exact(sock, size), max_bytes)

    def drain_errors(self, limit: int = 16) -> list[str]:
        """读空仪器的错误队列，返回非空条目（`0,"No error"` 视为干净并停止）。"""
        found: list[str] = []
        for _ in range(max(1, limit)):
            text = self.query("SYST:ERR?").strip()
            if not text or _is_no_error(text):
                break
            found.append(text)
        return found


def _is_no_error(text: str) -> bool:
    if "no error" in text.lower():
        return True
    code = text.split(",", 1)[0].strip().lstrip("+")
    return code == "0"


__all__ = [
    "DEFAULT_MAX_BLOCK_BYTES",
    "MAX_TEXT_BYTES",
    "ScpiSession",
    "encode_command",
    "parse_block_header",
    "parse_first_float",
    "query_once",
    "read_definite_block",
    "recv_exact",
]
