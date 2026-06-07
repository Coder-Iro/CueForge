"""Opt-in Chromium cookie database unlock support for yt-dlp."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

_PATCHED = False
_ENABLED = False
_ORIGINAL_OPEN_DATABASE_COPY: Callable[..., Any] | None = None


def set_chromium_cookie_unlock_enabled(enabled: bool) -> bool:
    """Enable or disable the yt-dlp cookie DB unlock patch for this process."""

    global _ENABLED
    _ENABLED = bool(enabled)
    if not _ENABLED:
        return _PATCHED
    return _install_patch()


def chromium_cookie_unlock_supported() -> bool:
    return os.name == "nt"


def _install_patch() -> bool:
    global _ORIGINAL_OPEN_DATABASE_COPY, _PATCHED
    if _PATCHED:
        return True
    if not chromium_cookie_unlock_supported():
        return False

    import yt_dlp.cookies

    original = yt_dlp.cookies._open_database_copy

    def open_database_copy(database_path: str, tmpdir: str) -> Any:
        try:
            return original(database_path, tmpdir)
        except PermissionError:
            if not _ENABLED:
                raise
            unlock_chromium_cookie_database(Path(database_path))
            return original(database_path, tmpdir)

    _ORIGINAL_OPEN_DATABASE_COPY = original
    yt_dlp.cookies._open_database_copy = open_database_copy
    _PATCHED = True
    return True


def unlock_chromium_cookie_database(database_path: Path) -> None:
    """Ask Windows Restart Manager to release locks on a Chromium cookie DB."""

    if not chromium_cookie_unlock_supported():
        raise RuntimeError("Chromium cookie unlock is only supported on Windows.")

    from ctypes import WINFUNCTYPE, byref, create_unicode_buffer, windll
    from ctypes.wintypes import DWORD, LPCWSTR, UINT

    error_success = 0
    error_more_data = 234
    rm_force_shutdown = 1

    @WINFUNCTYPE(None, UINT)
    def callback(percent_complete: UINT) -> None:
        return None

    rstrtmgr = windll.LoadLibrary("Rstrtmgr")
    session_handle = DWORD(0)
    session_key = create_unicode_buffer(256)
    result = DWORD(rstrtmgr.RmStartSession(byref(session_handle), 0, session_key)).value
    if result != error_success:
        raise RuntimeError(f"RmStartSession returned non-zero result: {result}")

    try:
        filenames = (LPCWSTR * 1)(str(database_path))
        result = DWORD(rstrtmgr.RmRegisterResources(session_handle, 1, filenames, 0, None, 0, None)).value
        if result != error_success:
            raise RuntimeError(f"RmRegisterResources returned non-zero result: {result}")

        proc_info_needed = DWORD(0)
        proc_info = DWORD(0)
        reboot_reasons = DWORD(0)
        result = DWORD(
            rstrtmgr.RmGetList(session_handle, byref(proc_info_needed), byref(proc_info), None, byref(reboot_reasons))
        ).value
        if result not in (error_success, error_more_data):
            raise RuntimeError(f"RmGetList returned non-successful result: {result}")
        if not proc_info_needed.value:
            return

        result = DWORD(rstrtmgr.RmShutdown(session_handle, rm_force_shutdown, callback)).value
        if result != error_success:
            raise RuntimeError(f"RmShutdown returned non-successful result: {result}")
    finally:
        result = DWORD(rstrtmgr.RmEndSession(session_handle)).value
        if result != error_success:
            raise RuntimeError(f"RmEndSession returned non-zero result: {result}")
