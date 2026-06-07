"""Metadata lookup, normalization, and matching utilities."""

from cueforge.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
from cueforge.metadata.bpm import GetSongBpmConfig, GetSongBpmProvider
from cueforge.metadata.cover_art import CoverArtConfig, CoverArtProvider
from cueforge.metadata.hints import MetadataHint, build_hint_candidates, extract_metadata_hints
from cueforge.metadata.fingerprint import AcoustIDConfig, AcoustIDProvider, AudioFingerprint, FpcalcFingerprinter
from cueforge.metadata.musicbrainz import MusicBrainzConfig, MusicBrainzProvider
from cueforge.metadata.resolver import MetadataResolution, MetadataResolver
from cueforge.metadata.soundcloud import (
    as_reference_candidate,
    build_soundcloud_metadata,
    build_soundcloud_native_candidate,
)
from cueforge.metadata.ytmusic import YouTubeMusicProvider
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata

__all__ = [
    "MetadataCandidate",
    "AcoustIDConfig",
    "AcoustIDProvider",
    "AudioFingerprint",
    "CoverArtConfig",
    "CoverArtProvider",
    "FpcalcFingerprinter",
    "GetSongBpmConfig",
    "GetSongBpmProvider",
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
