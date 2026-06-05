"""Platform-aware metadata resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ytdj.metadata.normalize import build_safe_fallback, merge_metadata
from ytdj.metadata.soundcloud import as_reference_candidate, build_soundcloud_native_candidate
from ytdj.metadata.ytmusic import YouTubeMusicProvider
from ytdj.metadata.musicbrainz import MusicBrainzProvider
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata
from ytdj.sources import SourcePlatform, detect_source_platform, trust_policy_for


@dataclass(slots=True)
class MetadataResolution:
    metadata: TrackMetadata
    state: ReviewState
    candidates: list[MetadataCandidate]
    platform: SourcePlatform


YTMusicProviderFactory = Callable[[Path | None], Any]
MusicBrainzProviderFactory = Callable[[], Any]


class MetadataResolver:
    def __init__(
        self,
        *,
        ytmusic_provider_factory: YTMusicProviderFactory | None = None,
        musicbrainz_provider_factory: MusicBrainzProviderFactory | None = None,
    ) -> None:
        self._ytmusic_provider_factory = ytmusic_provider_factory or (lambda auth_path: YouTubeMusicProvider(auth_path=auth_path))
        self._musicbrainz_provider_factory = musicbrainz_provider_factory or MusicBrainzProvider

    def resolve(
        self,
        *,
        url: str,
        info: dict[str, Any],
        ytmusic_auth_path: Path | None = None,
        log: Callable[[str], None] | None = None,
    ) -> MetadataResolution:
        platform = detect_source_platform(url, info)
        policy = trust_policy_for(platform)
        fallback = build_safe_fallback(info, url)

        if policy.trust_native_metadata and platform == SourcePlatform.SOUNDCLOUD:
            return self._resolve_soundcloud(url=url, info=info, fallback=fallback, log=log)

        youtube = TrackMetadata()
        if policy.use_youtube_music:
            youtube = self._ytmusic_provider_factory(ytmusic_auth_path).lookup(url)
        reference = youtube.with_defaults_from(fallback).normalized()
        candidates = self._musicbrainz_candidates(reference, info, log=log)
        metadata, state = merge_metadata(youtube=reference, candidates=candidates, fallback=fallback)
        return MetadataResolution(metadata=metadata, state=state, candidates=candidates, platform=platform)

    def _resolve_soundcloud(
        self,
        *,
        url: str,
        info: dict[str, Any],
        fallback: TrackMetadata,
        log: Callable[[str], None] | None,
    ) -> MetadataResolution:
        native_candidate = build_soundcloud_native_candidate(info, url)
        metadata = native_candidate.metadata.with_defaults_from(fallback)
        state = ReviewState.AUTO_APPROVED if metadata.is_minimum_viable() else ReviewState.REVIEW_REQUIRED
        reference_candidates = [
            as_reference_candidate(candidate)
            for candidate in self._musicbrainz_candidates(metadata, info, log=log)
        ]
        return MetadataResolution(
            metadata=metadata,
            state=state,
            candidates=[native_candidate, *reference_candidates],
            platform=SourcePlatform.SOUNDCLOUD,
        )

    def _musicbrainz_candidates(
        self,
        reference: TrackMetadata,
        info: dict[str, Any],
        *,
        log: Callable[[str], None] | None,
    ) -> list[MetadataCandidate]:
        try:
            return self._musicbrainz_provider_factory().lookup(reference, duration_ms=_duration_ms(info))
        except Exception as exc:
            if log:
                log(f"MusicBrainz lookup skipped: {exc}")
            return []


def _duration_ms(info: dict[str, Any]) -> int | None:
    duration = info.get("duration")
    try:
        return int(float(duration) * 1000)
    except (TypeError, ValueError):
        return None

