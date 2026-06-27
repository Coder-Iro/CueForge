"""Authentication helpers for ytmusicapi."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from platformdirs import user_data_path

YTMUSIC_ORIGIN = "https://music.youtube.com"
YTMUSIC_COOKIE_URL = f"{YTMUSIC_ORIGIN}/"
OAUTH_CLIENT_ENV_VAR = "CUEFORGE_GOOGLE_OAUTH_CLIENT"
OAUTH_TOKEN_FILE_NAME = "ytmusic_oauth_token.json"


@dataclass(slots=True, frozen=True)
class YTMusicCookieAuthConfig:
    cookie_file: Path


@dataclass(slots=True, frozen=True)
class YTMusicOAuthClient:
    client_id: str
    client_secret: str
    source_path: Path


class YTMusicCookieAuthError(RuntimeError):
    """Raised when a cookie file cannot produce a ytmusicapi auth payload."""


class YTMusicOAuthError(RuntimeError):
    """Raised when OAuth setup cannot continue."""


def build_ytmusic_cookie_auth(config: YTMusicCookieAuthConfig) -> dict[str, str]:
    """Build a ytmusicapi browser-auth dictionary from a Netscape cookies.txt file."""

    try:
        cookie_header = _cookie_header_from_cookie_file(config.cookie_file)
    except Exception as exc:
        raise YTMusicCookieAuthError(f"쿠키 파일을 읽을 수 없음: {exc}") from exc
    return _auth_from_cookie_header(cookie_header)


def default_ytmusic_oauth_token_path() -> Path:
    return user_data_path("CueForge") / OAUTH_TOKEN_FILE_NAME


def ytmusic_oauth_client_candidates(root: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []
    env_path = os.environ.get(OAUTH_CLIENT_ENV_VAR)
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        (
            root / "config" / "google_oauth_client.json",
            root / "google_oauth_client.json",
        )
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def find_ytmusic_oauth_client_file(root: Path) -> Path | None:
    for path in ytmusic_oauth_client_candidates(root):
        if path.exists():
            return path
    return None


def load_ytmusic_oauth_client(path: Path) -> YTMusicOAuthClient:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise YTMusicOAuthError(f"OAuth 클라이언트 파일을 읽을 수 없음: {exc}") from exc
    if not isinstance(payload, dict):
        raise YTMusicOAuthError("OAuth 클라이언트 파일 형식이 올바르지 않음")

    client = payload.get("installed") or payload.get("web") or payload
    if not isinstance(client, dict):
        raise YTMusicOAuthError("OAuth 클라이언트 설정을 찾을 수 없음")
    client_id = str(client.get("client_id") or "").strip()
    client_secret = str(client.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise YTMusicOAuthError("OAuth client_id/client_secret이 비어 있음")
    return YTMusicOAuthClient(client_id=client_id, client_secret=client_secret, source_path=path)


def build_ytmusic_oauth_credentials(client: YTMusicOAuthClient) -> Any:
    from ytmusicapi import OAuthCredentials

    return OAuthCredentials(client_id=client.client_id, client_secret=client.client_secret)


def write_ytmusic_oauth_token(token: dict[str, Any], path: Path) -> None:
    if "expires_at" not in token and token.get("expires_in"):
        token = {**token, "expires_at": int(time.time()) + int(token["expires_in"])}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")


def _auth_from_cookie_header(cookie_header: str) -> dict[str, str]:
    if not cookie_header:
        raise YTMusicCookieAuthError("music.youtube.com 쿠키가 없음")
    if not _has_secure_3papisid(cookie_header):
        raise YTMusicCookieAuthError("__Secure-3PAPISID 쿠키가 없음")
    # ytmusicapi recalculates SAPISIDHASH on each request when this marker is present.
    return {
        "Accept": "*/*",
        "Authorization": "SAPISIDHASH 0_0",
        "Content-Type": "application/json",
        "Cookie": cookie_header,
        "X-Goog-AuthUser": "0",
        "origin": YTMUSIC_ORIGIN,
        "x-origin": YTMUSIC_ORIGIN,
    }


def _cookie_header_from_cookie_file(path: Path) -> str:
    from yt_dlp.cookies import YoutubeDLCookieJar

    jar = YoutubeDLCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return _cookie_header_for_ytmusic(jar)


def _cookie_header_for_ytmusic(cookie_jar: Any) -> str:
    if hasattr(cookie_jar, "get_cookie_header"):
        return str(cookie_jar.get_cookie_header(YTMUSIC_COOKIE_URL) or "")
    raise TypeError("cookie jar does not support get_cookie_header")


def _has_secure_3papisid(cookie_header: str) -> bool:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header.replace('"', ""))
    except Exception:
        return "__Secure-3PAPISID=" in cookie_header
    return "__Secure-3PAPISID" in cookie
