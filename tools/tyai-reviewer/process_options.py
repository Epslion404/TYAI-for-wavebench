"""Keep reviewer-owned command-line processes silent on Windows."""
import os
import subprocess
import threading

_desktop_handle = None
_desktop_lock = threading.Lock()
DESKTOP_NAME = "TYAI-AutoReview"


def process_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def background_desktop() -> str:
    """Keep nested OpenCode tool consoles off the user's visible desktop too."""
    import ctypes
    from ctypes import wintypes

    global _desktop_handle
    with _desktop_lock:
        if _desktop_handle is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            create = user32.CreateDesktopW
            create.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            create.restype = wintypes.HANDLE
            # Read/write objects, create windows, enumerate. Never switch desktops.
            _desktop_handle = create(DESKTOP_NAME, None, None, 0, 0x00C3, None)
            if not _desktop_handle:
                raise ctypes.WinError(ctypes.get_last_error())
    # Retain the desktop handle until process exit, including concurrent launches.
    return DESKTOP_NAME


def background_process_options() -> dict:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}
