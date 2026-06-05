"""Metadata lookup, normalization, and matching utilities."""

from ytdj.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
from ytdj.metadata.musicbrainz import MusicBrainzConfig, MusicBrainzProvider
from ytdj.metadata.resolver import MetadataResolution, MetadataResolver
from ytdj.metadata.soundcloud import (
    as_reference_candidate,
    build_soundcloud_metadata,
    build_soundcloud_native_candidate,
)
from ytdj.metadata.ytmusic import YouTubeMusicProvider
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata

__all__ = [
    "MetadataCandidate",
    "MusicBrainzConfig",
    "MusicBrainzProvider",
    "MetadataResolution",
    "MetadataResolver",
    "ReviewState",
    "TrackMetadata",
    "YouTubeMusicProvider",
    "as_reference_candidate",
    "build_safe_fallback",
    "clean_metadata",
    "build_soundcloud_metadata",
    "build_soundcloud_native_candidate",
    "merge_metadata",
    "parse_artist_title",
]
