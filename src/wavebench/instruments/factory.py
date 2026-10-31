from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Mapping, cast

from wavebench.config import ConnectionConfig, SerialConnectionConfig
from wavebench.errors import ConfigError
from wavebench.logging import CommandLogger
from wavebench.plugins.api import PluginKind
from wavebench.services.access_policy import AccessMode, normalize_access_mode
from wavebench.services.resource_lease import ResourceLease, resource_fingerprint
from wavebench.transport.base import InstrumentTransport
from wavebench.transport.binary import visa_binary_contract_supported
from wavebench.transport.can_transport import CanTransport
from wavebench.transport.guarded import GuardedAuditedTransport
from wavebench.transport.pyvisa_transport import PyVisaTransport
from wavebench.transport.rsinstrument_transport import RsInstrumentTransport
from wavebench.transport.serial_transport import SerialTransport
from wavebench.transport.session import InstrumentSessionState

from .api import DriverContext, InstrumentDescriptor
from .capabilities import validate_declared_capabilities
from .contracts import InstrumentDriver
from .registry import resolve_instrument_descriptor
from .scope_extension_capabilities import SCOPE_STRICT_V2_CAPABILITIES

RSINSTRUMENT_BACKENDS = (
    "rsinstrument-socket",
    "rsinstrument",
    "rsinstrument-rsvisa",
    "rsinstrument-pyvisa-py",
)

RSINSTRUMENT_SOCKET_DEFAULT_PORT = 5025


@dataclass(frozen=True)
class OpenedInstrument:
    descriptor: InstrumentDescriptor
    driver: InstrumentDriver
    transport: InstrumentTransport | None = None
    session_state: InstrumentSessionState | None = None


