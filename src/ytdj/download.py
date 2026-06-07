"""yt-dlp based download pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from ytdj.chrome_cookie_unlock import set_chromium_cookie_unlock_enabled


class CookieBrowser(str, Enum):
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"


@dataclass(slots=True)
class DownloadConfig:
    output_dir: Path
    ffmpeg_location: Path | None = None
    cookie_browser: CookieBrowser | str | None = None
    allow_playlists: bool = False
    audio_bitrate_kbps: int = 320
    keep_original: bool = False
    allow_remote_js_components: bool = True
    unlock_browser_cookie_database: bool = False
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
        options = self._base_options()
        try:
            with self._ydl_factory(options) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as exc:
            if not _should_retry_without_browser_cookies(exc, options):
                raise
            with self._ydl_factory(_without_browser_cookies(options)) as ydl:
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

        try:
            return self._download_with_options(url, options)
        except Exception as exc:
            if not _should_retry_without_browser_cookies(exc, options):
                raise
            return self._download_with_options(url, _without_browser_cookies(options))

    def _download_with_options(self, url: str, options: dict[str, Any]) -> DownloadResult:
        with self._ydl_factory(options) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = self._resolve_output_path(ydl, info)
            return DownloadResult(path=final_path, info=info)

    def _base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": self.config.quiet,
            "no_warnings": self.config.quiet,
            "windowsfilenames": True,
            "noplaylist": not self.config.allow_playlists,
        }
        if self.config.ffmpeg_location:
            options["ffmpeg_location"] = str(self.config.ffmpeg_location)
        cookie_browser = _cookie_browser_value(self.config.cookie_browser)
        set_chromium_cookie_unlock_enabled(
            bool(cookie_browser and self.config.unlock_browser_cookie_database and _is_chromium_cookie_browser(cookie_browser))
        )
        if cookie_browser:
            options["cookiesfrombrowser"] = (cookie_browser,)
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


def _cookie_browser_value(cookie_browser: CookieBrowser | str | None) -> str:
    if isinstance(cookie_browser, CookieBrowser):
        return cookie_browser.value
    if isinstance(cookie_browser, str):
        return cookie_browser.strip()
    return ""


def _is_chromium_cookie_browser(cookie_browser: str) -> bool:
    return cookie_browser.casefold() in {"chrome", "edge", "chromium", "brave", "vivaldi", "opera"}


def _should_retry_without_browser_cookies(exc: Exception, options: dict[str, Any]) -> bool:
    if not options.get("cookiesfrombrowser"):
        return False
    message = str(exc).casefold()
    return "could not copy" in message and "cookie" in message and "database" in message


def _without_browser_cookies(options: dict[str, Any]) -> dict[str, Any]:
    fallback = dict(options)
    fallback.pop("cookiesfrombrowser", None)
    return fallback
