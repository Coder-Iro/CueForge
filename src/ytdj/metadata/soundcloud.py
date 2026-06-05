"""SoundCloud native metadata mapping."""

from __future__ import annotations

from typing import Any

from ytdj.metadata.normalize import clean_metadata, parse_artist_title, squash_spaces
from ytdj.models import MetadataCandidate, TrackMetadata

REMIX_MARKERS = (
    "bootleg",
    "remix",
    "edit",
    "mashup",
    "flip",
    "refix",
    "free dl",
    "free download",
)


def build_soundcloud_metadata(info: dict[str, Any], source_url: str = "") -> TrackMetadata:
    title = squash_spaces(str(info.get("title") or info.get("track") or ""))
    parsed_artist, _ = parse_artist_title(title)
    artist = squash_spaces(
        str(
            info.get("artist")
            or info.get("creator")
            or _first(info.get("creators"))
            or info.get("uploader")
            or parsed_artist
            or ""
        )
    )
    url = squash_spaces(source_url or str(info.get("webpage_url") or info.get("original_url") or ""))
    description = squash_spaces(str(info.get("description") or ""))
    comment_parts = [part for part in (url, _description_excerpt(description)) if part]
    cover_url = squash_spaces(str(info.get("thumbnail") or ""))

    metadata = TrackMetadata(
        title=title,
        artist=artist,
        album=squash_spaces(str(info.get("album") or "")),
        album_artist=artist,
        genre=_soundcloud_genre(info),
        release_date=str(info.get("release_date") or info.get("upload_date") or ""),
        cover_url=cover_url,
        cover_source="SoundCloud native" if cover_url else "",
        source_url=url,
        comments=" | ".join(comment_parts),
    )
    cleaned = clean_metadata(metadata)
    return TrackMetadata(
        title=title,
        artist=cleaned.artist,
        album=cleaned.album,
        album_artist=cleaned.album_artist,
        genre=cleaned.genre,
        release_date=cleaned.release_date,
        track_number=cleaned.track_number,
        disc_number=cleaned.disc_number,
        label=cleaned.label,
        isrc=cleaned.isrc,
        cover_url=cleaned.cover_url,
        cover_source=cleaned.cover_source,
        source_url=cleaned.source_url,
        musicbrainz_recording_id=cleaned.musicbrainz_recording_id,
        musicbrainz_release_id=cleaned.musicbrainz_release_id,
        comments=cleaned.comments,
    )


def build_soundcloud_native_candidate(info: dict[str, Any], source_url: str = "") -> MetadataCandidate:
    metadata = build_soundcloud_metadata(info, source_url)
    matched = ["native_metadata", "title", "artist", "source_url"]
    if metadata.genre:
        matched.append("genre")
    if metadata.cover_url:
        matched.append("cover")
    if _looks_like_remix(metadata.title):
        matched.append("remix_title_preserved")
    return MetadataCandidate(
        provider="soundcloud",
        metadata=metadata,
        score=0.99 if metadata.is_minimum_viable() else 0.64,
        matched_fields=tuple(matched),
        raw={
            "trusted_native": True,
            "reference_only": False,
            "reason": "SoundCloud native metadata is trusted for remix, bootleg, edit, and mashup tracks.",
        },
    )


def as_reference_candidate(candidate: MetadataCandidate) -> MetadataCandidate:
    return MetadataCandidate(
        provider=f"{candidate.provider}_reference",
        metadata=candidate.metadata,
        score=min(candidate.score, 0.84),
        matched_fields=candidate.matched_fields,
        raw={**candidate.raw, "reference_only": True},
    )


def _soundcloud_genre(info: dict[str, Any]) -> str:
    genre = squash_spaces(str(info.get("genre") or ""))
    if genre:
        return genre
    for tag in _tags(info):
        if tag and not tag.startswith("#"):
            return tag
    return ""


def _tags(info: dict[str, Any]) -> list[str]:
    value = info.get("tags") or info.get("categories") or []
    if isinstance(value, str):
        return [squash_spaces(part.strip("#")) for part in value.replace(",", " ").split() if part.strip("#")]
    if isinstance(value, list | tuple):
        return [squash_spaces(str(part).strip("#")) for part in value if squash_spaces(str(part).strip("#"))]
    return []


def _description_excerpt(description: str, limit: int = 500) -> str:
    if not description:
        return ""
    if len(description) <= limit:
        return description
    return description[:limit].rstrip() + "..."


def _looks_like_remix(title: str) -> bool:
    lowered = title.casefold()
    return any(marker in lowered for marker in REMIX_MARKERS)


def _first(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple) and value:
        return str(value[0])
    return ""
