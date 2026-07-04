"""Metadata hints extracted from video descriptions and titles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cueforge.metadata.normalize import build_safe_fallback, clean_metadata, parse_artist_title, squash_spaces
from cueforge.models import MetadataCandidate, TrackMetadata

THEME_HEADER_RE = re.compile(
    r"^\s*(?:▮|■|#|\*|-)?\s*"
    r"(?P<context>"
    r"オープニングテーマ|エンディングテーマ|主題歌|挿入歌|"
    r"opening\s*theme|ending\s*theme|insert\s*song|"
    r"OPテーマ|EDテーマ|OP\b|ED\b"
    r")\s*[:：]?\s*"
    r"(?P<rest>.*)",
    re.IGNORECASE,
)

QUOTED_SONG_RE = re.compile(
    r"(?P<artist>.+?)[「『\"“](?P<title>.+?)[」』\"”]"
)

TITLE_QUOTED_SONG_RE = re.compile(
    r"(?P<artist>.+?)[「『“](?P<title>.+?)[」』”]"
)

TITLE_FIRST_QUOTED_SONG_RE = re.compile(
    r"[「『“](?P<title>.+?)[」』”]\s*(?P<artist>.+)"
)

BY_SONG_RE = re.compile(
    r"[\"“](?P<title>.+?)[\"”]\s+by\s+(?P<artist>.+)",
    re.IGNORECASE,
)

CREDIT_RE = re.compile(r"^(?P<label>[^:：]{1,24})\s*[:：]\s*(?P<value>.+)$", re.IGNORECASE)
TITLE_LABELS = {
    "제목",
    "곡",
    "곡명",
    "노래 제목",
    "title",
    "song",
    "song title",
    "track",
    "track title",
    "曲名",
    "楽曲名",
    "タイトル",
}
ARTIST_LABELS = {
    "아티스트",
    "가수",
    "artist",
    "artists",
    "performer",
    "performed by",
    "アーティスト",
    "歌手",
}
COMPOSER_LABELS = {
    "작사/작곡",
    "작곡/작사",
    "작곡",
    "작사",
    "작사 작곡",
    "작곡 작사",
    "作詞/作曲",
    "作曲/作詞",
    "作詞",
    "作曲",
    "composer",
    "composition",
    "music",
    "lyrics/music",
}
VOCAL_LABELS = {
    "노래",
    "보컬",
    "가수",
    "歌",
    "歌唱",
    "vocal",
    "vocals",
    "singer",
}


@dataclass(frozen=True, slots=True)
class MetadataHint:
    metadata: TrackMetadata
    context: str
    source: str
    raw_text: str
    prefer_initial: bool = False

    def to_candidate(self) -> MetadataCandidate:
        provider_prefix = self.source.casefold().replace(" ", "_")
        matched_fields = tuple(dict.fromkeys((self.source, self.context, "title", "artist")))
        reason = (
            "Cover metadata extracted from the video title and channel."
            if self.context == "cover"
            else "Anime theme metadata extracted from the video description."
        )
        return MetadataCandidate(
            provider=f"{provider_prefix}_{self.context.casefold().replace(' ', '_')}",
            score=0.78,
            matched_fields=matched_fields,
            metadata=self.metadata,
            raw={
                "source": self.source,
                "context": self.context,
                "raw_text": self.raw_text,
                "reason": reason,
                **({"prefer_initial_metadata": True} if self.prefer_initial else {}),
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
        header = THEME_HEADER_RE.match(line)
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
    if not hints:
        hints = extract_cover_hints(info)
    if not hints:
        hints = (
            extract_credit_hints(info)
            if _looks_like_cover_source(info)
            else [*extract_title_hints(info), *extract_credit_hints(info)]
        )
    preferred_types = preferred_theme_types(info)
    if preferred_types:
        matching_hints = [hint for hint in hints if theme_type_from_context(hint.context) in preferred_types]
        if matching_hints:
            hints = matching_hints
    fallback = build_safe_fallback(info)
    return [_candidate_with_fallback(hint.to_candidate(), fallback) for hint in hints]


def extract_cover_hints(info: dict[str, Any]) -> list[MetadataHint]:
    title = squash_spaces(str(info.get("title") or ""))
    if not title or not _looks_like_cover_source(info):
        return []
    artist = _cover_performer_artist(info)
    song_title = _cover_song_title_from_video_title(title, artist)
    if not artist or not song_title:
        return []
    metadata = clean_metadata(TrackMetadata(title=song_title, artist=artist, album_artist=artist))
    if not metadata.is_minimum_viable():
        return []
    return [
        MetadataHint(
            metadata=metadata,
            context="cover",
            source="title",
            raw_text=title,
            prefer_initial=True,
        )
    ]


def extract_title_hints(info: dict[str, Any]) -> list[MetadataHint]:
    title = squash_spaces(str(info.get("title") or ""))
    metadata = _parse_title_quoted_song(title) or _parse_artist_dash_title(title, genre="")
    if not metadata or not metadata.is_minimum_viable():
        return []
    return [
        MetadataHint(
            metadata=metadata,
            context="quoted_song" if "「" in title or "『" in title or "“" in title else "artist_title",
            source="title",
            raw_text=title,
        )
    ]


def extract_credit_hints(info: dict[str, Any]) -> list[MetadataHint]:
    description = str(info.get("description") or "")
    lines = [squash_spaces(line) for line in description.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    if _is_auto_generated_youtube_description(lines):
        return []

    title = ""
    explicit_artist = ""
    composer = ""
    vocalist = ""
    for line in lines[:12]:
        match = CREDIT_RE.match(line)
        if not match:
            continue
        label = _normalize_credit_label(match.group("label"))
        value = _clean_credit_value(match.group("value"))
        if not value:
            continue
        if label in TITLE_LABELS and not title:
            title = _strip_parenthetical_alias(value)
        if label in ARTIST_LABELS and not explicit_artist:
            explicit_artist = value
        if label in COMPOSER_LABELS and not composer:
            composer = value
        if label in VOCAL_LABELS and not vocalist:
            vocalist = value

    title = title or _description_song_title(lines)
    if not title:
        return []

    artist = explicit_artist or composer or vocalist
    if not artist:
        return []

    metadata = clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist))
    if not metadata.is_minimum_viable():
        return []
    return [
        MetadataHint(
            metadata=metadata,
            context="credits",
            source="description",
            raw_text="\n".join(lines[:12]),
        )
    ]


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
    metadata = _parse_quoted_song(line) or _parse_by_song(line) or _parse_artist_dash_title(line, genre="Anison")
    if not metadata or not metadata.is_minimum_viable():
        return None
    return MetadataHint(
        metadata=metadata,
        context=context,
        source="description",
        raw_text=raw_text,
        prefer_initial=True,
    )


def _parse_quoted_song(line: str) -> TrackMetadata | None:
    match = QUOTED_SONG_RE.search(line)
    if not match:
        return None
    artist = _strip_artist_prefix(match.group("artist"))
    title = match.group("title")
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist, genre="Anison"))


def _parse_title_quoted_song(line: str) -> TrackMetadata | None:
    match = TITLE_QUOTED_SONG_RE.search(line)
    if match:
        artist = _strip_artist_prefix(match.group("artist"))
        title = match.group("title")
        return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist))

    match = TITLE_FIRST_QUOTED_SONG_RE.search(line)
    if not match:
        return None
    artist = _strip_title_trailing_artist_noise(match.group("artist"))
    title = match.group("title")
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist))


def _parse_by_song(line: str) -> TrackMetadata | None:
    match = BY_SONG_RE.search(line)
    if not match:
        return None
    artist = _strip_episode_suffix(match.group("artist"))
    title = match.group("title")
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist, genre="Anison"))


def _parse_artist_dash_title(line: str, *, genre: str) -> TrackMetadata | None:
    artist, title = parse_artist_title(line)
    if not artist or not title:
        return None
    return clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist, genre=genre))


def _candidate_with_fallback(candidate: MetadataCandidate, fallback: TrackMetadata) -> MetadataCandidate:
    metadata = candidate.metadata.with_defaults_from(
        TrackMetadata(
            genre=fallback.genre,
            release_date=fallback.release_date,
            cover_url=fallback.cover_url,
            cover_source=fallback.cover_source,
            source_url=fallback.source_url,
            comments=fallback.comments,
        )
    ).normalized()
    return MetadataCandidate(
        provider=candidate.provider,
        score=candidate.score,
        matched_fields=candidate.matched_fields,
        metadata=metadata,
        raw=candidate.raw,
    )


def _description_song_title(lines: list[str]) -> str:
    for line in lines[:4]:
        if CREDIT_RE.match(line):
            continue
        if line.casefold().startswith(("http://", "https://", "streaming ", "download ")):
            continue
        if " by " in line.casefold() and len(line) > 80:
            continue
        return _strip_parenthetical_alias(line)
    return ""


def _looks_like_cover_source(info: dict[str, Any]) -> bool:
    source = squash_spaces(
        " ".join(
            str(value or "")
            for value in (
                info.get("fulltitle"),
                info.get("title"),
                info.get("track"),
                info.get("description"),
            )
        )
    ).casefold()
    return any(token in source for token in ("cover", "커버", "歌ってみた", "covered by"))


def _cover_performer_artist(info: dict[str, Any]) -> str:
    title_artist = _cover_performer_from_title(str(info.get("title") or info.get("fulltitle") or ""))
    if title_artist:
        return title_artist
    for key in ("channel", "uploader"):
        artist = _clean_cover_performer(str(info.get(key) or ""))
        if artist:
            return artist
    return ""


def _cover_performer_from_title(value: str) -> str:
    value = squash_spaces(value)
    match = re.search(
        r"(?:[【\[\(（]\s*)?covered\s+by\s+(?P<artist>.+?)(?:\s*[】\]\)）]|$)",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return _clean_cover_performer(match.group("artist"))


def _clean_cover_performer(value: str) -> str:
    value = squash_spaces(value)
    value = re.sub(r"\s+-\s+topic$", "", value, flags=re.IGNORECASE)
    return value.strip(" -–—/|｜ㅣ")


def _cover_song_title_from_video_title(title: str, artist: str) -> str:
    title = _strip_cover_markers(title)
    segments = [squash_spaces(segment) for segment in re.split(r"[|｜ㅣ]", title) if squash_spaces(segment)]
    for segment in reversed(segments or [title]):
        if _segment_looks_like_cover_performer(segment, artist):
            continue
        candidate = _cover_song_title_from_segment(segment)
        if candidate:
            return candidate
    return ""


def _strip_cover_markers(value: str) -> str:
    value = squash_spaces(value)
    value = re.sub(r"\s*[【\[\(]\s*(?:cover|covered|커버|歌ってみた)\s*[】\]\)]\s*", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\bcover(?:ed)?(?:\s+by)?\b|커버|歌ってみた", " ", value, flags=re.IGNORECASE)
    return squash_spaces(value).strip(" -–—/|｜ㅣ")


def _cover_song_title_from_segment(segment: str) -> str:
    segment = squash_spaces(segment).strip(" -–—/|｜ㅣ")
    if not segment:
        return ""

    leading = _leading_title_before_bracket(segment)
    if leading:
        return leading

    had_slash_context = "/" in segment
    if "/" in segment:
        segment = squash_spaces(segment.rsplit("/", 1)[-1]).strip(" -–—")

    if had_slash_context:
        for separator in (" - ", " – ", " — "):
            if separator in segment:
                left, _right = segment.split(separator, 1)
                return _clean_cover_song_title(left)
        match = re.match(r"^(?P<title>.+?)\s*[-–—]\s*(?P<artist>.+)$", segment)
        if match:
            return _clean_cover_song_title(match.group("title"))
    elif re.search(r"\s[-–—]\s", segment):
        return ""

    return _clean_cover_song_title(segment)


def _segment_looks_like_cover_performer(segment: str, artist: str) -> bool:
    segment = squash_spaces(segment)
    artist = squash_spaces(artist)
    if not segment or not artist:
        return False
    segment_base = squash_spaces(re.sub(r"\s*[（(].*?[）)]", "", segment))
    if not segment_base:
        return False
    segment_norm = segment_base.casefold()
    artist_norm = artist.casefold()
    return segment_norm in artist_norm or artist_norm in segment_norm


def _leading_title_before_bracket(value: str) -> str:
    match = re.match(r"^(?P<title>.+?)\s*[【\[\(（].+[】\]\)）]\s*$", value)
    if not match:
        return ""
    return _clean_cover_song_title(match.group("title"))


def _clean_cover_song_title(value: str) -> str:
    value = squash_spaces(value)
    value = re.sub(r"^\s*(?:song|title|track)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    value = _strip_cover_original_credit_tail(value)
    return squash_spaces(value).strip(" -–—/|｜ㅣ")


def _strip_cover_original_credit_tail(value: str) -> str:
    for separator in (" - ", " – ", " — "):
        if separator not in value:
            continue
        left, right = value.split(separator, 1)
        if _looks_like_original_credit_tail(right):
            return left
    return value


def _looks_like_original_credit_tail(value: str) -> bool:
    return bool(re.search(r"\b(?:feat\.?|ft\.?|featuring)\b", squash_spaces(value), flags=re.IGNORECASE))


def _is_auto_generated_youtube_description(lines: list[str]) -> bool:
    if not lines:
        return False
    first = lines[0].casefold()
    return first.startswith("provided to youtube by ") and any(
        line.casefold() == "auto-generated by youtube." for line in lines[-4:]
    )


def _strip_parenthetical_alias(value: str) -> str:
    value = squash_spaces(value)
    match = re.match(r"^(?P<base>.+?)\s*[（(](?P<alias>[^）)]+)[）)]\s*$", value)
    if not match:
        return value
    base = squash_spaces(match.group("base"))
    alias = squash_spaces(match.group("alias"))
    if _contains_cjk(base) and _mostly_latin(alias):
        return base
    return value


def _contains_cjk(value: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" or "\uac00" <= char <= "\ud7af" for char in value)


def _mostly_latin(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    latin = [char for char in letters if "a" <= char.casefold() <= "z"]
    return len(latin) / len(letters) >= 0.7


def _normalize_credit_label(value: str) -> str:
    return squash_spaces(value).casefold()


def _clean_credit_value(value: str) -> str:
    value = squash_spaces(value)
    value = re.split(r"\s{2,}|[／/]\s*(?:편곡|arrange|編曲)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    return squash_spaces(value)


def _strip_artist_prefix(value: str) -> str:
    value = squash_spaces(value)
    value = re.sub(r"^(?:歌|歌唱|アーティスト|artist|singer)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    return _strip_episode_suffix(value)


def _strip_title_trailing_artist_noise(value: str) -> str:
    value = _strip_artist_prefix(value)
    return squash_spaces(
        re.sub(
            r"\s+(?:full|full\s*ver(?:sion)?|short\s*ver(?:sion)?|music\s*video|mv|lyrics?|歌詞)\s*$",
            "",
            value,
            flags=re.IGNORECASE,
        )
    )


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
