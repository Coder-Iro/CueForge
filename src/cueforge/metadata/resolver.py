"""Platform-aware metadata resolution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from cueforge.metadata.bpm import GetSongBpmConfig, GetSongBpmProvider
from cueforge.metadata.cover_art import CoverArtProvider
from cueforge.metadata.hints import build_hint_candidates
from cueforge.metadata.normalize import build_safe_fallback, merge_metadata
from cueforge.metadata.soundcloud import as_reference_candidate, build_soundcloud_native_candidate
from cueforge.metadata.ytmusic import YouTubeMusicProvider
from cueforge.metadata.musicbrainz import MusicBrainzProvider
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata
from cueforge.sources import SourcePlatform, detect_source_platform, trust_policy_for


@dataclass(slots=True)
class MetadataResolution:
    metadata: TrackMetadata
    state: ReviewState
    candidates: list[MetadataCandidate]
    platform: SourcePlatform


YTMusicProviderFactory = Callable[..., Any]
MusicBrainzProviderFactory = Callable[[], Any]
CoverArtProviderFactory = Callable[[], Any]
BpmProviderFactory = Callable[[GetSongBpmConfig], Any]


class MetadataResolver:
    def __init__(
        self,
        *,
        ytmusic_provider_factory: YTMusicProviderFactory | None = None,
        musicbrainz_provider_factory: MusicBrainzProviderFactory | None = None,
        cover_art_provider_factory: CoverArtProviderFactory | None = None,
        bpm_config: GetSongBpmConfig | None = None,
        bpm_provider_factory: BpmProviderFactory | None = None,
    ) -> None:
        self._ytmusic_provider_factory = ytmusic_provider_factory or YouTubeMusicProvider
        self._musicbrainz_provider_factory = musicbrainz_provider_factory or MusicBrainzProvider
        self._cover_art_provider_factory = cover_art_provider_factory or CoverArtProvider
        self._bpm_config = bpm_config or GetSongBpmConfig()
        self._bpm_provider_factory = bpm_provider_factory or GetSongBpmProvider

    def resolve(
        self,
        *,
        url: str,
        info: dict[str, Any],
        ytmusic_auth_path: Path | None = None,
        ytmusic_cookie_browser: str | None = None,
        unlock_browser_cookie_database: bool = False,
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
                auth_path=ytmusic_auth_path,
                cookie_browser=ytmusic_cookie_browser,
                unlock_browser_cookie_database=unlock_browser_cookie_database,
                log=log,
            ).lookup(url)
            if youtube.title or youtube.artist:
                _log(log, f"YouTube Music 메타데이터 수신: {youtube.artist} - {youtube.title}")
            else:
                _log(log, "YouTube Music 메타데이터 비어 있음")
        reference = youtube.with_defaults_from(fallback).normalized()
        hint_candidates = build_hint_candidates(info)
        candidates = self._enriched_hint_candidates(hint_candidates, info, log=log)
        reference_candidates = self._musicbrainz_candidates(reference, info, log=log)
        if hint_candidates:
            reference_candidates = [as_reference_candidate(candidate) for candidate in reference_candidates]
        candidates.extend(reference_candidates)
        metadata, state = merge_metadata(youtube=reference, candidates=candidates, fallback=fallback)
        metadata = self.enrich_cover_art(metadata, platform=platform, fallback_cover_url=reference.cover_url or fallback.cover_url, log=log)
        metadata, bpm_candidates = self.enrich_bpm(metadata, info=info, platform=platform, log=log)
        candidates.extend(bpm_candidates)
        return MetadataResolution(metadata=metadata, state=state, candidates=candidates, platform=platform)

    def _new_ytmusic_provider(
        self,
        *,
        auth_path: Path | None,
        cookie_browser: str | None,
        unlock_browser_cookie_database: bool,
        log: Callable[[str], None] | None,
    ) -> Any:
        try:
            return self._ytmusic_provider_factory(
                auth_path=auth_path,
                cookie_browser=cookie_browser,
                unlock_browser_cookie_database=unlock_browser_cookie_database,
                log=log,
            )
        except TypeError:
            return self._ytmusic_provider_factory(auth_path)

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
        metadata, bpm_candidates = self.enrich_bpm(metadata, info=info, platform=SourcePlatform.SOUNDCLOUD, log=log)
        state = ReviewState.AUTO_APPROVED if metadata.is_minimum_viable() else ReviewState.REVIEW_REQUIRED
        reference_candidates = [
            as_reference_candidate(candidate)
            for candidate in self._musicbrainz_candidates(metadata, info, log=log)
        ]
        return MetadataResolution(
            metadata=metadata,
            state=state,
            candidates=[native_candidate, *reference_candidates, *bpm_candidates],
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

        if metadata.musicbrainz_release_id:
            try:
                cover_url = self._cover_art_provider_factory().lookup(metadata.musicbrainz_release_id)
            except Exception as exc:
                _log(log, f"cover art lookup skipped: {exc}")
            else:
                if cover_url:
                    _log(log, "cover art: Cover Art Archive 500px")
                    return replace(metadata, cover_url=cover_url, cover_source="Cover Art Archive")

        fallback = fallback_cover_url or metadata.cover_url
        if fallback:
            _log(log, "cover art fallback: platform thumbnail")
            return replace(metadata, cover_url=fallback, cover_source=metadata.cover_source or "platform thumbnail")

        _log(log, "cover art unavailable")
        return metadata

    def enrich_bpm(
        self,
        metadata: TrackMetadata,
        *,
        info: dict[str, Any],
        platform: SourcePlatform,
        log: Callable[[str], None] | None = None,
    ) -> tuple[TrackMetadata, list[MetadataCandidate]]:
        try:
            candidates = self._bpm_provider_factory(self._bpm_config).lookup(
                metadata,
                info=info,
                platform=platform,
                duration_ms=_duration_ms(info),
            )
        except Exception as exc:
            _log(log, f"GetSongBPM lookup skipped: {exc}")
            candidates = []

        if metadata.bpm:
            _log(log, f"bpm resolved: {metadata.bpm} from {metadata.bpm_source or 'metadata'}")
            return metadata, candidates

        best = max(candidates, key=lambda candidate: candidate.score, default=None)
        if best and best.metadata.bpm and best.score >= 0.85:
            enriched = metadata.overlay(
                TrackMetadata(
                    bpm=best.metadata.bpm,
                    bpm_source=best.metadata.bpm_source,
                    bpm_confidence=best.metadata.bpm_confidence,
                )
            ).normalized()
            _log(log, f"bpm resolved: {enriched.bpm} from {enriched.bpm_source}")
            return enriched, candidates

        if self._bpm_config.client_key.strip():
            _log(log, "bpm skipped: no strict external match")
        else:
            _log(log, "bpm skipped: no native BPM and GetSongBPM API key missing")
        return metadata, candidates

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

    def _enriched_hint_candidates(
        self,
        hints: list[MetadataCandidate],
        info: dict[str, Any],
        *,
        log: Callable[[str], None] | None,
    ) -> list[MetadataCandidate]:
        if not hints:
            return []
        candidates: list[MetadataCandidate] = []
        for hint in hints:
            enriched = self._musicbrainz_candidates(hint.metadata, info, log=log)
            if enriched:
                for candidate in enriched:
                    metadata = candidate.metadata.with_defaults_from(hint.metadata).normalized()
                    candidates.append(
                        MetadataCandidate(
                            provider=f"{candidate.provider}_from_{hint.provider}",
                            metadata=metadata,
                            score=candidate.score,
                            matched_fields=tuple(dict.fromkeys((*hint.matched_fields, *candidate.matched_fields))),
                            raw={
                                **candidate.raw,
                                "hint": hint.raw,
                                "hint_metadata": hint.metadata,
                            },
                        )
                    )
            else:
                candidates.append(hint)
        return candidates


def _duration_ms(info: dict[str, Any]) -> int | None:
    duration = info.get("duration")
    try:
        return int(float(duration) * 1000)
    except (TypeError, ValueError):
        return None


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)
