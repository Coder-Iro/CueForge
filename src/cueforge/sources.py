"""Source platform detection and metadata trust policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse


class SourcePlatform(str, Enum):
    YOUTUBE = "youtube"
    YOUTUBE_MUSIC = "youtube_music"
    SOUNDCLOUD = "soundcloud"
    UNKNOWN = "unknown"

    @property
    def display_name(self) -> str:
        return {
            SourcePlatform.YOUTUBE: "YouTube",
            SourcePlatform.YOUTUBE_MUSIC: "YouTube Music",
            SourcePlatform.SOUNDCLOUD: "SoundCloud",
            SourcePlatform.UNKNOWN: "Unknown",
        }[self]


@dataclass(frozen=True, slots=True)
class SourceTrustPolicy:
    platform: SourcePlatform
    trust_native_metadata: bool
    use_youtube_music: bool
    allow_external_auto_approve: bool
    note: str


def detect_source_platform(url: str = "", info: dict[str, Any] | None = None) -> SourcePlatform:
    extractor = str((info or {}).get("extractor_key") or (info or {}).get("extractor") or "").casefold()
    if "soundcloud" in extractor:
        return SourcePlatform.SOUNDCLOUD
    if "youtube" in extractor:
        return SourcePlatform.YOUTUBE_MUSIC if _is_youtube_music_url(url) else SourcePlatform.YOUTUBE

    host = urlparse(url).netloc.casefold()
    if "soundcloud.com" in host:
        return SourcePlatform.SOUNDCLOUD
    if "music.youtube.com" in host:
        return SourcePlatform.YOUTUBE_MUSIC
    if "youtube.com" in host or "youtu.be" in host:
        return SourcePlatform.YOUTUBE
    return SourcePlatform.UNKNOWN


def trust_policy_for(platform: SourcePlatform) -> SourceTrustPolicy:
    if platform == SourcePlatform.SOUNDCLOUD:
        return SourceTrustPolicy(
            platform=platform,
            trust_native_metadata=True,
            use_youtube_music=False,
            allow_external_auto_approve=False,
            note="SoundCloud native metadata is trusted for remix, bootleg, edit, and mashup tracks.",
        )
    if platform in {SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC}:
        return SourceTrustPolicy(
            platform=platform,
            trust_native_metadata=False,
            use_youtube_music=True,
            allow_external_auto_approve=True,
            note="YouTube metadata is treated as a fallback and enriched with music providers.",
        )
    return SourceTrustPolicy(
        platform=platform,
        trust_native_metadata=False,
        use_youtube_music=False,
        allow_external_auto_approve=False,
        note="Unknown sources require review before tagging.",
    )


def _is_youtube_music_url(url: str) -> bool:
    return "music.youtube.com" in urlparse(url).netloc.casefold()

