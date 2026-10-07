"""SCPI over TCP 会话、IEEE 488.2 确定块解析与错误队列读取（只用标准库）。"""

from __future__ import annotations

import socket
import math
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

MAX_TEXT_BYTES = 64 * 1024
DEFAULT_MAX_BLOCK_BYTES = 8 * 1024 * 1024

Reader = Callable[[int], bytes]


class ConfigurationError(ValueError):
    """调用参数非法；仅用于调用方输入，不用于设备响应解析。"""


class ScpiConfigurationError(ConfigurationError):
    """SCPI 客户端参数非法，与设备响应解析失败区分开。"""


def encode_command(command: str) -> bytes:
    """把命令编码为设备可接受的一行（统一 LF 结尾）。"""
    if not isinstance(command, str):
        raise ScpiConfigurationError("SCPI command must be text")
    normalized = command.rstrip()
    if not normalized:
        raise ScpiConfigurationError("SCPI command must not be empty")
    if any(character in normalized for character in ("\r", "\n", "\x00", ";")):
        raise ScpiConfigurationError("SCPI command must be one line and must not contain command separators")
    try:
        return (normalized + "\n").encode("ascii")
    except UnicodeEncodeError as exc:
        raise ScpiConfigurationError("SCPI command must contain ASCII characters only") from exc


_CHANNEL_RE = re.compile(r"^CHAN[1-4]$", re.IGNORECASE)


def validate_channel(channel: str) -> str:
    """Return a canonical oscilloscope channel name safe for command composition."""
    if not isinstance(channel, str) or not _CHANNEL_RE.fullmatch(channel.strip()):
        raise ScpiConfigurationError("channel must be one of CHAN1, CHAN2, CHAN3, CHAN4")
    return channel.strip().upper()


def classify_exception(exc: BaseException) -> str:
    """把传输异常稳定映射为对 Agent 有意义的失败类别。"""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (ConnectionError, OSError)):
        return "transport_error"
    if isinstance(exc, ConfigurationError):
        return "configuration_error"
    if isinstance(exc, ValueError):
        return "parse_error"
    return "internal_error"


def parse_first_float(text: str) -> float | None:
    """取响应首个逗号分隔字段的浮点值；空串或非数值返回 None。"""
    candidate = (text or "").strip().split(",")[0].strip()
    if not candidate:
        return None
    try:
        value = float(candidate)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


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
    with ScpiSession(host, port, timeout) as session:
        return session.query(command)


