"""rekordbox-friendly MP3 tag writer."""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable
from pathlib import Path

from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TBPM,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSRC,
    TXXX,
    WOAS,
    ID3NoHeaderError,
)

from cueforge.models import TagWriteResult, TrackMetadata

CoverFetcher = Callable[[str], tuple[bytes, str]]

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class RekordboxTagWriter:
    def __init__(self, *, cover_fetcher: CoverFetcher | None = None) -> None:
        self._cover_fetcher = cover_fetcher or _fetch_cover

    def write(self, path: Path, metadata: TrackMetadata) -> TagWriteResult:
        path = Path(path)
        warnings: list[str] = []
        written: list[str] = []
        skipped: list[str] = []

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        _set_text(tags, TIT2, "title", metadata.title, written, skipped)
        _set_text(tags, TPE1, "artist", metadata.artist, written, skipped)
        _set_text(tags, TALB, "album", metadata.album, written, skipped)
        _set_text(tags, TPE2, "album_artist", metadata.album_artist, written, skipped)
        _set_text(tags, TCON, "genre", metadata.genre, written, skipped)
        _set_text(tags, TDRC, "date", metadata.release_date, written, skipped)
        _set_text(tags, TPUB, "label", metadata.label, written, skipped)
        _set_text(tags, TSRC, "isrc", metadata.isrc, written, skipped)

        if metadata.bpm:
            tags.setall("TBPM", [TBPM(encoding=3, text=[str(metadata.bpm)])])
            written.append("bpm")
        else:
            skipped.append("bpm")

        if metadata.track_number:
            tags.setall("TRCK", [TRCK(encoding=3, text=[str(metadata.track_number)])])
            written.append("track_number")
        else:
            skipped.append("track_number")

        if metadata.disc_number:
            tags.setall("TPOS", [TPOS(encoding=3, text=[str(metadata.disc_number)])])
            written.append("disc_number")
        else:
            skipped.append("disc_number")

        if metadata.comments or metadata.source_url:
            comment = metadata.comments or metadata.source_url
            tags.setall("COMM::eng", [COMM(encoding=3, lang="eng", desc="", text=[comment])])
            written.append("comments")
        else:
            skipped.append("comments")

        if metadata.source_url:
            tags.setall("WOAS", [WOAS(url=metadata.source_url)])
            written.append("source_url")
        else:
            skipped.append("source_url")

        _set_txxx(tags, "MusicBrainz Recording Id", metadata.musicbrainz_recording_id, "musicbrainz_recording_id", written, skipped)
        _set_txxx(tags, "MusicBrainz Album Id", metadata.musicbrainz_release_id, "musicbrainz_release_id", written, skipped)
        if metadata.bpm:
            _set_txxx(tags, "BPM Source", metadata.bpm_source, "bpm_source", written, skipped)
            bpm_confidence = "" if metadata.bpm_confidence is None else f"{metadata.bpm_confidence:.3f}"
            _set_txxx(tags, "BPM Confidence", bpm_confidence, "bpm_confidence", written, skipped)
        else:
            skipped.extend(["bpm_source", "bpm_confidence"])

        if metadata.cover_url:
            try:
                data, mime = self._cover_fetcher(metadata.cover_url)
                mime = _normalize_cover_mime(mime, metadata.cover_url)
                if data and _is_image_mime(mime):
                    tags.setall("APIC:", [APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data)])
                    written.append("cover")
                elif data:
                    skipped.append("cover")
                    warnings.append(f"cover fetch returned non-image content type: {mime}")
                else:
                    skipped.append("cover")
            except Exception as exc:
                skipped.append("cover")
                warnings.append(f"cover fetch failed: {exc}")
        else:
            skipped.append("cover")

        tags.save(path, v2_version=3)
        return TagWriteResult(
            path=path,
            written_fields=tuple(dict.fromkeys(written)),
            skipped_fields=tuple(dict.fromkeys(skipped)),
            warnings=tuple(warnings),
        )


def safe_track_filename(metadata: TrackMetadata, suffix: str = ".mp3") -> str:
    artist = metadata.artist or "Unknown Artist"
    title = metadata.title or "Unknown Title"
    stem = f"{artist} - {title}"
    stem = INVALID_FILENAME_CHARS.sub("_", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return f"{stem or 'track'}{suffix}"


def _set_text(
    tags: ID3,
    frame_type: type,
    field_name: str,
    value: str,
    written: list[str],
    skipped: list[str],
) -> None:
    value = value.strip()
    frame_id = frame_type.__name__
    if value:
        tags.setall(frame_id, [frame_type(encoding=3, text=[value])])
        written.append(field_name)
    else:
        skipped.append(field_name)


def _set_txxx(
    tags: ID3,
    description: str,
    value: str,
    field_name: str,
    written: list[str],
    skipped: list[str],
) -> None:
    if value:
        tags.setall(f"TXXX:{description}", [TXXX(encoding=3, desc=description, text=[value])])
        written.append(field_name)
    else:
        skipped.append(field_name)


def _fetch_cover(url: str) -> tuple[bytes, str]:
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    mime = _normalize_cover_mime(response.headers.get("Content-Type", ""), url)
    return response.content, mime


def _normalize_cover_mime(mime: str, url: str) -> str:
    cleaned = (mime or "").split(";", 1)[0].strip().lower()
    if cleaned:
        return cleaned
    return mimetypes.guess_type(url)[0] or "image/jpeg"


def _is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")
