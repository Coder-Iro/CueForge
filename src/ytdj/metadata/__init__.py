"""Metadata lookup, normalization, and matching utilities."""

from ytdj.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
from ytdj.metadata.hints import MetadataHint, build_hint_candidates, extract_metadata_hints
from ytdj.metadata.fingerprint import AcoustIDConfig, AcoustIDProvider, AudioFingerprint, FpcalcFingerprinter
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
    "AcoustIDConfig",
    "AcoustIDProvider",
    "AudioFingerprint",
    "FpcalcFingerprinter",
    "MusicBrainzConfig",
    "MetadataHint",
    "MusicBrainzProvider",
    "MetadataResolution",
    "MetadataResolver",
    "ReviewState",
    "TrackMetadata",
    "YouTubeMusicProvider",
    "as_reference_candidate",
    "build_safe_fallback",
    "build_hint_candidates",
    "clean_metadata",
    "build_soundcloud_metadata",
    "build_soundcloud_native_candidate",
    "extract_metadata_hints",
    "merge_metadata",
    "parse_artist_title",
]
