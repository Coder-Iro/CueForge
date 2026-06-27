"""Authentication helpers for ytmusicapi."""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

from platformdirs import user_data_path
import requests

YTMUSIC_ORIGIN = "https://music.youtube.com"
YTMUSIC_COOKIE_URL = f"{YTMUSIC_ORIGIN}/"
OAUTH_CLIENT_ENV_VAR = "CUEFORGE_GOOGLE_OAUTH_CLIENT"
OAUTH_ACCOUNT_FILE_NAME = "ytmusic_oauth_account.json"
OAUTH_TOKEN_FILE_NAME = "ytmusic_oauth_token.json"
YTMUSIC_OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube openid email profile"
GOOGLE_OAUTH_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
OAUTH_LOCAL_CALLBACK_PATH = "/oauth/callback"
OAUTH_LOCAL_TIMEOUT_SECONDS = 180
YTMUSIC_TOKEN_KEYS = ("access_token", "refresh_token", "expires_at", "expires_in", "scope", "token_type")


@dataclass(slots=True, frozen=True)
class YTMusicCookieAuthConfig:
    cookie_file: Path


@dataclass(slots=True, frozen=True)
class YTMusicOAuthClient:
    client_id: str
    client_secret: str
    source_path: Path
    auth_uri: str = GOOGLE_OAUTH_AUTH_URI
    token_uri: str = GOOGLE_OAUTH_TOKEN_URI


@dataclass(slots=True, frozen=True)
class YTMusicOAuthAccount:
    email: str = ""
    name: str = ""
    picture: str = ""
    sub: str = ""


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


def default_ytmusic_oauth_account_path() -> Path:
    return user_data_path("CueForge") / OAUTH_ACCOUNT_FILE_NAME


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
    return YTMusicOAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        source_path=path,
        auth_uri=str(client.get("auth_uri") or GOOGLE_OAUTH_AUTH_URI).strip() or GOOGLE_OAUTH_AUTH_URI,
        token_uri=str(client.get("token_uri") or GOOGLE_OAUTH_TOKEN_URI).strip() or GOOGLE_OAUTH_TOKEN_URI,
    )


def build_ytmusic_oauth_credentials(client: YTMusicOAuthClient) -> Any:
    from ytmusicapi import OAuthCredentials

    return OAuthCredentials(client_id=client.client_id, client_secret=client.client_secret)


