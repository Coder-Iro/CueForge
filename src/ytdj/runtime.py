"""Runtime helpers for packaged dependency discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    name: str
    path: Path | None
    source: str
    version: str = ""

    @property
    def available(self) -> bool:
        return self.path is not None


DEPENDENCY_COMMANDS = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
    "fpcalc": ("-version",),
    "deno": ("--version",),
}


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def bundled_bin_root(root: Path | None = None) -> Path:
    return (root or app_root()) / "bin"


def bundled_path_dirs(root: Path | None = None) -> tuple[Path, ...]:
    bin_root = bundled_bin_root(root)
    if not bin_root.exists():
        return ()
    dirs = [bin_root]
    dirs.extend(path for path in bin_root.rglob("*") if path.is_dir())
    return tuple(dict.fromkeys(path.resolve() for path in dirs))


def configure_dependency_path(root: Path | None = None) -> tuple[Path, ...]:
    dirs = bundled_path_dirs(root)
    if not dirs:
        return ()

    current = os.environ.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    normalized = {_normalize_for_path(Path(part)) for part in parts}
    prepend = [path for path in dirs if _normalize_for_path(path) not in normalized]
    if prepend:
        os.environ["PATH"] = os.pathsep.join([*(str(path) for path in prepend), *parts])
    return tuple(prepend)


def find_executable(name: str, *, explicit_path: Path | None = None, root: Path | None = None) -> DependencyStatus:
    executable = _windows_executable_name(name)
    if explicit_path:
        path = _resolve_explicit_executable(explicit_path, executable)
        if path:
            return DependencyStatus(name=name, path=path, source="settings")

    bundled = _find_bundled_executable(executable, root=root)
    if bundled:
        return DependencyStatus(name=name, path=bundled, source="bundled")

    detected = shutil.which(executable)
    if detected:
        return DependencyStatus(name=name, path=Path(detected), source="PATH")
    return DependencyStatus(name=name, path=None, source="missing")


def dependency_diagnostics(root: Path | None = None) -> list[DependencyStatus]:
    return [
        _with_version(find_executable(name, root=root), DEPENDENCY_COMMANDS[name])
        for name in DEPENDENCY_COMMANDS
    ]


def format_diagnostics(root: Path | None = None) -> str:
    lines = [
        f"python: {sys.version.split()[0]}",
        f"frozen: {bool(getattr(sys, 'frozen', False))}",
        f"app_root: {app_root() if root is None else root}",
    ]
    try:
        import PySide6

        lines.append(f"PySide6: {PySide6.__version__}")
    except Exception as exc:
        lines.append(f"PySide6: unavailable ({exc})")
    try:
        import yt_dlp

        lines.append(f"yt-dlp: {yt_dlp.version.__version__}")
    except Exception as exc:
        lines.append(f"yt-dlp: unavailable ({exc})")

    for status in dependency_diagnostics(root=root):
        path = str(status.path) if status.path else "<missing>"
        suffix = f" :: {status.version}" if status.version else ""
        lines.append(f"{status.name}: {status.source}: {path}{suffix}")
    return "\n".join(lines)


def _with_version(status: DependencyStatus, args: tuple[str, ...]) -> DependencyStatus:
    if not status.path:
        return status
    try:
        result = subprocess.run(
            [str(status.path), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:
        return DependencyStatus(status.name, status.path, status.source, f"version check failed: {exc}")
    output = (result.stdout or result.stderr).strip().splitlines()
    version = output[0].strip() if output else ""
    return DependencyStatus(status.name, status.path, status.source, version)


def _resolve_explicit_executable(path: Path, executable: str) -> Path | None:
    if path.is_dir():
        candidate = path / executable
        return candidate if candidate.exists() else None
    return path if path.exists() else None


def _find_bundled_executable(executable: str, *, root: Path | None) -> Path | None:
    bin_root = bundled_bin_root(root)
    if not bin_root.exists():
        return None
    direct = bin_root / executable
    if direct.exists():
        return direct
    matches = sorted(bin_root.rglob(executable))
    return matches[0] if matches else None


def _windows_executable_name(name: str) -> str:
    if os.name == "nt" and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def _normalize_for_path(path: Path) -> str:
    return str(path.resolve()).casefold() if os.name == "nt" else str(path.resolve())
