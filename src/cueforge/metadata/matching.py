"""Metadata candidate scoring helpers."""

from __future__ import annotations

from difflib import SequenceMatcher

from cueforge.metadata.normalize import squash_spaces


def text_similarity(left: str, right: str) -> float:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _normalize_text(value: str) -> str:
    return squash_spaces(value).casefold()

