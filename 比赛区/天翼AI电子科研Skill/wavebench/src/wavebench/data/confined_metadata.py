"""Read MCP capture metadata without following a redirected package member."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from wavebench.errors import ConfigError


def _windows_handle_path(fd: int) -> Path:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = kernel32.GetFinalPathNameByHandleW
    get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_path.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = get_path(handle, None, 0, 0)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = get_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise OSError("cannot determine capture metadata handle path")
    name = buffer.value
    if name.startswith("\\\\?\\UNC\\"):
        name = "\\\\" + name[8:]
    elif name.startswith("\\\\?\\"):
        name = name[4:]
    return Path(name)


def _open_posix_metadata(path: Path) -> int:
    # Walk from the filesystem root using pinned directory descriptors. Resolving
    # a pathname and then opening it would leave parent-directory replacement races.
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError("confined capture metadata reads are unsupported on this platform")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory_fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
        )
    finally:
        os.close(directory_fd)


def read_confined_metadata(package_dir: Path, allowed_root: Path) -> str:
    """Reject linked metadata and verify the opened object before reading bytes.

    POSIX uses a no-follow descriptor walk; Windows checks the final opened handle
    path. Only MCP opts into this policy; local capture loaders retain their API.
    Hard links and concurrent edits to an already admitted file are not isolated.
    """
    message = "capture metadata must be an ordinary file inside the allowed capture package"
    fd = None
    try:
        if ".." in package_dir.parts or ".." in allowed_root.parts:
            raise ConfigError(message)
        # These paths are already canonicalized by the MCP directory gate. Do not
        # resolve them again, which could silently adopt a replacement link target.
        package_dir = package_dir.absolute()
        allowed_root = allowed_root.absolute()
        if not package_dir.is_relative_to(allowed_root):
            raise ConfigError(message)
        metadata_path = package_dir / "metadata.json"
        entry = metadata_path.lstat()
        if not stat.S_ISREG(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400:
            raise ConfigError(message)
        if os.name == "nt":
            fd = os.open(metadata_path, os.O_RDONLY | os.O_BINARY)
            if _windows_handle_path(fd) != metadata_path:
                raise ConfigError(message)
        else:
            fd = _open_posix_metadata(metadata_path)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfigError(message)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None  # The stream now owns the descriptor, including on read failure.
            return stream.read()
    except (OSError, RuntimeError, UnicodeError) as exc:
        # Avoid reflecting a link target or its contents through an MCP error.
        raise ConfigError(message) from exc
    finally:
        if fd is not None:
            os.close(fd)
