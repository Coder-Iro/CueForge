"""Platform-aware metadata resolution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from cueforge.metadata.hints import build_hint_candidates
from cueforge.metadata.normalize import (
    build_safe_fallback,
    merge_metadata,
    prefer_creator_artist_over_official_metadata,
)
from cueforge.metadata.soundcloud import build_soundcloud_native_candidate
from cueforge.metadata.ytmusic import YouTubeMusicProvider
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata
from cueforge.sources import SourcePlatform, detect_source_platform, trust_policy_for


@dataclass(slots=True)
class MetadataResolution:
    metadata: TrackMetadata
    state: ReviewState
    candidates: list[MetadataCandidate]
    platform: SourcePlatform


YTMusicProviderFactory = Callable[..., Any]
GenerativeSuggesterFactory = Callable[[], Any]


class MetadataResolver:
    def __init__(
        self,
        *,
        ytmusic_provider_factory: YTMusicProviderFactory | None = None,
        generative_suggester_factory: GenerativeSuggesterFactory | None = None,
    ) -> None:
        self._ytmusic_provider_factory = ytmusic_provider_factory or YouTubeMusicProvider
        self._generative_suggester_factory = generative_suggester_factory

    def resolve(
        self,
        *,
        url: str,
        info: dict[str, Any],
        ytmusic_oauth_client_file: Path | None = None,
        ytmusic_oauth_token_file: Path | None = None,
        log: Callable[[str], None] | None = None,
    ) -> MetadataResolution:
        platform = detect_source_platform(url, info)
        policy = trust_policy_for(platform)
        fallback = build_safe_fallback(info, url)

        if policy.trust_native_metadata and platform == SourcePlatform.SOUNDCLOUD:
            return self._resolve_soundcloud(url=url, info=info, fallback=fallback, log=log)

        youtube = TrackMetadata()
        if policy.use_youtube_music:
            _log(log, "YouTube Music 메타데이터 조회 준비")
            youtube = self._new_ytmusic_provider(
                oauth_client_file=ytmusic_oauth_client_file,
                oauth_token_file=ytmusic_oauth_token_file,
                log=log,
            ).lookup(url)
            youtube = prefer_creator_artist_over_official_metadata(youtube, info)
            if youtube.title or youtube.artist:
                _log(log, f"YouTube Music 메타데이터 수신: {youtube.artist} - {youtube.title}")
            else:
                _log(log, "YouTube Music 메타데이터 비어 있음")
        reference = youtube.with_defaults_from(fallback).normalized()
        candidates = build_hint_candidates(info)
        candidates.extend(self._generative_suggestions(reference, info, candidates, log=log))
        metadata, state = merge_metadata(youtube=reference, candidates=candidates, fallback=fallback)
        metadata = self.enrich_cover_art(metadata, platform=platform, fallback_cover_url=reference.cover_url or fallback.cover_url, log=log)
        return MetadataResolution(metadata=metadata, state=state, candidates=candidates, platform=platform)

    def _new_ytmusic_provider(
        self,
        *,
        oauth_client_file: Path | None,
        oauth_token_file: Path | None,
        log: Callable[[str], None] | None,
    ) -> Any:
        try:
            return self._ytmusic_provider_factory(
                oauth_client_file=oauth_client_file,
                oauth_token_file=oauth_token_file,
                log=log,
            )
        except TypeError:
            return self._ytmusic_provider_factory()

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
        metadata = self.enrich_cover_art(metadata, platform=SourcePlatform.SOUNDCLOUD, fallback_cover_url=fallback.cover_url, log=log)
        state = ReviewState.AUTO_APPROVED if metadata.is_minimum_viable() else ReviewState.REVIEW_REQUIRED
        return MetadataResolution(
            metadata=metadata,
            state=state,
            candidates=[native_candidate],
            platform=SourcePlatform.SOUNDCLOUD,
        )

    def enrich_cover_art(
        self,
        metadata: TrackMetadata,
        *,
        platform: SourcePlatform,
        fallback_cover_url: str = "",
        log: Callable[[str], None] | None = None,
    ) -> TrackMetadata:
        if platform == SourcePlatform.SOUNDCLOUD and metadata.cover_url:
            _log(log, "cover art: SoundCloud native artwork")
            return replace(metadata, cover_source=metadata.cover_source or "SoundCloud native")

        if metadata.cover_url and metadata.cover_source != "platform thumbnail":
            _log(log, "cover art: metadata artwork")
            return replace(metadata, cover_source=metadata.cover_source or "metadata artwork")

        fallback = fallback_cover_url or metadata.cover_url
        if fallback:
            _log(log, "cover art fallback: platform thumbnail")
            return replace(metadata, cover_url=fallback, cover_source=metadata.cover_source or "platform thumbnail")

        _log(log, "cover art unavailable")
        return metadata

    def _generative_suggestions(
        self,
        reference: TrackMetadata,
        info: dict[str, Any],
        candidates: list[MetadataCandidate],
        *,
        log: Callable[[str], None] | None,
    ) -> list[MetadataCandidate]:
        if not self._generative_suggester_factory:
            return []
        suggester = self._generative_suggester_factory()
        return suggester.suggest(info=info, reference=reference, candidates=candidates, log=log)


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)
