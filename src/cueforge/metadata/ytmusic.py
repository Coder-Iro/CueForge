"""YouTube Music metadata lookup."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from cueforge.metadata.ytmusic_auth import (
    YTMusicCookieAuthConfig,
    YTMusicCookieAuthError,
    build_ytmusic_cookie_auth,
)
from cueforge.metadata.normalize import clean_metadata, clean_title, parse_artist_title
from cueforge.models import TrackMetadata


class YTMusicLike(Protocol):
    def get_song(self, videoId: str) -> dict[str, Any]: ...

    def get_watch_playlist(self, videoId: str, limit: int = 25) -> dict[str, Any]: ...


class YouTubeMusicProvider:
    def __init__(
        self,
        *,
        auth_path: Path | None = None,
        cookie_file: Path | None = None,
        client: YTMusicLike | None = None,
        client_factory: Callable[[Any], YTMusicLike] | None = None,
        browser_auth_builder: Callable[[], dict[str, str] | None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.auth_path = auth_path
        self.cookie_file = cookie_file
        self._client = client
        self._client_factory = client_factory
        self._browser_auth_builder = browser_auth_builder
        self._log = log

    def lookup(self, url_or_video_id: str) -> TrackMetadata:
        video_id = extract_video_id(url_or_video_id)
        if not video_id:
            return TrackMetadata(source_url=url_or_video_id)

        self._emit(f"YouTube Music 조회 시작: {video_id}")
        client = self._client or self._create_client()
        self._emit("YouTube Music get_song 호출")
        song = self._safe_call(lambda: client.get_song(video_id)) or {}
        self._emit("YouTube Music watch playlist 호출")
        watch = self._safe_call(lambda: client.get_watch_playlist(videoId=video_id, limit=1)) or {}
        self._emit("YouTube Music 조회 완료")
        return clean_metadata(_metadata_from_ytmusic(video_id, song, watch, url_or_video_id))

    def _create_client(self) -> YTMusicLike:
        from ytmusicapi import YTMusic

        auth = self._resolve_auth()
        if self._client_factory:
            return self._client_factory(auth)
        if auth is None:
            return YTMusic()
        return YTMusic(auth)

    def _resolve_auth(self) -> Any:
        if self.auth_path and self.auth_path.exists():
            self._emit("YTMusic 인증: 수동 JSON 사용")
            return str(self.auth_path)

        if self.cookie_file and self.cookie_file.exists():
            builder = self._browser_auth_builder or self._build_browser_auth
            try:
                self._emit("YTMusic 인증: 쿠키 파일 읽는 중")
                auth = builder()
            except YTMusicCookieAuthError as exc:
                self._emit(f"YTMusic 쿠키 파일 인증 생략: {exc}")
                return None
            if auth:
                self._emit("YTMusic 인증: 쿠키 파일 사용")
            return auth

        self._emit("YTMusic 인증: 무인증 조회 사용")
        return None

    def _build_browser_auth(self) -> dict[str, str] | None:
        if not self.cookie_file:
            return None
        return build_ytmusic_cookie_auth(YTMusicCookieAuthConfig(cookie_file=self.cookie_file))

    def _emit(self, message: str) -> None:
        if self._log:
            self._log(message)

    @staticmethod
    def _safe_call(call: Any) -> dict[str, Any] | None:
        try:
            result = call()
        except Exception:
            return None
        return result if isinstance(result, dict) else None


def extract_video_id(url_or_video_id: str) -> str:
    value = url_or_video_id.strip()
    if not value:
        return ""
    if "://" not in value:
        return value
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0]
    if parsed.netloc.endswith("youtu.be"):
        return parsed.path.strip("/")
    return ""


def _metadata_from_ytmusic(
    video_id: str,
    song: dict[str, Any],
    watch: dict[str, Any],
    source_url: str,
) -> TrackMetadata:
    details = song.get("videoDetails") or {}
    microformat = (song.get("microformat") or {}).get("microformatDataRenderer") or {}
    track = _first_track(watch)
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    artists = track.get("artists") if isinstance(track.get("artists"), list) else []
    thumbnails = (
        track.get("thumbnails")
        or details.get("thumbnail", {}).get("thumbnails")
        or microformat.get("thumbnail", {}).get("thumbnails")
        or []
    )

    title = str(track.get("title") or details.get("title") or microformat.get("title") or "")
    parsed_artist, parsed_title = parse_artist_title(title)
    if not parsed_artist and parsed_title and parsed_title != clean_title(title):
        title = parsed_title
    artist = _join_names(artists) or str(details.get("author") or microformat.get("ownerChannelName") or "")
    cover_url = _largest_thumbnail(thumbnails)
    return TrackMetadata(
        title=title,
        artist=artist,
        album=str(album.get("name") or ""),
        album_artist=artist or str(details.get("author") or ""),
        release_date=str(microformat.get("publishDate") or ""),
        cover_url=cover_url,
        cover_source="YouTube Music thumbnail" if cover_url else "",
        source_url=source_url if "://" in source_url else f"https://music.youtube.com/watch?v={video_id}",
        comments=f"https://music.youtube.com/watch?v={video_id}",
    )


def _first_track(watch: dict[str, Any]) -> dict[str, Any]:
    tracks = watch.get("tracks")
    if isinstance(tracks, list) and tracks:
        first = tracks[0]
        return first if isinstance(first, dict) else {}
    return {}


def _join_names(items: list[Any]) -> str:
    names = []
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return ", ".join(names)


def _largest_thumbnail(thumbnails: list[Any]) -> str:
    valid = [item for item in thumbnails if isinstance(item, dict) and item.get("url")]
    if not valid:
        return ""
    best = max(valid, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
    return str(best["url"])
