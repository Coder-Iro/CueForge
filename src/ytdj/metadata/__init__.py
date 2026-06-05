"""Metadata lookup, normalization, and matching utilities."""

from ytdj.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
from ytdj.metadata.musicbrainz import MusicBrainzConfig, MusicBrainzProvider
from ytdj.metadata.ytmusic import YouTubeMusicProvider
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata

__all__ = [
    "MetadataCandidate",
    "MusicBrainzConfig",
    "MusicBrainzProvider",
    "ReviewState",
    "TrackMetadata",
    "YouTubeMusicProvider",
    "build_safe_fallback",
    "clean_metadata",
    "merge_metadata",
    "parse_artist_title",
]
