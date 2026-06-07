"""External BPM lookup helpers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from cueforge.metadata.matching import score_candidate
from cueforge.metadata.normalize import clean_metadata, squash_spaces
from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.sources import SourcePlatform

GETSONGBPM_ENDPOINT = "https://api.getsong.co/search/"


@dataclass(frozen=True, slots=True)
class GetSongBpmConfig:
    client_key: str = ""
    endpoint: str = GETSONGBPM_ENDPOINT
    timeout_seconds: int = 15
    user_agent: str = "CueForge/0.1.0"


class GetSongBpmProvider:
    def __init__(self, config: GetSongBpmConfig | None = None, *, session: Any | None = None) -> None:
        self.config = config or GetSongBpmConfig()
        self._session = session or _requests_session()
        headers = getattr(self._session, "headers", None)
        if headers is not None:
            headers.update({"User-Agent": self.config.user_agent})
            if self.config.client_key:
                headers.update({"X-API-KEY": self.config.client_key})

    def lookup(
        self,
        reference: TrackMetadata,
        *,
        info: dict[str, Any],
        platform: SourcePlatform,
        duration_ms: int | None = None,
    ) -> list[MetadataCandidate]:
        native = native_bpm_candidate(info, platform)
        if native:
            return [native]
        if not self.config.client_key.strip() or not reference.title.strip() or not reference.artist.strip():
            return []

        response = self._session.get(
            self.config.endpoint,
            params={
                "type": "both",
                "lookup": f"song:{reference.title} artist:{reference.artist}",
                "limit": "5",
            },
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = [
            candidate
            for item in _search_items(payload)
            if (candidate := _candidate_from_item(reference, item, duration_ms=duration_ms)) is not None
        ]
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def native_bpm_candidate(info: dict[str, Any], platform: SourcePlatform) -> MetadataCandidate | None:
    for key, value in _native_bpm_fields(info):
        bpm = parse_bpm(value)
        if bpm:
            source = f"native:{platform.value if platform != SourcePlatform.UNKNOWN else 'source'}"
            return MetadataCandidate(
                provider="bpm_native",
                metadata=TrackMetadata(bpm=bpm, bpm_source=source, bpm_confidence=1.0).normalized(),
                score=1.0,
                matched_fields=("bpm", "native_metadata"),
                raw={"field": key, "value": value, "source": source},
            )
    return None


def parse_bpm(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if not match:
            return None
        value = match.group(0)
    try:
        bpm = math.floor(float(value) + 0.5)
    except (TypeError, ValueError):
        return None
    return bpm if bpm > 0 else None


def _candidate_from_item(reference: TrackMetadata, item: dict[str, Any], *, duration_ms: int | None) -> MetadataCandidate | None:
    bpm = parse_bpm(_first_value(item, "bpm", "tempo", "song_bpm", "track.bpm", "track.tempo", "song.bpm", "song.tempo"))
    if not bpm:
        return None
    metadata = clean_metadata(
        TrackMetadata(
            title=_text_value(item, "song_title", "title", "track.title", "song.title", "name"),
            artist=_text_value(item, "artist", "artist_name", "artist.name", "artists", "performer", "song.artist"),
            album=_text_value(item, "album", "album_title", "album.name", "release", "release.title"),
            release_date=_text_value(item, "year", "release_year", "date", "release_date", "album.year", "release.date"),
            bpm=bpm,
            bpm_source="GetSongBPM",
        )
    )
    scored = score_candidate(
        reference,
        metadata,
        provider_score=1.0,
        reference_duration_ms=duration_ms,
        candidate_duration_ms=_duration_ms(item),
        provider="getsongbpm",
        raw=item,
    )
    metadata = clean_metadata(
        TrackMetadata(
            title=metadata.title,
            artist=metadata.artist,
            album=metadata.album,
            release_date=metadata.release_date,
            bpm=bpm,
            bpm_source="GetSongBPM",
            bpm_confidence=scored.score,
        )
    )
    return MetadataCandidate(
        provider="getsongbpm",
        metadata=metadata,
        score=scored.score,
        matched_fields=scored.matched_fields,
        raw=item,
    )


def _native_bpm_fields(info: dict[str, Any]) -> list[tuple[str, Any]]:
    fields = [("bpm", info.get("bpm")), ("tempo", info.get("tempo"))]
    track = info.get("track")
    if isinstance(track, dict):
        fields.extend([("track.bpm", track.get("bpm")), ("track.tempo", track.get("tempo"))])
    return fields


def _search_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("search", "songs", "results", "data", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _search_items(value)
            if nested:
                return nested
    return []


def _duration_ms(item: dict[str, Any]) -> int | None:
    value = _first_value(item, "duration_ms", "length_ms", "duration", "length", "song.duration", "track.duration")
    if value in (None, ""):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return int(duration if duration > 1000 else duration * 1000)


def _text_value(item: dict[str, Any], *paths: str) -> str:
    for path in paths:
        value = _path_value(item, path)
        text = _field_text(value)
        if text:
            return squash_spaces(text)
    return ""


def _first_value(item: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _path_value(item, path)
        if value not in (None, ""):
            return value
    return None


def _path_value(item: dict[str, Any], path: str) -> Any:
    value: Any = item
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _field_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "title", "text", "artist_name"):
            text = _field_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list | tuple):
        parts = [_field_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    return str(value)


def _requests_session() -> Any:
    import requests

    return requests.Session()
