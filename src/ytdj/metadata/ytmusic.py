"""YouTube Music metadata lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from ytdj.metadata.normalize import clean_metadata
from ytdj.models import TrackMetadata


class YTMusicLike(Protocol):
    def get_song(self, videoId: str) -> dict[str, Any]: ...

    def get_watch_playlist(self, videoId: str, limit: int = 25) -> dict[str, Any]: ...


class YouTubeMusicProvider:
    def __init__(
        self,
        *,
        auth_path: Path | None = None,
        client: YTMusicLike | None = None,
    ) -> None:
        self.auth_path = auth_path
        self._client = client

    def lookup(self, url_or_video_id: str) -> TrackMetadata:
        video_id = extract_video_id(url_or_video_id)
        if not video_id:
            return TrackMetadata(source_url=url_or_video_id)

        client = self._client or self._create_client()
        song = self._safe_call(lambda: client.get_song(video_id)) or {}
        watch = self._safe_call(lambda: client.get_watch_playlist(videoId=video_id, limit=1)) or {}
        return clean_metadata(_metadata_from_ytmusic(video_id, song, watch, url_or_video_id))

    def _create_client(self) -> YTMusicLike:
        from ytmusicapi import YTMusic

        if self.auth_path and self.auth_path.exists():
            return YTMusic(str(self.auth_path))
        return YTMusic()

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

    return TrackMetadata(
        title=str(track.get("title") or details.get("title") or microformat.get("title") or ""),
        artist=_join_names(artists) or str(details.get("author") or microformat.get("ownerChannelName") or ""),
        album=str(album.get("name") or ""),
        album_artist=_join_names(artists) or str(details.get("author") or ""),
        release_date=str(microformat.get("publishDate") or ""),
        cover_url=_largest_thumbnail(thumbnails),
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

