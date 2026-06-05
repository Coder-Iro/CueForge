"""yt-dlp based download pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class CookieBrowser(str, Enum):
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"


@dataclass(slots=True)
class DownloadConfig:
    output_dir: Path
    ffmpeg_location: Path | None = None
    cookie_browser: CookieBrowser | None = None
    audio_bitrate_kbps: int = 320
    keep_original: bool = False
    allow_remote_js_components: bool = True
    quiet: bool = True


@dataclass(slots=True)
class DownloadProgress:
    status: str
    percent: float | None = None
    speed: float | None = None
    eta: float | None = None
    filename: Path | None = None


@dataclass(slots=True)
class DownloadResult:
    path: Path
    info: dict[str, Any]


class YoutubeDLLike(Protocol):
    def __enter__(self) -> "YoutubeDLLike": ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]: ...

    def prepare_filename(self, info: dict[str, Any]) -> str: ...


YDLFactory = Callable[[dict[str, Any]], YoutubeDLLike]
ProgressCallback = Callable[[DownloadProgress], None]


class YTDLPDownloader:
    def __init__(
        self,
        config: DownloadConfig,
        *,
        ydl_factory: YDLFactory | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.config = config
        self._ydl_factory = ydl_factory or self._default_ydl_factory
        self._progress_callback = progress_callback

    def fetch_info(self, url: str) -> dict[str, Any]:
        with self._ydl_factory(self._base_options()) as ydl:
            return ydl.extract_info(url, download=False)

    def download_audio(self, url: str) -> DownloadResult:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        options = self._base_options()
        options.update(
            {
                "format": "bestaudio/best",
                "outtmpl": {"default": str(self.config.output_dir / "%(id)s.%(ext)s")},
                "keepvideo": self.config.keep_original,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": str(self.config.audio_bitrate_kbps),
                    }
                ],
                "progress_hooks": [self._progress_hook],
            }
        )

        with self._ydl_factory(options) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = self._resolve_output_path(ydl, info)
            return DownloadResult(path=final_path, info=info)

    def _base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": self.config.quiet,
            "no_warnings": self.config.quiet,
            "windowsfilenames": True,
            "noplaylist": False,
        }
        if self.config.ffmpeg_location:
            options["ffmpeg_location"] = str(self.config.ffmpeg_location)
        if self.config.cookie_browser:
            options["cookiesfrombrowser"] = (self.config.cookie_browser.value,)
        if self.config.allow_remote_js_components:
            options["remote_components"] = ["ejs:github"]
        return options

    def _progress_hook(self, payload: dict[str, Any]) -> None:
        if not self._progress_callback:
            return
        total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
        downloaded = payload.get("downloaded_bytes")
        percent = None
        if total and downloaded is not None:
            percent = max(0.0, min(100.0, downloaded / total * 100))
        filename = payload.get("filename")
        self._progress_callback(
            DownloadProgress(
                status=str(payload.get("status") or ""),
                percent=percent,
                speed=payload.get("speed"),
                eta=payload.get("eta"),
                filename=Path(filename) if filename else None,
            )
        )

    def _resolve_output_path(self, ydl: YoutubeDLLike, info: dict[str, Any]) -> Path:
        requested_downloads = info.get("requested_downloads") or []
        for item in requested_downloads:
            filepath = item.get("filepath") or item.get("_filename")
            if filepath:
                path = Path(filepath)
                if path.suffix.lower() == ".mp3" or path.exists():
                    return path

        prepared = Path(ydl.prepare_filename(info))
        return prepared.with_suffix(".mp3")

    @staticmethod
    def _default_ydl_factory(options: dict[str, Any]) -> YoutubeDLLike:
        from yt_dlp import YoutubeDL

        return YoutubeDL(options)
