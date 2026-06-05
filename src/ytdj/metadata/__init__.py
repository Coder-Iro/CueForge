"""Metadata lookup, normalization, and matching utilities."""

from ytdj.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata

__all__ = [
    "MetadataCandidate",
    "ReviewState",
    "TrackMetadata",
    "build_safe_fallback",
    "clean_metadata",
    "merge_metadata",
    "parse_artist_title",
]

