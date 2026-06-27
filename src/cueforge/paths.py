"""Application path defaults."""

from __future__ import annotations

from pathlib import Path


def default_output_dir() -> Path:
    """Return the default user-facing music output folder."""

    try:
        from platformdirs import user_downloads_dir

        downloads = Path(user_downloads_dir())
    except Exception:
        downloads = Path.home() / "Downloads"
    return downloads / "CueForge"


def legacy_cwd_output_dir(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / "downloads"
