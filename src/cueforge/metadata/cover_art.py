"""Cover Art Archive lookup for MusicBrainz releases."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from platformdirs import user_cache_path


class HTTPSessionLike(Protocol):
    headers: dict[str, str]

    def get(self, url: str, *, timeout: int) -> Any: ...


@dataclass(slots=True)
class CoverArtConfig:
    app_name: str = "CueForge"
    app_version: str = "0.1.0"
    contact: str = ""
    cache_path: Path | None = None
    timeout_seconds: int = 15
    image_size: str = "500"

    @property
    def user_agent(self) -> str:
        suffix = f" ({self.contact})" if self.contact else ""
        return f"{self.app_name}/{self.app_version}{suffix}"


class CoverArtProvider:
    API_ROOT = "https://coverartarchive.org"

    def __init__(
        self,
        config: CoverArtConfig | None = None,
        *,
        session: HTTPSessionLike | None = None,
    ) -> None:
        self.config = config or CoverArtConfig()
        self.session = session or self._create_session()
        self.session.headers.update({"User-Agent": self.config.user_agent})
        self.cache = _JsonCache(self.config.cache_path or user_cache_path("CueForge") / "cover_art.sqlite")

    def lookup(self, release_id: str) -> str:
        release_id = release_id.strip()
        if not release_id:
            return ""
        payload = self._get_release_json(release_id)
        return _select_front_cover_url(payload, size=self.config.image_size)

    def _get_release_json(self, release_id: str) -> dict[str, Any]:
        cache_key = f"release:{release_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        response = self.session.get(f"{self.API_ROOT}/release/{release_id}/", timeout=self.config.timeout_seconds)
        if getattr(response, "status_code", None) == 404:
            payload: dict[str, Any] = {}
        else:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                payload = {}
        self.cache.set(cache_key, payload)
        return payload

    @staticmethod
    def _create_session() -> HTTPSessionLike:
        import requests

        return requests.Session()


class _JsonCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def set(self, key: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, payload, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(payload), time.time()),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def _select_front_cover_url(payload: dict[str, Any], *, size: str = "500") -> str:
    images = payload.get("images") or []
    if not isinstance(images, list):
        return ""
    for image in images:
        if isinstance(image, dict) and image.get("front") is True:
            return _image_url(image, size=size)
    return ""


def _image_url(image: dict[str, Any], *, size: str) -> str:
    thumbnails = image.get("thumbnails")
    if isinstance(thumbnails, dict):
        for key in _thumbnail_keys(size):
            value = thumbnails.get(key)
            if value:
                return str(value)
    return str(image.get("image") or "")


def _thumbnail_keys(size: str) -> tuple[str, ...]:
    if size == "500":
        return ("500", "large")
    if size == "250":
        return ("250", "small")
    return (size,)