class ScpiSession:
    """单条 TCP 连接内复用查询，避免每条命令重新握手。"""

    def __init__(self, host: str, port: int = 5025, timeout: float = 2.0) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ScpiConfigurationError("host 不能为空")
        try:
            port_value = int(port)
        except (TypeError, ValueError) as exc:
            raise ScpiConfigurationError("port 必须位于 1..65535") from exc
        if not 1 <= port_value <= 65535:
            raise ScpiConfigurationError("port 必须位于 1..65535")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ScpiConfigurationError("timeout 必须是有限正数") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ScpiConfigurationError("timeout 必须是有限正数")
        self.host = host
        self.port = port_value
        self.timeout = timeout_value
        self._socket: socket.socket | None = None
        # Keep bytes beyond a line terminator for the next query.  TCP packet
        # boundaries do not correspond to SCPI response boundaries.
        self._read_buffer = bytearray()
        # A CR may be the first half of CRLF.  If the LF arrives in the next
        # recv, consume it before interpreting the next response.
        self._awaiting_lf_after_cr = False
        # Definite blocks may have one optional LF, CR or CRLF after the
        # payload.  Consume it lazily, under the next query's deadline.
        self._pending_block_ending = False

    def open(self) -> ScpiSession:
        if self._socket is None:
            self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._socket.settimeout(self.timeout)
        return self

    def close(self) -> None:
        try:
            if self._socket is not None:
                # 先半关闭写端，让已经 sendall 的无响应 SCPI 命令以 FIN 结束，
                # 避免 Windows 上直接 close 偶发以 RST 丢弃最后一条命令。
                try:
                    self._socket.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                self._socket.close()
        finally:
            self._socket = None
            self._read_buffer.clear()
            self._awaiting_lf_after_cr = False
            self._pending_block_ending = False

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

        实际空行返回空串；超时（包括没有收到任何字节）抛 TimeoutError，
        关闭会话以免迟到响应污染下一条查询。异常携带 scpi_command。
        """
        return self._query_line(command)

    @contextmanager
    def _query_exchange(self, command: str) -> Iterator[float]:
        """One deadline covers sending, framing and the entire response."""
        encoded = encode_command(command)
        try:
            sock = self._require()
        except ConnectionError as exc:
            exc.scpi_command = command
            raise
        original_timeout = sock.gettimeout()
        deadline = time.monotonic() + self.timeout
        try:
            sock.settimeout(self._remaining(deadline))
            sock.sendall(encoded)
            yield deadline
            self._remaining(deadline)
        except (OSError, ValueError) as exc:
            exc.scpi_command = command
            # Preserve the read/parse failure even if socket.close also fails.
            try:
                self.close()
            except OSError:
                pass
            if isinstance(exc, TimeoutError):
                failure = TimeoutError(f"SCPI query {command!r} timed out: {exc}")
                failure.scpi_command = command
                raise failure from exc
            raise
        finally:
            if self._socket is sock:
                sock.settimeout(original_timeout)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("SCPI query deadline exceeded")
        return min(self.timeout, remaining)

    def _receive(self, deadline: float, size: int = 4096) -> None:
        sock = self._require()
        sock.settimeout(self._remaining(deadline))
        data = sock.recv(size)
        self._remaining(deadline)
        if not data:
            raise ConnectionError("SCPI connection closed before a complete response")
        self._read_buffer.extend(data)

    def _prepare_response(self, deadline: float) -> None:
        """Discard only the preceding response's deferred terminator."""
        while self._awaiting_lf_after_cr or self._pending_block_ending:
            self._remaining(deadline)
            if not self._read_buffer:
                self._receive(deadline)
            first = self._read_buffer[0]
            if self._awaiting_lf_after_cr:
                if first == ord("\n"):
                    del self._read_buffer[0]
                self._awaiting_lf_after_cr = False
            else:
                if first in (ord("\r"), ord("\n")):
                    del self._read_buffer[0]
                    self._awaiting_lf_after_cr = first == ord("\r")
                self._pending_block_ending = False

    def _query_line(self, command: str, *, require_response: bool = False) -> str:
        """Send one query and consume exactly one response line.

        ``require_response`` is reserved for protocol-critical queries such as
        ``SYST:ERR?``.  A timeout or an empty response then invalidates this
        session, so a delayed response cannot be mistaken for the next query.
        """
        with self._query_exchange(command) as deadline:
            self._prepare_response(deadline)
            while True:
                self._remaining(deadline)
                line = self._take_buffered_line()
                if line is not None:
                    if len(line) > MAX_TEXT_BYTES:
                        raise ValueError(f"SCPI 文本响应超过上限 {MAX_TEXT_BYTES} 字节")
                    text = line.decode("utf-8", "replace").strip()
                    if require_response and not text:
                        raise TimeoutError("SCPI query returned an empty response")
                    return text
                if len(self._read_buffer) >= MAX_TEXT_BYTES:
                    raise ValueError(f"SCPI 文本响应超过上限 {MAX_TEXT_BYTES} 字节或缺少行结束符")
                self._receive(deadline)

    def _take_buffered_line(self) -> bytes | None:
        for index, byte in enumerate(self._read_buffer):
            if byte == ord("\n"):
                line = bytes(self._read_buffer[:index])
                del self._read_buffer[: index + 1]
                return line
            if byte == ord("\r"):
                line = bytes(self._read_buffer[:index])
                del self._read_buffer[: index + 1]
                if self._read_buffer[:1] == b"\n":
                    del self._read_buffer[0]
                else:
                    self._awaiting_lf_after_cr = True
                return line
        return None

    def query_block(self, command: str, max_bytes: int = DEFAULT_MAX_BLOCK_BYTES) -> bytes:
        """发送命令并读取 IEEE 488.2 确定块。"""
        with self._query_exchange(command) as deadline:
            self._prepare_response(deadline)
            payload = read_definite_block(lambda size: self._read_exact(size, deadline), max_bytes)
            self._pending_block_ending = True
            return payload

    def _read_exact(self, size: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        while size > 0:
            self._remaining(deadline)
            if not self._read_buffer:
                self._receive(deadline, min(size, 65536))
            count = min(size, len(self._read_buffer))
            chunks.append(bytes(self._read_buffer[:count]))
            del self._read_buffer[:count]
            size -= count
        return b"".join(chunks)

    def drain_errors(self, limit: int = 16) -> list[str]:
        """读空仪器的错误队列，返回非空条目（`0,"No error"` 视为干净并停止）。"""
        try:
            found: list[str] = []
            for _ in range(max(1, limit)):
                text = self._query_line("SYST:ERR?", require_response=True)
                if _is_no_error(text):
                    break
                found.append(text)
            return found
        except (ConnectionError, TimeoutError, ValueError):
            # A late error-queue response would otherwise be consumed by the
            # next measurement query and corrupt the request/response order.
            self.close()
            raise


def _is_no_error(text: str) -> bool:
    if "no error" in text.lower():
        return True
    code = text.split(",", 1)[0].strip().lstrip("+-")
    return code == "0"


__all__ = [
    "DEFAULT_MAX_BLOCK_BYTES",
    "ConfigurationError",
    "MAX_TEXT_BYTES",
    "ScpiConfigurationError",
    "ScpiSession",
    "classify_exception",
    "encode_command",
    "parse_block_header",
    "parse_first_float",
    "query_once",
    "read_definite_block",
    "recv_exact",
    "validate_channel",
]