def build_ytmusic_oauth_authorization_url(client: YTMusicOAuthClient, *, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YTMUSIC_OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{client.auth_uri}?{query}"


def exchange_ytmusic_oauth_code(
    client: YTMusicOAuthClient,
    *,
    code: str,
    redirect_uri: str,
    session: Any | None = None,
) -> dict[str, Any]:
    http = session or requests
    try:
        response = http.post(
            client.token_uri,
            data={
                "code": code,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
    except Exception as exc:
        raise YTMusicOAuthError(f"OAuth 토큰 요청 실패: {exc}") from exc
    try:
        token = response.json()
    except Exception as exc:
        raise YTMusicOAuthError(f"OAuth 토큰 응답을 읽을 수 없음: {exc}") from exc
    if not isinstance(token, dict):
        raise YTMusicOAuthError("OAuth 토큰 응답 형식이 올바르지 않음")
    if response.status_code >= 400 or token.get("error"):
        message = token.get("error_description") or token.get("error") or response.text
        raise YTMusicOAuthError(f"OAuth 토큰 교환 실패: {message}")
    if not token.get("access_token") or not token.get("refresh_token"):
        raise YTMusicOAuthError("OAuth 토큰 응답에 access_token 또는 refresh_token이 없습니다.")
    return token


def run_ytmusic_oauth_desktop_flow(
    client: YTMusicOAuthClient,
    *,
    open_browser: Callable[[str], object] | None = webbrowser.open,
    timeout_seconds: int = OAUTH_LOCAL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    state = secrets.token_urlsafe(24)
    server = _OAuthCallbackServer(("127.0.0.1", 0), _OAuthCallbackHandler)
    server.timeout = timeout_seconds
    redirect_uri = f"http://127.0.0.1:{server.server_port}{OAUTH_LOCAL_CALLBACK_PATH}"
    auth_url = build_ytmusic_oauth_authorization_url(client, redirect_uri=redirect_uri, state=state)
    try:
        if open_browser:
            open_browser(auth_url)
        server.handle_request()
        result = server.result
    finally:
        server.server_close()
    if result is None:
        raise YTMusicOAuthError("OAuth 승인 시간이 초과되었습니다. 다시 연결해 주세요.")
    if result.get("error"):
        raise YTMusicOAuthError(str(result.get("error_description") or result["error"]))
    if result.get("state") != state:
        raise YTMusicOAuthError("OAuth state 검증에 실패했습니다. 다시 연결해 주세요.")
    code = str(result.get("code") or "")
    if not code:
        raise YTMusicOAuthError("OAuth 승인 코드가 비어 있습니다. 다시 연결해 주세요.")
    return exchange_ytmusic_oauth_code(client, code=code, redirect_uri=redirect_uri)


def fetch_ytmusic_oauth_account(token: dict[str, Any], *, session: Any | None = None) -> YTMusicOAuthAccount:
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise YTMusicOAuthError("OAuth access_token이 없어 Google 계정을 확인할 수 없습니다.")
    http = session or requests
    try:
        response = http.get(
            GOOGLE_OAUTH_USERINFO_URI,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
    except Exception as exc:
        raise YTMusicOAuthError(f"Google 계정 정보 요청 실패: {exc}") from exc
    try:
        payload = response.json()
    except Exception as exc:
        raise YTMusicOAuthError(f"Google 계정 정보 응답을 읽을 수 없음: {exc}") from exc
    if not isinstance(payload, dict):
        raise YTMusicOAuthError("Google 계정 정보 응답 형식이 올바르지 않음")
    if response.status_code >= 400 or payload.get("error"):
        message = payload.get("error_description") or payload.get("error") or response.text
        raise YTMusicOAuthError(f"Google 계정 정보 확인 실패: {message}")
    account = YTMusicOAuthAccount(
        email=str(payload.get("email") or "").strip(),
        name=str(payload.get("name") or "").strip(),
        picture=str(payload.get("picture") or "").strip(),
        sub=str(payload.get("sub") or "").strip(),
    )
    if not account.email and not account.name and not account.sub:
        raise YTMusicOAuthError("Google 계정 정보에 식별할 수 있는 값이 없습니다.")
    return account


def google_oauth_account_label(account: YTMusicOAuthAccount | None) -> str:
    if not account:
        return ""
    if account.email and account.name:
        return f"{account.email} ({account.name})"
    return account.email or account.name or account.sub


def read_ytmusic_oauth_account(path: Path) -> YTMusicOAuthAccount | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return YTMusicOAuthAccount(
        email=str(payload.get("email") or "").strip(),
        name=str(payload.get("name") or "").strip(),
        picture=str(payload.get("picture") or "").strip(),
        sub=str(payload.get("sub") or "").strip(),
    )


def write_ytmusic_oauth_account(account: YTMusicOAuthAccount, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(account), ensure_ascii=False, indent=2), encoding="utf-8")


def write_ytmusic_oauth_token(token: dict[str, Any], path: Path) -> None:
    token = {key: token[key] for key in YTMUSIC_TOKEN_KEYS if key in token}
    if "expires_at" not in token and token.get("expires_in"):
        token = {**token, "expires_at": int(time.time()) + int(token["expires_in"])}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")


class _OAuthCallbackServer(HTTPServer):
    result: dict[str, str] | None = None


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != OAUTH_LOCAL_CALLBACK_PATH:
            self.send_error(404)
            return
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
        self.server.result = query  # type: ignore[attr-defined]
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>CueForge Google OAuth</title>"
            "<body style='font-family:Segoe UI,sans-serif;background:#111;color:#eee;padding:32px'>"
            "<h1>CueForge Google account connected</h1>"
            "<p>You can close this browser tab and return to CueForge.</p>"
            "</body>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


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
