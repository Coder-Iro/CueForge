"""yt-dlp based download pipeline."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlparse

from cueforge.rate_limit import global_rate_limiter


@dataclass(slots=True)
class DownloadConfig:
    output_dir: Path
    ffmpeg_location: Path | None = None
    cookie_file: Path | None = None
    allow_playlists: bool = False
    audio_bitrate_kbps: int = 0
    keep_original: bool = False
    allow_remote_js_components: bool = True
    youtube_request_interval_seconds: float = 0.0
    youtube_preferred_lang: str = "ko"
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


@dataclass(slots=True)
class PlaylistExpansionResult:
    urls: list[str]
    skipped_count: int = 0
    expected_count: int | None = None


class DownloadCanceled(RuntimeError):
    """Raised when the active download job is canceled by the user."""


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
        self._wait_for_youtube_request(url)
        with self._ydl_factory(options) as ydl:
            return ydl.extract_info(url, download=False)

    def expand_playlist(self, url: str) -> PlaylistExpansionResult:
        options = self._base_options()
        options.update(
            {
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "noplaylist": False,
                "playlist_items": None,
                "playlistend": None,
                "playliststart": 1,
                "skip_download": True,
            }
        )
        result = self._expand_playlist_with_options(url, options)
        if _is_incomplete_playlist_result(result) and _is_youtube_playlist_url(url):
            retry_options = dict(options)
            _merge_extractor_args(retry_options, {"youtubetab": {"skip": ["webpage"]}})
            retry_result = self._expand_playlist_with_options(url, retry_options)
            if len(retry_result.urls) > len(result.urls):
                return retry_result
        return result

    def _expand_playlist_with_options(self, url: str, options: dict[str, Any]) -> PlaylistExpansionResult:
        self._wait_for_youtube_request(url)
        with self._ydl_factory(options) as ydl:
            info = ydl.extract_info(url, download=False)
        return _playlist_expansion_result(info, source_url=url)

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

        return self._download_with_options(url, options)

    def _download_with_options(self, url: str, options: dict[str, Any]) -> DownloadResult:
        self._wait_for_youtube_request(url)
        with self._ydl_factory(options) as ydl:
            info = ydl.extract_info(url, download=True)
            final_path = self._resolve_output_path(ydl, info)
            return DownloadResult(path=final_path, info=info)

    def _wait_for_youtube_request(self, url: str) -> None:
        if self.config.youtube_request_interval_seconds <= 0 or not _is_youtube_url(url):
            return
        global_rate_limiter("yt-dlp-youtube").wait(self.config.youtube_request_interval_seconds)

    def _base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": self.config.quiet,
            "no_warnings": self.config.quiet,
            "windowsfilenames": True,
            "noplaylist": not self.config.allow_playlists,
        }
        if self.config.ffmpeg_location:
            options["ffmpeg_location"] = str(self.config.ffmpeg_location)
        if self.config.cookie_file:
            options["cookiefile"] = str(self.config.cookie_file)
        if self.config.allow_remote_js_components:
            options["remote_components"] = ["ejs:github"]
        youtube_lang = self.config.youtube_preferred_lang.strip()
        if youtube_lang:
            _merge_extractor_args(options, {"youtube": {"lang": [youtube_lang]}})
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


def _playlist_expansion_result(info: dict[str, Any] | None, *, source_url: str) -> PlaylistExpansionResult:
    if not isinstance(info, dict):
        return PlaylistExpansionResult(urls=[], skipped_count=1)
    entries = info.get("entries")
    if not entries:
        return PlaylistExpansionResult(urls=[])

    urls: list[str] = []
    skipped_count = 0
    for entry in entries:
        url = _playlist_entry_url(entry, source_url=source_url)
        if url:
            urls.append(url)
        else:
            skipped_count += 1
    return PlaylistExpansionResult(urls=urls, skipped_count=skipped_count, expected_count=_playlist_expected_count(info))


def _playlist_entry_url(entry: Any, *, source_url: str) -> str:
    if not isinstance(entry, dict):
        return ""

    for key in ("webpage_url", "original_url", "url"):
        value = str(entry.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value

    video_id = str(entry.get("id") or "").strip()
    ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").casefold()
    if video_id and ("youtube" in ie_key or _is_youtube_playlist_url(source_url)):
        return _youtube_watch_url(video_id, source_url=source_url)
    return ""


def _youtube_watch_url(video_id: str, *, source_url: str) -> str:
    host = "music.youtube.com" if urlparse(source_url).netloc.casefold() == "music.youtube.com" else "www.youtube.com"
    return f"https://{host}/watch?v={quote(video_id, safe='')}"


def _is_youtube_playlist_url(url: str) -> bool:
    return _is_youtube_url(url)


def _is_youtube_url(url: str) -> bool:
    host = urlparse(url).netloc.casefold()
    return "youtube.com" in host or "youtu.be" in host


def _playlist_expected_count(info: dict[str, Any]) -> int | None:
    for key in ("playlist_count", "n_entries"):
        value = info.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _is_incomplete_playlist_result(result: PlaylistExpansionResult) -> bool:
    if result.expected_count is None:
        return False
    return len(result.urls) + result.skipped_count < result.expected_count


def _merge_extractor_args(options: dict[str, Any], extra: dict[str, dict[str, list[str]]]) -> None:
    extractor_args = {
        extractor: {key: list(values) for key, values in args.items()}
        for extractor, args in (options.get("extractor_args") or {}).items()
    }
    for extractor, args in extra.items():
        existing_args = extractor_args.setdefault(extractor, {})
        for key, values in args.items():
            existing_args[key] = list(values)
    options["extractor_args"] = extractor_args
