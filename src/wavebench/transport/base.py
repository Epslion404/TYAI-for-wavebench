from __future__ import annotations

from typing import Protocol

from .contracts import BinaryQueryResult, BinaryResponseFraming, ReplayPolicy

class InstrumentTransport(Protocol):
    resource: str
    def record_event(self, direction: str, text: str) -> None: ...
    def write(self, command: str) -> None: ...
    def write_bytes(self, command: bytes) -> None: ...
    # 原始帧是可选的传输能力：目前只有串口后端实现。缺失时由 GuardedAuditedTransport
    # 在传输前报 `raw_frame_unsupported`，调用方不应假设所有后端都支持。
    def send_frame(
        self,
        frame: bytes,
        *,
        max_bytes: int,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> None: ...
    def read_exact(
        self,
        size: int,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes: ...
    def read_until(
        self,
        delimiter: bytes,
        *,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes: ...
    def query(
        self,
        command: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> str: ...
    def query_float_list(
        self,
        command: str,
        *,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> list[float]: ...
    def query_bin_block(
        self,
        command: str,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> bytes: ...
    def query_binary(
        self,
        command: str,
        *,
        framing: BinaryResponseFraming,
        max_bytes: int,
        timeout_ms: int | None = None,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> BinaryQueryResult: ...
    def query_opc(
        self,
        *,
        replay: ReplayPolicy = ReplayPolicy.NO_REPLAY,
    ) -> str: ...
    def close(self) -> None: ...
