"""Metadata candidate scoring helpers."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from ytdj.metadata.normalize import squash_spaces
from ytdj.models import MetadataCandidate, TrackMetadata


def text_similarity(left: str, right: str) -> float:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def score_candidate(
    reference: TrackMetadata,
    candidate: TrackMetadata,
    *,
    provider_score: float = 0.0,
    reference_duration_ms: int | None = None,
    candidate_duration_ms: int | None = None,
    provider: str = "musicbrainz",
    raw: dict[str, Any] | None = None,
) -> MetadataCandidate:
    score = max(0.0, min(provider_score, 1.0)) * 0.20
    matched_fields: list[str] = []

    title_score = text_similarity(reference.title, candidate.title)
    score += title_score * 0.35
    if title_score >= 0.9:
        matched_fields.append("title")

    artist_score = text_similarity(reference.artist, candidate.artist)
    score += artist_score * 0.30
    if artist_score >= 0.9:
        matched_fields.append("artist")

    album_score = text_similarity(reference.album, candidate.album)
    score += album_score * 0.10
    if album_score >= 0.9:
        matched_fields.append("album")

    if _year(reference.release_date) and _year(reference.release_date) == _year(candidate.release_date):
        score += 0.05
        matched_fields.append("release_year")

    duration_score = _duration_score(reference_duration_ms, candidate_duration_ms)
    score += duration_score * 0.20
    if duration_score >= 0.9:
        matched_fields.append("duration")

    return MetadataCandidate(
        provider=provider,
        metadata=candidate,
        score=round(max(0.0, min(score, 1.0)), 3),
        matched_fields=tuple(matched_fields),
        raw=raw or {},
    )


def _normalize_text(value: str) -> str:
    return squash_spaces(value).casefold()


def _year(value: str) -> str:
    value = squash_spaces(value)
    if len(value) >= 4 and value[:4].isdigit():
        return value[:4]
    return ""


def _duration_score(reference_ms: int | None, candidate_ms: int | None) -> float:
    if not reference_ms or not candidate_ms:
        return 0.0
    delta = abs(reference_ms - candidate_ms)
    if delta <= 2000:
        return 1.0
    if delta >= 15000:
        return 0.0
    return 1.0 - ((delta - 2000) / 13000)

