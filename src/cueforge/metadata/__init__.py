"""Metadata lookup, normalization, and matching utilities."""

from cueforge.metadata.normalize import (
    build_safe_fallback,
    clean_metadata,
    merge_metadata,
    parse_artist_title,
)
from cueforge.metadata.hints import MetadataHint, build_hint_candidates, extract_metadata_hints
from cueforge.metadata.openai_parser import DEFAULT_OPENAI_MODEL, OpenAIMetadataConfig, OpenAIMetadataSuggester
from cueforge.metadata.openai_oauth import (
    OpenAICodexOAuthError,
    default_openai_codex_oauth_token_path,
    fetch_openai_codex_models,
    fetch_openai_codex_usage,
    format_openai_codex_usage,
    openai_codex_model_ids,
    openai_codex_oauth_account_label,
    read_openai_codex_oauth_token,
    run_openai_codex_oauth_desktop_flow,
    write_openai_codex_oauth_token,
)
from cueforge.metadata.resolver import MetadataResolution, MetadataResolver
from cueforge.metadata.soundcloud import (
    build_soundcloud_metadata,
    build_soundcloud_native_candidate,
)
from cueforge.metadata.ytmusic import YouTubeMusicProvider
from cueforge.metadata.ytmusic_auth import (
    YTMusicOAuthAccount,
    YTMusicOAuthClient,
    YTMusicOAuthError,
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
    "MetadataHint",
    "MetadataResolution",
    "MetadataResolver",
    "DEFAULT_OPENAI_MODEL",
    "OpenAIMetadataConfig",
    "OpenAIMetadataSuggester",
    "OpenAICodexOAuthError",
    "ReviewState",
    "TrackMetadata",
    "YouTubeMusicProvider",
    "YTMusicOAuthAccount",
    "YTMusicOAuthClient",
    "YTMusicOAuthError",
    "build_safe_fallback",
    "build_hint_candidates",
    "build_ytmusic_oauth_credentials",
    "clean_metadata",
    "default_ytmusic_oauth_token_path",
    "default_openai_codex_oauth_token_path",
    "fetch_openai_codex_models",
    "fetch_openai_codex_usage",
    "format_openai_codex_usage",
    "openai_codex_model_ids",
    "find_ytmusic_oauth_client_file",
    "build_soundcloud_metadata",
    "build_soundcloud_native_candidate",
    "extract_metadata_hints",
    "fetch_ytmusic_oauth_account",
    "load_ytmusic_oauth_client",
    "merge_metadata",
    "parse_artist_title",
    "google_oauth_account_label",
    "openai_codex_oauth_account_label",
    "default_ytmusic_oauth_account_path",
    "read_ytmusic_oauth_account",
    "read_ytmusic_oauth_token",
    "read_openai_codex_oauth_token",
    "refresh_ytmusic_oauth_token_if_needed",
    "run_openai_codex_oauth_desktop_flow",
    "run_ytmusic_oauth_desktop_flow",
    "write_openai_codex_oauth_token",
    "write_ytmusic_oauth_account",
    "write_ytmusic_oauth_token",
]
