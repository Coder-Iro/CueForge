"""Metadata cleanup and merge policy."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata

NOISE_PATTERNS = [
    re.compile(r"\s*\[(?:official\s*)?(?:music\s*)?video\]\s*", re.IGNORECASE),
    re.compile(r"\s*\((?:official\s*)?(?:music\s*)?video\)\s*", re.IGNORECASE),
    re.compile(r"\s*\[(?:official\s*)?audio\]\s*", re.IGNORECASE),
    re.compile(r"\s*\((?:official\s*)?audio\)\s*", re.IGNORECASE),
    re.compile(r"\s*\[(?:lyrics?|lyric video)\]\s*", re.IGNORECASE),
    re.compile(r"\s*\((?:lyrics?|lyric video)\)\s*", re.IGNORECASE),
    re.compile(r"\s*\b(?:HD|HQ|4K)\b\s*$", re.IGNORECASE),
]


def squash_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_title(value: str) -> str:
    title = value or ""
    for pattern in NOISE_PATTERNS:
        title = pattern.sub(" ", title)
    return squash_spaces(title)


def clean_artist(value: str) -> str:
    return squash_spaces(value)


def parse_artist_title(value: str) -> tuple[str, str]:
    cleaned = clean_title(value)
    for separator in (" - ", " – ", " — "):
        if separator in cleaned:
            artist, title = cleaned.split(separator, 1)
            return clean_artist(artist), clean_title(title)
    return "", cleaned


def _clean_date(value: str) -> str:
    value = squash_spaces(value)
    match = re.match(r"^(\d{4})(?:[-./]?(\d{2}))?(?:[-./]?(\d{2}))?", value)
    if not match:
        return value
    parts = [part for part in match.groups() if part]
    return "-".join(parts)


def clean_metadata(metadata: TrackMetadata) -> TrackMetadata:
    return TrackMetadata(
        title=clean_title(metadata.title),
        artist=clean_artist(metadata.artist),
        album=squash_spaces(metadata.album),
        album_artist=squash_spaces(metadata.album_artist),
        genre=squash_spaces(metadata.genre),
        release_date=_clean_date(metadata.release_date),
        track_number=metadata.track_number,
        disc_number=metadata.disc_number,
        label=squash_spaces(metadata.label),
        isrc=squash_spaces(metadata.isrc).upper(),
        cover_url=squash_spaces(metadata.cover_url),
        source_url=squash_spaces(metadata.source_url),
        musicbrainz_recording_id=squash_spaces(metadata.musicbrainz_recording_id),
        musicbrainz_release_id=squash_spaces(metadata.musicbrainz_release_id),
        comments=squash_spaces(metadata.comments),
    )


def build_safe_fallback(info: dict[str, Any], source_url: str = "") -> TrackMetadata:
    title = str(info.get("track") or info.get("title") or "")
    artist = str(
        info.get("artist")
        or _first(info.get("artists"))
        or info.get("creator")
        or _first(info.get("creators"))
        or info.get("uploader")
        or info.get("uploader_id")
        or ""
    )
    parsed_artist, parsed_title = parse_artist_title(title)
    return clean_metadata(
        TrackMetadata(
            title=parsed_title or title,
            artist=artist or parsed_artist,
            album=str(info.get("album") or info.get("series") or ""),
            album_artist=str(info.get("album_artist") or _first(info.get("album_artists")) or ""),
            genre=str(info.get("genre") or _first(info.get("genres")) or _first(info.get("categories")) or ""),
            release_date=str(info.get("release_date") or info.get("upload_date") or ""),
            track_number=_to_int(info.get("track_number")),
            disc_number=_to_int(info.get("disc_number")),
            cover_url=str(info.get("thumbnail") or ""),
            source_url=source_url or str(info.get("webpage_url") or ""),
            comments=str(info.get("webpage_url") or source_url or ""),
        )
    )


def merge_metadata(
    *,
    user: TrackMetadata | None = None,
    youtube: TrackMetadata | None = None,
    candidates: Iterable[MetadataCandidate] = (),
    fallback: TrackMetadata | None = None,
) -> tuple[TrackMetadata, ReviewState]:
    resolved = fallback or TrackMetadata()
    if youtube:
        resolved = resolved.overlay(youtube.normalized())

    best_candidate = max(candidates, key=lambda candidate: candidate.score, default=None)
    state = ReviewState.MANUAL_REQUIRED
    if best_candidate:
        resolved = resolved.overlay(best_candidate.metadata.normalized())
        state = best_candidate.review_state

    if user:
        resolved = resolved.overlay(user.normalized())

    resolved = resolved.normalized()
    if not best_candidate and resolved.is_minimum_viable():
        state = ReviewState.REVIEW_REQUIRED
    if not resolved.is_minimum_viable():
        state = ReviewState.MANUAL_REQUIRED
    return resolved, state


def _first(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple) and value:
        return str(value[0])
    return ""


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

