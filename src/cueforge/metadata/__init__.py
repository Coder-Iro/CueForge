"""Metadata lookup, normalization, and matching utilities."""

from cueforge.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
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
from cueforge.metadata.ytmusic_auth import (
    YTMusicCookieAuthConfig,
    YTMusicCookieAuthError,
    YTMusicOAuthClient,
    YTMusicOAuthError,
    build_ytmusic_cookie_auth,
    build_ytmusic_oauth_credentials,
    default_ytmusic_oauth_token_path,
    find_ytmusic_oauth_client_file,
    load_ytmusic_oauth_client,
    run_ytmusic_oauth_desktop_flow,
    write_ytmusic_oauth_token,
)
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata

__all__ = [
    "MetadataCandidate",
    "AcoustIDConfig",
    "AcoustIDProvider",
    "AudioFingerprint",
    "CoverArtConfig",
    "CoverArtProvider",
    "FpcalcFingerprinter",
    "MusicBrainzConfig",
    "MetadataHint",
    "MusicBrainzProvider",
    "MetadataResolution",
    "MetadataResolver",
    "ReviewState",
    "TrackMetadata",
    "YouTubeMusicProvider",
    "YTMusicCookieAuthConfig",
    "YTMusicCookieAuthError",
    "YTMusicOAuthClient",
    "YTMusicOAuthError",
    "as_reference_candidate",
    "build_safe_fallback",
    "build_hint_candidates",
    "build_ytmusic_cookie_auth",
    "build_ytmusic_oauth_credentials",
    "clean_metadata",
    "default_ytmusic_oauth_token_path",
    "find_ytmusic_oauth_client_file",
    "build_soundcloud_metadata",
    "build_soundcloud_native_candidate",
    "extract_metadata_hints",
    "load_ytmusic_oauth_client",
    "merge_metadata",
    "parse_artist_title",
    "run_ytmusic_oauth_desktop_flow",
    "write_ytmusic_oauth_token",
]
