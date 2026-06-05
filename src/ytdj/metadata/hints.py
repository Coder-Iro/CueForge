"""Metadata hints extracted from video descriptions and titles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ytdj.metadata.normalize import clean_metadata, parse_artist_title, squash_spaces
from ytdj.models import MetadataCandidate, TrackMetadata

THEME_HEADER_RE = re.compile(
    r"(?:▮|■|#|\*|-)?\s*"
    r"(?P<context>"
    r"オープニングテーマ|エンディングテーマ|主題歌|挿入歌|"
    r"opening\s*theme|ending\s*theme|insert\s*song|"
    r"OP(?:テーマ)?|ED(?:テーマ)?"
    r")\s*[:：]?\s*"
    r"(?P<rest>.*)",
    re.IGNORECASE,
)

QUOTED_SONG_RE = re.compile(
    r"(?P<artist>.+?)[「『\"“](?P<title>.+?)[」』\"”]"
)

BY_SONG_RE = re.compile(
    r"[\"“](?P<title>.+?)[\"”]\s+by\s+(?P<artist>.+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MetadataHint:
    metadata: TrackMetadata
    context: str
    source: str
    raw_text: str

    def to_candidate(self) -> MetadataCandidate:
        return MetadataCandidate(
            provider=f"description_{self.context.casefold().replace(' ', '_')}",
            score=0.78,
            matched_fields=("description", "theme_context", "title", "artist"),
            metadata=self.metadata,
            raw={
                "source": self.source,
                "context": self.context,
                "raw_text": self.raw_text,
                "reason": "Anime theme metadata extracted from the video description.",
            },
        )


def extract_metadata_hints(info: dict[str, Any]) -> list[MetadataHint]:
    description = str(info.get("description") or "")
    hints: list[MetadataHint] = []
    pending_context = ""
    pending_raw = ""

    for raw_line in description.splitlines():
        line = squash_spaces(raw_line)
        if not line:
            continue
        header = THEME_HEADER_RE.search(line)
        if header:
            pending_context = squash_spaces(header.group("context"))
            rest = squash_spaces(header.group("rest"))
            pending_raw = line
            if rest:
                hint = _parse_theme_line(rest, pending_context, line)
                if hint:
                    hints.append(hint)
                    pending_context = ""
                    pending_raw = ""
            continue

        if pending_context:
            hint = _parse_theme_line(line, pending_context, f"{pending_raw} {line}".strip())
            if hint:
                hints.append(hint)
                pending_context = ""
                pending_raw = ""

    return _dedupe_hints(hints)


def build_hint_candidates(info: dict[str, Any]) -> list[MetadataCandidate]:
    hints = extract_metadata_hints(info)
    preferred_types = preferred_theme_types(info)
    if preferred_types:
        matching_hints = [hint for hint in hints if theme_type_from_context(hint.context) in preferred_types]
        if matching_hints:
            hints = matching_hints
    return [hint.to_candidate() for hint in hints]


def preferred_theme_types(info: dict[str, Any]) -> set[str]:
    title = squash_spaces(str(info.get("title") or ""))
    lowered = title.casefold()
    preferred: set[str] = set()
    if any(token in lowered for token in ("エンディング", "ending", " ed ", " ed映像", "edテーマ")):
        preferred.add("ED")
    if any(token in lowered for token in ("オープニング", "opening", " op ", " op映像", "opテーマ")):
        preferred.add("OP")
    return preferred


def theme_type_from_context(context: str) -> str:
    lowered = squash_spaces(context).casefold()
    if "エンディング" in lowered or "ending" in lowered or lowered.startswith("ed"):
        return "ED"
    if "オープニング" in lowered or "opening" in lowered or lowered.startswith("op"):
        return "OP"
    if "挿入歌" in lowered or "insert" in lowered:
        return "INSERT"
    return "THEME"


def _parse_theme_line(line: str, context: str, raw_text: str) -> MetadataHint | None:
    metadata = _parse_quoted_song(line) or _parse_by_song(line) or _parse_artist_dash_title(line)
    if not metadata or not metadata.is_minimum_viable():
        return None
    return MetadataHint(
        metadata=metadata,
        context=context,
        source="description",
        raw_text=raw_text,
    )


def _parse_quoted_song(line: str) -> TrackMetadata | None:
    match = QUOTED_SONG_RE.search(line)
    if not match:
        return None
    artist = _strip_artist_prefix(match.group("artist"))
    title = match.group("title")
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist, genre="Anison"))


def _parse_by_song(line: str) -> TrackMetadata | None:
    match = BY_SONG_RE.search(line)
    if not match:
        return None
    artist = _strip_episode_suffix(match.group("artist"))
    title = match.group("title")
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist, genre="Anison"))


def _parse_artist_dash_title(line: str) -> TrackMetadata | None:
    artist, title = parse_artist_title(line)
    if not artist or not title:
        return None
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist, genre="Anison"))


def _strip_artist_prefix(value: str) -> str:
    value = squash_spaces(value)
    value = re.sub(r"^(?:歌|歌唱|アーティスト|artist|singer)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    return _strip_episode_suffix(value)


def _strip_episode_suffix(value: str) -> str:
    return squash_spaces(re.sub(r"\s*\((?:ep|eps|episode|episodes)\.?\s*[^)]*\)\s*$", "", value, flags=re.IGNORECASE))


def _dedupe_hints(hints: list[MetadataHint]) -> list[MetadataHint]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[MetadataHint] = []
    for hint in hints:
        key = (
            hint.metadata.title.casefold(),
            hint.metadata.artist.casefold(),
            hint.context.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(hint)
    return unique
