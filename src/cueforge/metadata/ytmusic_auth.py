"""Browser cookie based authentication helpers for ytmusicapi."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

from cueforge.chrome_cookie_unlock import set_chromium_cookie_unlock_enabled

YTMUSIC_ORIGIN = "https://music.youtube.com"
YTMUSIC_COOKIE_URL = f"{YTMUSIC_ORIGIN}/"


@dataclass(slots=True, frozen=True)
class YTMusicBrowserAuthConfig:
    cookie_browser: str = ""
    unlock_browser_cookie_database: bool = False


class YTMusicBrowserAuthError(RuntimeError):
    """Raised when browser cookies cannot produce a ytmusicapi auth payload."""


CookieJarLoader = Callable[[str], Any]


def build_ytmusic_browser_auth(
    config: YTMusicBrowserAuthConfig,
    *,
    cookie_jar_loader: CookieJarLoader | None = None,
) -> dict[str, str] | None:
    """Build a ytmusicapi browser-auth dictionary from a local browser cookie jar."""

    browser = config.cookie_browser.strip().casefold()
    if not browser:
        return None

    if config.unlock_browser_cookie_database and _is_chromium_cookie_browser(browser):
        set_chromium_cookie_unlock_enabled(True)
    else:
        set_chromium_cookie_unlock_enabled(False)

    loader = cookie_jar_loader or _load_browser_cookie_jar
    try:
        jar = loader(browser)
        cookie_header = _cookie_header_for_ytmusic(jar)
    except Exception as exc:
        raise YTMusicBrowserAuthError(f"브라우저 쿠키를 읽을 수 없음: {exc}") from exc

    if not cookie_header:
        raise YTMusicBrowserAuthError("music.youtube.com 쿠키가 없음")
    if not _has_secure_3papisid(cookie_header):
        raise YTMusicBrowserAuthError("__Secure-3PAPISID 쿠키가 없음")

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


def _load_browser_cookie_jar(browser: str) -> Any:
    from yt_dlp.cookies import extract_cookies_from_browser

    return extract_cookies_from_browser(browser, logger=_SilentYtDlpLogger())


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


def _is_chromium_cookie_browser(browser: str) -> bool:
    return browser in {"brave", "chrome", "chromium", "edge", "opera", "vivaldi", "whale"}


class _SilentYtDlpLogger:
    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        return None

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        return None
