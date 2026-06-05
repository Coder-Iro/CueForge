"""Audio fingerprint lookup through Chromaprint/fpcalc and AcoustID."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ytdj.metadata.normalize import clean_metadata
from ytdj.models import MetadataCandidate, TrackMetadata
from ytdj.runtime import find_executable


class FingerprintError(Exception):
    """Raised when fingerprint extraction or lookup fails."""


class FingerprintUnavailable(FingerprintError):
    """Raised when fingerprint recognition is not configured or available."""


class HTTPSessionLike(Protocol):
    headers: dict[str, str]

    def post(self, url: str, *, data: dict[str, str], timeout: int) -> Any: ...


class FingerprinterLike(Protocol):
    def fingerprint(self, audio_path: Path) -> "AudioFingerprint": ...


@dataclass(slots=True)
class AudioFingerprint:
    duration_seconds: int
    fingerprint: str


@dataclass(slots=True)
class AcoustIDConfig:
    client_key: str = ""
    fpcalc_path: Path | None = None
    app_name: str = "YT-DJ"
    app_version: str = "0.1.0"
    rate_limit_seconds: float = 0.34
    timeout_seconds: int = 15

    @property
    def user_agent(self) -> str:
        return f"{self.app_name}/{self.app_version}"


class FpcalcFingerprinter:
    def __init__(
        self,
        fpcalc_path: Path | None = None,
        *,
        runner: Any = subprocess.run,
    ) -> None:
        self.fpcalc_path = fpcalc_path
        self._runner = runner

    def fingerprint(self, audio_path: Path) -> AudioFingerprint:
        executable = self._executable()
        try:
            result = self._runner(
                [executable, "-json", str(audio_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise FingerprintUnavailable("fpcalc executable was not found") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "fpcalc failed").strip()
            raise FingerprintError(message)

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FingerprintError("fpcalc returned invalid JSON") from exc

        fingerprint = str(payload.get("fingerprint") or "").strip()
        duration = _to_int(payload.get("duration"))
        if not fingerprint or not duration:
            raise FingerprintError("fpcalc did not return duration and fingerprint")
        return AudioFingerprint(duration_seconds=duration, fingerprint=fingerprint)

    def _executable(self) -> str:
        detected = find_executable("fpcalc", explicit_path=self.fpcalc_path)
        if not detected.path:
            raise FingerprintUnavailable("fpcalc is not on PATH")
        return str(detected.path)


class AcoustIDProvider:
    API_ROOT = "https://api.acoustid.org/v2"

    def __init__(
        self,
        config: AcoustIDConfig | None = None,
        *,
        session: HTTPSessionLike | None = None,
        fingerprinter: FingerprinterLike | None = None,
        sleeper: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.config = config or AcoustIDConfig()
        self.session = session or self._create_session()
        self.session.headers.update({"User-Agent": self.config.user_agent})
        self.fingerprinter = fingerprinter or FpcalcFingerprinter(self.config.fpcalc_path)
        self._sleeper = sleeper
        self._clock = clock
        self._last_request_at = 0.0

    def lookup(self, audio_path: Path) -> list[MetadataCandidate]:
        if not self.config.client_key.strip():
            raise FingerprintUnavailable("AcoustID client key is not configured")
        fingerprint = self.fingerprinter.fingerprint(Path(audio_path))
        payload = self._lookup_fingerprint(fingerprint)
        candidates = [
            candidate
            for result in payload.get("results", [])
            if isinstance(result, dict)
            for candidate in self._candidates_from_result(result)
        ]
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    def _lookup_fingerprint(self, fingerprint: AudioFingerprint) -> dict[str, Any]:
        now = self._clock()
        wait = self.config.rate_limit_seconds - (now - self._last_request_at)
        if wait > 0:
            self._sleeper(wait)
        response = self.session.post(
            f"{self.API_ROOT}/lookup",
            data={
                "client": self.config.client_key.strip(),
                "duration": str(fingerprint.duration_seconds),
                "fingerprint": fingerprint.fingerprint,
                "meta": "recordings releases releaseids tracks compress",
                "format": "json",
            },
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        self._last_request_at = self._clock()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _candidates_from_result(self, result: dict[str, Any]) -> list[MetadataCandidate]:
        recordings = result.get("recordings") or []
        raw_score = _to_float(result.get("score"))
        candidates: list[MetadataCandidate] = []
        for recording in recordings:
            if not isinstance(recording, dict):
                continue
            metadata = _metadata_from_recording(recording, result)
            if not metadata.is_minimum_viable():
                continue
            candidates.append(
                MetadataCandidate(
                    provider="acoustid",
                    metadata=metadata,
                    score=_candidate_score(raw_score),
                    matched_fields=tuple(
                        field
                        for field, value in (
                            ("fingerprint", True),
                            ("title", metadata.title),
                            ("artist", metadata.artist),
                            ("album", metadata.album),
                            ("release_year", metadata.release_date),
                        )
                        if value
                    ),
                    raw={"acoustid_score": raw_score, "result": result, "recording": recording},
                )
            )
        return candidates

    @staticmethod
    def _create_session() -> HTTPSessionLike:
        import requests

        return requests.Session()


def _metadata_from_recording(recording: dict[str, Any], result: dict[str, Any]) -> TrackMetadata:
    release = _best_release(recording.get("releases") or [])
    recording_id = str(recording.get("id") or "")
    release_id = str(release.get("id") or "")
    artist = _artist_names(recording.get("artists") or recording.get("artist-credit") or [])
    album_artist = _artist_names(release.get("artists") or release.get("artist-credit") or []) or artist
    return clean_metadata(
        TrackMetadata(
            title=str(recording.get("title") or ""),
            artist=artist,
            album=str(release.get("title") or ""),
            album_artist=album_artist,
            release_date=str(release.get("date") or ""),
            source_url=_source_url(recording_id, str(result.get("id") or "")),
            musicbrainz_recording_id=recording_id,
            musicbrainz_release_id=release_id,
        )
    )


def _source_url(recording_id: str, acoustid_id: str) -> str:
    if recording_id:
        return f"https://musicbrainz.org/recording/{recording_id}"
    if acoustid_id:
        return f"https://acoustid.org/track/{acoustid_id}"
    return ""


def _candidate_score(raw_score: float) -> float:
    if raw_score >= 0.90:
        return round(max(raw_score, 0.85), 3)
    if raw_score >= 0.75:
        return round(min(raw_score, 0.84), 3)
    return round(min(raw_score, 0.64), 3)


def _best_release(releases: list[Any]) -> dict[str, Any]:
    valid = [release for release in releases if isinstance(release, dict)]
    if not valid:
        return {}
    official = [release for release in valid if release.get("status") == "Official"]
    return sorted(official or valid, key=lambda release: str(release.get("date") or "9999"))[0]


def _artist_names(artists: list[Any]) -> str:
    names: list[str] = []
    for item in artists:
        if isinstance(item, dict):
            artist = item.get("artist") if isinstance(item.get("artist"), dict) else {}
            name = item.get("name") or artist.get("name")
            if name:
                names.append(str(name))
    return ", ".join(names)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int | None:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None
