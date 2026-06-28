"""Metadata cleanup and merge policy."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata

NOISE_PATTERNS = [
    re.compile(r"^\s*[【\[\(](?:official\s*)?(?:mv|music\s*video|audio|lyric\s*video|lyrics?|visualizer|pv)[】\]\)]\s*", re.IGNORECASE),
    re.compile(r"\s*\[(?:official\s*)?(?:music\s*)?video\]\s*", re.IGNORECASE),
    re.compile(r"\s*\((?:official\s*)?(?:music\s*)?video\)\s*", re.IGNORECASE),
    re.compile(r"\s*[【\[\(](?:official\s*)?(?:mv|music\s*video|audio|lyric\s*video|lyrics?|visualizer|pv)[】\]\)]\s*$", re.IGNORECASE),
    re.compile(r"\s*\[(?:official\s*)?audio\]\s*", re.IGNORECASE),
    re.compile(r"\s*\((?:official\s*)?audio\)\s*", re.IGNORECASE),
    re.compile(r"\s*\[(?:lyrics?|lyric video)\]\s*", re.IGNORECASE),
    re.compile(r"\s*\((?:lyrics?|lyric video)\)\s*", re.IGNORECASE),
    re.compile(r"\s*\b(?:HD|HQ|4K)\b\s*$", re.IGNORECASE),
]

GENERIC_ARTIST_LABELS = {
    "보컬로이드",
    "보컬 로이드",
    "보카로",
    "vocaloid",
    "ボーカロイド",
    "ボカロ",
    "utau",
    "우타우",
}


def squash_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_title(value: str) -> str:
    title = value or ""
    for pattern in NOISE_PATTERNS:
        title = pattern.sub(" ", title)
    return squash_spaces(title).strip(" -–—")


def clean_artist(value: str) -> str:
    return squash_spaces(value)


def parse_artist_title(value: str) -> tuple[str, str]:
    cleaned = clean_title(value)
    for separator in (" - ", " – ", " — "):
        if separator in cleaned:
            artist, title = cleaned.split(separator, 1)
            return _validated_parsed_artist_title(artist, title)
    match = re.match(r"^(?P<artist>[^-–—]{1,80}?)\s*[-–—]\s*(?P<title>[^-–—].+)$", cleaned)
    if match:
        return _validated_parsed_artist_title(match.group("artist"), match.group("title"))
    return "", cleaned


def is_generic_artist_label(value: str) -> bool:
    normalized = squash_spaces(value).casefold()
    compact = normalized.replace(" ", "")
    return normalized in GENERIC_ARTIST_LABELS or compact in {label.replace(" ", "") for label in GENERIC_ARTIST_LABELS}


def _validated_parsed_artist_title(artist: str, title: str) -> tuple[str, str]:
    parsed_artist = clean_artist(artist)
    parsed_title = clean_title(title)
    if is_generic_artist_label(parsed_artist):
        return "", parsed_title
    return parsed_artist, parsed_title


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
        cover_source=squash_spaces(metadata.cover_source),
        source_url=squash_spaces(metadata.source_url),
        musicbrainz_recording_id=squash_spaces(metadata.musicbrainz_recording_id),
        musicbrainz_release_id=squash_spaces(metadata.musicbrainz_release_id),
        comments=squash_spaces(metadata.comments),
    )


def build_safe_fallback(info: dict[str, Any], source_url: str = "") -> TrackMetadata:
    title = str(info.get("track") or info.get("title") or "")
    preferred_creator = preferred_creator_artist(info)
    native_artist = str(
        info.get("artist")
        or _first(info.get("artists"))
        or preferred_creator
        or info.get("creator")
        or _first(info.get("creators"))
        or ""
    )
    parsed_artist, parsed_title = parse_artist_title(title)
    artist = native_artist or str(info.get("uploader") or info.get("uploader_id") or "") or parsed_artist
    metadata = clean_metadata(
        TrackMetadata(
            title=title or parsed_title,
            artist=artist,
            album=str(info.get("album") or info.get("series") or ""),
            album_artist=str(info.get("album_artist") or _first(info.get("album_artists")) or ""),
            genre=str(info.get("genre") or _first(info.get("genres")) or _first(info.get("categories")) or ""),
            release_date=str(info.get("release_date") or info.get("upload_date") or ""),
            track_number=_to_int(info.get("track_number")),
            disc_number=_to_int(info.get("disc_number")),
            cover_url=str(info.get("thumbnail") or ""),
            cover_source="platform thumbnail" if info.get("thumbnail") else "",
            source_url=source_url or str(info.get("webpage_url") or ""),
            comments=str(info.get("webpage_url") or source_url or ""),
        )
    )
    metadata = prefer_creator_artist_over_official_metadata(metadata, info)
    if preferred_creator and metadata.artist == preferred_creator and not metadata.album_artist:
        metadata = replace(metadata, album_artist=preferred_creator)
    return metadata


def preferred_creator_artist(info: dict[str, Any]) -> str:
    creators = _creator_values(info)
    if len(creators) < 2:
        return ""
    non_official = [creator for creator in creators if not is_official_project_label(creator)]
    official = [creator for creator in creators if is_official_project_label(creator)]
    if official and non_official:
        return non_official[0]
    return ""


def prefer_creator_artist_over_official_metadata(metadata: TrackMetadata, info: dict[str, Any]) -> TrackMetadata:
    creator_artist = preferred_creator_artist(info)
    if not creator_artist or not _metadata_artist_is_official_project(metadata.artist, info):
        return metadata
    album_artist = metadata.album_artist
    if not album_artist or _metadata_artist_is_official_project(album_artist, info):
        album_artist = creator_artist
    return clean_metadata(replace(metadata, artist=creator_artist, album_artist=album_artist))


def is_official_project_label(value: str) -> bool:
    normalized = squash_spaces(value).casefold().strip(" ,，、")
    if not normalized:
        return False
    separated = f" {normalized} "
    return (
        "公式" in normalized
        or "オフィシャル" in normalized
        or "チャンネル" in normalized
        or "프로젝트" in normalized
        or _contains_separated_token(separated, "official")
        or _contains_separated_token(separated, "channel")
        or _contains_separated_token(separated, "project")
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
        if best_candidate.score >= 0.85 or not resolved.is_minimum_viable():
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


def _creator_values(info: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("creator", "creators"):
        value = info.get(key)
        if isinstance(value, str):
            values.extend(_split_creator_value(value))
        elif isinstance(value, list | tuple):
            for item in value:
                values.extend(_split_creator_value(str(item)))
    return [value for value in values if value]


def _split_creator_value(value: str) -> list[str]:
    return [squash_spaces(part) for part in re.split(r"\s*[,，、]\s*", value) if squash_spaces(part)]


def _metadata_artist_is_official_project(artist: str, info: dict[str, Any]) -> bool:
    artist = squash_spaces(artist)
    if not artist:
        return False
    if is_official_project_label(artist):
        return True
    artist_norm = artist.casefold()
    for key in ("channel", "uploader"):
        source = squash_spaces(str(info.get(key) or ""))
        if source and artist_norm == source.casefold() and is_official_project_label(source):
            return True
    return False


def _contains_separated_token(value: str, token: str) -> bool:
    separator = r"[\s_./|｜\-:：]"
    return bool(re.search(rf"(?:^|{separator}){re.escape(token)}(?:$|{separator})", value))


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
