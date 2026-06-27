"""Cookie-file based authentication helpers for ytmusicapi."""

from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

YTMUSIC_ORIGIN = "https://music.youtube.com"
YTMUSIC_COOKIE_URL = f"{YTMUSIC_ORIGIN}/"


@dataclass(slots=True, frozen=True)
class YTMusicCookieAuthConfig:
    cookie_file: Path


class YTMusicCookieAuthError(RuntimeError):
    """Raised when a cookie file cannot produce a ytmusicapi auth payload."""


def build_ytmusic_cookie_auth(config: YTMusicCookieAuthConfig) -> dict[str, str]:
    """Build a ytmusicapi browser-auth dictionary from a Netscape cookies.txt file."""

    try:
        cookie_header = _cookie_header_from_cookie_file(config.cookie_file)
    except Exception as exc:
        raise YTMusicCookieAuthError(f"쿠키 파일을 읽을 수 없음: {exc}") from exc
    return _auth_from_cookie_header(cookie_header)


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
