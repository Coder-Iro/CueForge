"""MusicBrainz metadata lookup with cache and rate limiting."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from platformdirs import user_cache_path

from cueforge.metadata.matching import score_candidate
from cueforge.metadata.normalize import clean_metadata
from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.rate_limit import ProviderRateLimiter, global_rate_limiter


class HTTPSessionLike(Protocol):
    headers: dict[str, str]

    def get(self, url: str, *, params: dict[str, str], timeout: int) -> Any: ...


@dataclass(slots=True)
class MusicBrainzConfig:
    app_name: str = "CueForge"
    app_version: str = "0.1.0"
    contact: str = ""
    cache_path: Path | None = None
    rate_limit_seconds: float = 1.0
    timeout_seconds: int = 15

    @property
    def user_agent(self) -> str:
        suffix = f" ({self.contact})" if self.contact else ""
        return f"{self.app_name}/{self.app_version}{suffix}"


class MusicBrainzProvider:
    API_ROOT = "https://musicbrainz.org/ws/2"

    def __init__(
        self,
        config: MusicBrainzConfig | None = None,
        *,
        session: HTTPSessionLike | None = None,
        sleeper: Any = time.sleep,
        clock: Any = time.monotonic,
        rate_limiter: ProviderRateLimiter | None = None,
    ) -> None:
        self.config = config or MusicBrainzConfig()
        self.session = session or self._create_session()
        self.session.headers.update({"User-Agent": self.config.user_agent})
        self.cache = _JsonCache(self.config.cache_path or user_cache_path("CueForge") / "musicbrainz.sqlite")
        self._sleeper = sleeper
        self._clock = clock
        self._rate_limiter = rate_limiter or global_rate_limiter("musicbrainz")

    def lookup(self, reference: TrackMetadata, *, duration_ms: int | None = None) -> list[MetadataCandidate]:
        if not reference.title:
            return []
        params = {
            "query": _recording_query(reference),
            "fmt": "json",
            "limit": "5",
        }
        payload = self._get_json("recording", params)
        candidates = [
            self._candidate_from_recording(reference, item, duration_ms=duration_ms)
            for item in payload.get("recordings", [])
            if isinstance(item, dict)
        ]
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    def _get_json(self, resource: str, params: dict[str, str]) -> dict[str, Any]:
        cache_key = f"{resource}:{json.dumps(params, sort_keys=True)}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        self._rate_limiter.wait(self.config.rate_limit_seconds, sleeper=self._sleeper, clock=self._clock)
        response = self.session.get(
            f"{self.API_ROOT}/{resource}",
            params=params,
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            payload = {}
        self.cache.set(cache_key, payload)
        return payload

    def _candidate_from_recording(
        self,
        reference: TrackMetadata,
        recording: dict[str, Any],
        *,
        duration_ms: int | None,
    ) -> MetadataCandidate:
        release = _best_release(recording.get("releases") or [])
        candidate = clean_metadata(
            TrackMetadata(
                title=str(recording.get("title") or ""),
                artist=_artist_credit(recording.get("artist-credit") or []),
                album=str(release.get("title") or ""),
                album_artist=_artist_credit(release.get("artist-credit") or []) or _artist_credit(recording.get("artist-credit") or []),
                genre=_genre(recording),
                release_date=str(release.get("date") or ""),
                isrc=_first(recording.get("isrcs") or []),
                source_url="https://musicbrainz.org/recording/" + str(recording.get("id") or ""),
                musicbrainz_recording_id=str(recording.get("id") or ""),
                musicbrainz_release_id=str(release.get("id") or ""),
            )
        )
        provider_score = _to_float(recording.get("score")) / 100.0
        return score_candidate(
            reference,
            candidate,
            provider_score=provider_score,
            reference_duration_ms=duration_ms,
            candidate_duration_ms=_to_int(recording.get("length")),
            provider="musicbrainz",
            raw=recording,
        )

    @staticmethod
    def _create_session() -> HTTPSessionLike:
        import requests

        return requests.Session()


class _JsonCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, payload, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(payload), time.time()),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def _recording_query(metadata: TrackMetadata) -> str:
    parts = [f'recording:"{_escape(metadata.title)}"']
    if metadata.artist:
        parts.append(f'artist:"{_escape(metadata.artist)}"')
    if metadata.album:
        parts.append(f'release:"{_escape(metadata.album)}"')
    return " AND ".join(parts)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _best_release(releases: list[Any]) -> dict[str, Any]:
    valid = [release for release in releases if isinstance(release, dict)]
    if not valid:
        return {}
    official = [release for release in valid if release.get("status") == "Official"]
    return sorted(official or valid, key=lambda release: str(release.get("date") or "9999"))[0]


def _artist_credit(credits: list[Any]) -> str:
    names = []
    for credit in credits:
        if isinstance(credit, dict):
            artist = credit.get("artist") if isinstance(credit.get("artist"), dict) else {}
            name = credit.get("name") or artist.get("name")
            if name:
                names.append(str(name))
    return ", ".join(names)


def _genre(recording: dict[str, Any]) -> str:
    genres = recording.get("genres") or recording.get("tags") or []
    valid = [item for item in genres if isinstance(item, dict) and item.get("name")]
    if not valid:
        return ""
    best = sorted(valid, key=lambda item: int(item.get("count") or 0), reverse=True)[0]
    return str(best["name"])


def _first(items: list[Any]) -> str:
    return str(items[0]) if items else ""


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
