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
from cueforge.metadata.semantic import SemanticCandidateRanker, SemanticRankerConfig, prepare_semantic_model, semantic_model_cached
from cueforge.metadata.soundcloud import (
    as_reference_candidate,
    build_soundcloud_metadata,
    build_soundcloud_native_candidate,
)
from cueforge.metadata.ytmusic import YouTubeMusicProvider
from cueforge.metadata.ytmusic_auth import (
    YTMusicCookieAuthConfig,
    YTMusicCookieAuthError,
    YTMusicOAuthAccount,
    YTMusicOAuthClient,
    YTMusicOAuthError,
    build_ytmusic_cookie_auth,
    build_ytmusic_oauth_credentials,
    default_ytmusic_oauth_account_path,
    default_ytmusic_oauth_token_path,
    fetch_ytmusic_oauth_account,
    find_ytmusic_oauth_client_file,
    google_oauth_account_label,
    load_ytmusic_oauth_client,
    read_ytmusic_oauth_account,
    read_ytmusic_oauth_token,
    refresh_ytmusic_oauth_token_if_needed,
    run_ytmusic_oauth_desktop_flow,
    write_ytmusic_oauth_account,
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
    "SemanticCandidateRanker",
    "SemanticRankerConfig",
    "prepare_semantic_model",
    "semantic_model_cached",
    "TrackMetadata",
    "YouTubeMusicProvider",
    "YTMusicCookieAuthConfig",
    "YTMusicCookieAuthError",
    "YTMusicOAuthAccount",
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
    "fetch_ytmusic_oauth_account",
    "load_ytmusic_oauth_client",
    "merge_metadata",
    "parse_artist_title",
    "google_oauth_account_label",
    "default_ytmusic_oauth_account_path",
    "read_ytmusic_oauth_account",
    "read_ytmusic_oauth_token",
    "refresh_ytmusic_oauth_token_if_needed",
    "run_ytmusic_oauth_desktop_flow",
    "write_ytmusic_oauth_account",
    "write_ytmusic_oauth_token",
]
