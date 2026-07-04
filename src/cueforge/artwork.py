"""Cover artwork fetching and local cache utilities."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from platformdirs import user_cache_path

MAX_COVER_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CachedCover:
    path: Path
    mime: str
    source_url: str


def default_cover_cache_dir() -> Path:
    return user_cache_path("CueForge") / "covers"


def cache_cover_url(url: str, *, cache_dir: Path | None = None, cache_key: str = "", session: Any | None = None) -> CachedCover:
    data, mime = fetch_cover_url(url, session=session)
    cache_root = cache_dir or default_cover_cache_dir()
    cache_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:16]
    stem = _safe_cache_stem(cache_key) or "cover"
    path = cache_root / f"{stem}-{digest}{_cover_extension(mime, url)}"
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    return CachedCover(path=path, mime=mime, source_url=url)


def fetch_cover_url(url: str, *, session: Any | None = None) -> tuple[bytes, str]:
    client = session or requests
    response = client.get(url, timeout=15)
    response.raise_for_status()
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > MAX_COVER_BYTES:
                raise ValueError("cover image is too large")
        except ValueError as exc:
            if str(exc) == "cover image is too large":
                raise
    data = response.content
    if len(data) > MAX_COVER_BYTES:
        raise ValueError("cover image is too large")
    mime = normalize_cover_mime(response.headers.get("Content-Type", ""), url)
    if not is_image_mime(mime):
        raise ValueError(f"cover fetch returned non-image content type: {mime}")
    return data, mime


def read_cover_file(path: Path | str) -> tuple[bytes, str]:
    cover_path = Path(path)
    data = cover_path.read_bytes()
    if len(data) > MAX_COVER_BYTES:
        raise ValueError("cover image is too large")
    mime = normalize_cover_mime("", cover_path.name)
    if not is_image_mime(mime):
        raise ValueError(f"cover file has non-image content type: {mime}")
    return data, mime


def normalize_cover_mime(mime: str, source: str) -> str:
    cleaned = (mime or "").split(";", 1)[0].strip().lower()
    if cleaned:
        return cleaned
    return mimetypes.guess_type(source)[0] or "image/jpeg"


def is_image_mime(mime: str) -> bool:
    return mime.startswith("image/")


def _cover_extension(mime: str, url: str) -> str:
    guessed = mimetypes.guess_extension(mime)
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def _safe_cache_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip(".-")
    return stem[:80]