def open_instrument_driver(
    *,
    driver_reference: str,
    expected_kind: PluginKind,
    resource: str,
    configured_backend: str,
    timeout_ms: int,
    opc_timeout_ms: int,
    read_retry_attempts: int,
    read_retry_delay_ms: int,
    logger: CommandLogger,
    settings: Mapping[str, object] | None = None,
    options: Mapping[str, object] | None = None,
    serial_config: SerialConnectionConfig | None = None,
    access: AccessMode = "read_write",
    lease: ResourceLease | None = None,
) -> OpenedInstrument:
    normalized_access = normalize_access_mode(access, "access")
    if lease is not None and lease.fingerprint != resource_fingerprint(resource, lease.lock_id):
        raise ConfigError("resource lease does not match configured instrument resource")
    descriptor = resolve_instrument_descriptor(
        driver_reference,
        expected_kind=expected_kind,
    )
    waveform_binary_profile = (
        descriptor.scope_extensions.waveform_binary_profile
        if descriptor.scope_extensions is not None
        else None
    )
    average_capture_profile = (
        descriptor.scope_extensions.average_capture_profile_v2
        if descriptor.scope_extensions is not None
        else None
    )
    bounded_binary_profile_opt_in = (
        waveform_binary_profile is not None or average_capture_profile is not None
    )
    strict_v2_capability_opt_in = bool(
        set(descriptor.capabilities) & SCOPE_STRICT_V2_CAPABILITIES
    )
    construction_latched = bounded_binary_profile_opt_in or strict_v2_capability_opt_in
    backend = _select_backend(configured_backend, descriptor.backends)
    _validate_resource_scheme(resource, descriptor.resource_schemes)
    # Core-owned transport parameters (``can_*``) are not descriptor business
    # options; set them aside before the driver-level option validation so a CAN
    # descriptor does not have to re-declare bitrate/fd/termination settings.
    transport_options = {
        key: value
        for key, value in (options or {}).items()
        if key.startswith("can_")
    }
    try:
        validated_options = descriptor.validate_options(
            {
                key: value
                for key, value in (options or {}).items()
                if key not in transport_options
            }
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid options for instrument driver {descriptor.driver_id!r}: {exc}") from exc

    opened_transports: list[GuardedAuditedTransport] = []
    opened_session_state: InstrumentSessionState | None = None

    def open_transport() -> InstrumentTransport:
        nonlocal opened_session_state
        if opened_transports:
            raise ConfigError(
                f"instrument driver {descriptor.driver_id!r} requested more than one transport; "
                "instrument API v2 factories may open exactly one configured transport"
            )
        lease_acquired_here = False
        if lease is not None and not lease.acquired:
            lease.acquire()
            lease_acquired_here = True
        concrete_transport: InstrumentTransport | None = None
        try:
            concrete_transport = _open_transport(
                backend=backend,
                resource=resource,
                timeout_ms=timeout_ms,
                opc_timeout_ms=opc_timeout_ms,
                read_retry_attempts=read_retry_attempts,
                read_retry_delay_ms=read_retry_delay_ms,
                logger=logger,
                serial_config=serial_config,
                # Core-owned transport parameters (``can_*``) bypass the
                # descriptor business-option validator.
                options=transport_options,
            )
            session_state = InstrumentSessionState()
            transport = GuardedAuditedTransport(
                concrete_transport,
                access=normalized_access,
                lease=lease,
                release_lease_on_close=lease_acquired_here,
                session_state=session_state,
                construction_latched=construction_latched,
            )
            opened_session_state = session_state
        except Exception:
            if concrete_transport is not None:
                try:
                    concrete_transport.close()
                except Exception:
                    pass
            if lease_acquired_here:
                lease.release()
            raise
        opened_transports.append(transport)
        return transport

    context = DriverContext(
        driver_id=descriptor.driver_id,
        kind=descriptor.kind,
        resource=resource,
        backend=backend,
        timeout_ms=timeout_ms,
        opc_timeout_ms=opc_timeout_ms,
        logger=logger,
        _transport_factory=open_transport,
        settings=settings or {},
        options=validated_options,
        access=normalized_access,
    )
    try:
        driver = descriptor.factory(context)
        validate_declared_capabilities(descriptor, driver)
        if bounded_binary_profile_opt_in:
            if len(opened_transports) != 1:
                raise ConfigError(
                    f"instrument driver {descriptor.driver_id!r} bounded binary profile "
                    "requires exactly one context transport"
                )
            _validate_bounded_binary_transport(
                descriptor=descriptor,
                transport=opened_transports[0],
            )
            opened_transports[0]._mark_bounded_binary_backend_verified()
        if construction_latched:
            for transport in opened_transports:
                transport._release_construction_latch()
    except Exception as exc:
        _close_factory_failure(driver if "driver" in locals() else None, opened_transports)
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(
            f"failed to create {expected_kind} instrument driver {descriptor.driver_id!r}: {exc}"
        ) from exc
    return OpenedInstrument(
        descriptor=descriptor,
        driver=cast(InstrumentDriver, driver),
        transport=opened_transports[0] if opened_transports else None,
        session_state=opened_session_state,
    )


def _select_backend(configured_backend: str, supported: tuple[str, ...]) -> str:
    configured = configured_backend.strip().lower()
    aliases = {"lan": "pyvisa", "visa": "pyvisa", "pyvisa": "pyvisa"}
    normalized = aliases.get(configured, configured)
    if normalized in supported:
        return normalized
    rsinstrument_backends = tuple(
        backend for backend in supported if backend in RSINSTRUMENT_BACKENDS
    )
    if configured == "lan" and rsinstrument_backends and len(rsinstrument_backends) == len(supported):
        return rsinstrument_backends[0]
    if configured in {"visa", "pyvisa"} and supported == ("rsinstrument",):
        return "rsinstrument"
    raise ConfigError(
        f"configured backend {configured_backend!r} is not supported; "
        f"driver supports: {', '.join(supported)}"
    )


def _validate_resource_scheme(resource: str, supported: tuple[str, ...]) -> None:
    if not supported:
        return
    match = re.match(r"[A-Za-z]+", resource.strip())
    configured = match.group(0).lower() if match is not None else "unknown"
    if configured in supported:
        return
    raise ConfigError(
        f"configured resource scheme {configured!r} is not supported; "
        f"driver supports: {', '.join(supported)}"
    )


def _normalize_rsinstrument_socket_resource(resource: str) -> str:
    stripped = resource.strip()
    socket_match = re.fullmatch(
        r"TCPIP\d*::([^:]+)::([0-9]+)::SOCKET",
        stripped,
        flags=re.IGNORECASE,
    )
    if socket_match is not None:
        host, raw_port = socket_match.groups()
        port = int(raw_port)
        if not 1 <= port <= 65_535:
            raise ConfigError("RsInstrument SocketIO TCP port must be between 1 and 65535")
        return f"TCPIP::{host}::{port}::SOCKET"

    instr_match = re.fullmatch(
        r"TCPIP\d*::([^:]+)::INSTR",
        stripped,
        flags=re.IGNORECASE,
    )
    if instr_match is not None:
        host = instr_match.group(1)
        return (
            f"TCPIP::{host}::{RSINSTRUMENT_SOCKET_DEFAULT_PORT}::SOCKET"
        )

    raise ConfigError(
        "RsInstrument SocketIO requires TCPIP::<host>::INSTR or "
        "TCPIP::<host>::<port>::SOCKET; use an explicit VISA-based backend "
        "for other TCPIP resource forms"
    )


@dataclass(frozen=True)
class TransportRequest:
    """打开一个传输所需的全部输入；后端构造器只依赖它。"""

    backend: str
    resource: str
    connection: ConnectionConfig
    timeout_ms: int
    opc_timeout_ms: int
    read_retry_attempts: int
    read_retry_delay_ms: int
    logger: CommandLogger
    serial_config: SerialConnectionConfig | None = None
    options: Mapping[str, object] | None = None


TransportFactory = Callable[[TransportRequest], InstrumentTransport]


def _open_serial(request: TransportRequest) -> InstrumentTransport:
    if request.serial_config is None:
        raise ConfigError("serial instrument driver requires serial configuration")
    return SerialTransport.open(
        request.serial_config,
        logger=request.logger,
        read_retry_attempts=request.read_retry_attempts,
        read_retry_delay_ms=request.read_retry_delay_ms,
    )


def _open_pyvisa(request: TransportRequest) -> InstrumentTransport:
    return PyVisaTransport.open(request.connection, logger=request.logger)


def _open_rsinstrument(request: TransportRequest) -> InstrumentTransport:
    """`rsinstrument` 后端保持原样：不传 `select_visa`（既有兼容路径）。"""
    return RsInstrumentTransport.open(request.connection, logger=request.logger)


def _open_rsinstrument_visa(request: TransportRequest) -> InstrumentTransport:
    return RsInstrumentTransport.open(
        request.connection,
        logger=request.logger,
        select_visa=_SELECT_VISA_BACKENDS.get(request.backend),
    )


def _open_can(request: TransportRequest) -> InstrumentTransport:
    """Open a CAN/CAN-FD bus through python-can.

    CAN configuration is carried by the resource string (``can://<channel>``)
    and optional CORE options declared by the descriptor.  ``python-can`` is an
    optional extra, so a missing adapter fails before instrument I/O.
    """
    options = dict(request.options or {})
    is_fd = bool(options.get("can_fd", False))
    return CanTransport.open(
        request.serial_config,
        logger=request.logger,
        resource=request.resource,
        channel=options.get("can_channel"),
        bitrate=int(options.get("can_bitrate", 500_000)),
        arbitration_id=int(options.get("can_arbitration_id", 0x100)),
        is_extended_id=bool(options.get("can_extended_id", False)),
        is_fd=is_fd,
        read_timeout_ms=int(options.get("can_read_timeout_ms", 500)),
        max_frame_bytes=int(options.get("can_max_frame_bytes", 64 if is_fd else 8)),
        options=options,
    )


_SELECT_VISA_BACKENDS = {
    "rsinstrument-socket": "socketio",
    "rsinstrument-rsvisa": "rs",
    "rsinstrument-pyvisa-py": "pyvisa-py",
}

# 后端 -> 构造器。新增总线（例如 CAN／CAN-FD）时只需在这里注册一个构造器，
# descriptor.backends 校验、资源 scheme 校验与「I/O 前失败」都复用现有路径。
TRANSPORT_FACTORIES: dict[str, TransportFactory] = {
    "serial": _open_serial,
    "pyvisa": _open_pyvisa,
    "rsinstrument": _open_rsinstrument,
    "rsinstrument-socket": _open_rsinstrument_visa,
    "rsinstrument-rsvisa": _open_rsinstrument_visa,
    "rsinstrument-pyvisa-py": _open_rsinstrument_visa,
    "can": _open_can,
}


def _open_transport(
    *,
    backend: str,
    resource: str,
    timeout_ms: int,
    opc_timeout_ms: int,
    read_retry_attempts: int,
    read_retry_delay_ms: int,
    logger: CommandLogger,
    serial_config: SerialConnectionConfig | None,
    options: Mapping[str, object] | None = None,
) -> InstrumentTransport:
    factory = TRANSPORT_FACTORIES.get(backend)
    if factory is None:
        raise ConfigError(f"unsupported instrument transport backend: {backend}")
    if backend == "rsinstrument-socket":
        resource = _normalize_rsinstrument_socket_resource(resource)
    request = TransportRequest(
        backend=backend,
        resource=resource,
        connection=ConnectionConfig(
            backend="lan",
            resource=resource,
            timeout_ms=timeout_ms,
            opc_timeout_ms=opc_timeout_ms,
            read_retry_attempts=read_retry_attempts,
            read_retry_delay_ms=read_retry_delay_ms,
        ),
        timeout_ms=timeout_ms,
        opc_timeout_ms=opc_timeout_ms,
        read_retry_attempts=read_retry_attempts,
        read_retry_delay_ms=read_retry_delay_ms,
        logger=logger,
        serial_config=serial_config,
        options=options,
    )
    return factory(request)


def _close_factory_failure(
    driver: object | None,
    opened_transports: list[InstrumentTransport],
) -> None:
    if driver is not None and callable(getattr(driver, "close", None)):
        try:
            driver.close()
        except Exception:
            pass
    for transport in reversed(opened_transports):
        try:
            transport.close()
        except Exception:
            pass


def _validate_bounded_binary_transport(
    *,
    descriptor: InstrumentDescriptor,
    transport: GuardedAuditedTransport,
) -> None:
    """Accept only core-owned backends that prove the full bounded VISA path."""

    inner = transport.inner
    raw_session: object | None = None
    if isinstance(inner, PyVisaTransport):
        raw_session = inner.session
    elif isinstance(inner, RsInstrumentTransport):
        get_session_handle = getattr(inner.session, "get_session_handle", None)
        if callable(get_session_handle):
            try:
                raw_session = get_session_handle()
            except Exception:
                raw_session = None
    if raw_session is None or not visa_binary_contract_supported(raw_session):
        raise ConfigError(
            f"instrument driver {descriptor.driver_id!r} bounded binary operation requires "
            "a bounded PyVISA or RsInstrument INSTR resource"
        )
