"""CueForge-owned OpenAI Codex OAuth helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from platformdirs import user_data_path
import requests

OPENAI_CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_CODEX_OAUTH_AUTH_URI = "https://auth.openai.com/oauth/authorize"
OPENAI_CODEX_OAUTH_TOKEN_URI = "https://auth.openai.com/oauth/token"
OPENAI_CODEX_MODELS_URI = "https://chatgpt.com/backend-api/codex/models"
OPENAI_CODEX_MODELS_CLIENT_VERSION = "99.99.99"
OPENAI_CODEX_USAGE_URI = "https://chatgpt.com/backend-api/wham/usage"
OPENAI_CODEX_OAUTH_SCOPE = "openid profile email offline_access"
OPENAI_CODEX_OAUTH_TOKEN_FILE_NAME = "openai_codex_oauth_token.json"
OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_HOST = "localhost"
OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PORT = 1455
OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PATH = "/auth/callback"
OPENAI_CODEX_OAUTH_REDIRECT_URI = (
    f"http://{OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_HOST}:"
    f"{OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PORT}{OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PATH}"
)
OPENAI_CODEX_OAUTH_LOCAL_TIMEOUT_SECONDS = 180
OPENAI_CODEX_OAUTH_TOKEN_KEYS = (
    "access_token",
    "refresh_token",
    "id_token",
    "expires_at",
    "expires_in",
    "scope",
    "token_type",
    "account_id",
)


@dataclass(frozen=True, slots=True)
class CodexOAuthCredentials:
    access_token: str
    account_id: str = ""


class OpenAICodexOAuthError(RuntimeError):
    """Raised when CueForge cannot use its own OpenAI Codex OAuth token."""


def default_openai_codex_oauth_token_path() -> Path:
    return user_data_path("CueForge") / OPENAI_CODEX_OAUTH_TOKEN_FILE_NAME


def build_openai_codex_oauth_authorization_url(
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    auth_uri: str = OPENAI_CODEX_OAUTH_AUTH_URI,
) -> str:
    query = urlencode(
        {
            "client_id": OPENAI_CODEX_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OPENAI_CODEX_OAUTH_SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return f"{auth_uri}?{query}"


def exchange_openai_codex_oauth_code(
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    session: Any | None = None,
    token_uri: str = OPENAI_CODEX_OAUTH_TOKEN_URI,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    http = session or requests
    try:
        response = http.post(
            token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": OPENAI_CODEX_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise OpenAICodexOAuthError(f"ChatGPT OAuth 토큰 요청 실패: {exc}") from exc
    token = _response_json(response, "ChatGPT OAuth 토큰 응답을 읽을 수 없음")
    if getattr(response, "status_code", 200) >= 400 or token.get("error"):
        message = token.get("error_description") or token.get("error") or getattr(response, "text", "")
        raise OpenAICodexOAuthError(f"ChatGPT OAuth 토큰 교환 실패: {message}")
    if not token.get("access_token"):
        raise OpenAICodexOAuthError("ChatGPT OAuth 토큰 응답에 access_token이 없습니다.")
    return token


def run_openai_codex_oauth_desktop_flow(
    *,
    open_browser: Callable[[str], object] | None = webbrowser.open,
    timeout_seconds: int = OPENAI_CODEX_OAUTH_LOCAL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    state = secrets.token_urlsafe(24)
    code_verifier = _pkce_code_verifier()
    code_challenge = _pkce_code_challenge(code_verifier)
    try:
        server = _OpenAICodexOAuthCallbackServer(
            (OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_HOST, OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PORT),
            _OpenAICodexOAuthCallbackHandler,
        )
    except OSError as exc:
        raise OpenAICodexOAuthError(
            f"ChatGPT OAuth 로컬 콜백 포트 {OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PORT}를 열 수 없습니다: {exc}"
        ) from exc
    server.timeout = timeout_seconds
    auth_url = build_openai_codex_oauth_authorization_url(
        redirect_uri=OPENAI_CODEX_OAUTH_REDIRECT_URI,
        state=state,
        code_challenge=code_challenge,
    )
    try:
        if open_browser:
            open_browser(auth_url)
        server.handle_request()
        result = server.result
    finally:
        server.server_close()
    if result is None:
        raise OpenAICodexOAuthError("ChatGPT OAuth 승인 시간이 초과되었습니다. 다시 연결해 주세요.")
    if result.get("error"):
        raise OpenAICodexOAuthError(str(result.get("error_description") or result["error"]))
    if result.get("state") != state:
        raise OpenAICodexOAuthError("ChatGPT OAuth state 검증에 실패했습니다. 다시 연결해 주세요.")
    code = str(result.get("code") or "")
    if not code:
        raise OpenAICodexOAuthError("ChatGPT OAuth 승인 코드가 비어 있습니다. 다시 연결해 주세요.")
    return exchange_openai_codex_oauth_code(
        code=code,
        redirect_uri=OPENAI_CODEX_OAUTH_REDIRECT_URI,
        code_verifier=code_verifier,
    )


def read_openai_codex_oauth_token(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OpenAICodexOAuthError(f"ChatGPT OAuth 토큰 파일을 읽을 수 없음: {exc}") from exc
    if not isinstance(payload, dict):
        raise OpenAICodexOAuthError("ChatGPT OAuth 토큰 파일 형식이 올바르지 않음")
    return payload


def write_openai_codex_oauth_token(token: dict[str, Any], path: Path) -> None:
    stored = {key: token[key] for key in OPENAI_CODEX_OAUTH_TOKEN_KEYS if key in token and token[key]}
    if "expires_at" not in stored and stored.get("expires_in"):
        stored["expires_at"] = int(time.time()) + int(stored["expires_in"])
    access_token = str(stored.get("access_token") or "")
    account_id = str(stored.get("account_id") or "").strip() or _account_id_from_token(access_token)
    if account_id:
        stored["account_id"] = account_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh_openai_codex_oauth_token_if_needed(
    token_path: Path,
    *,
    session: Any | None = None,
    token_uri: str = OPENAI_CODEX_OAUTH_TOKEN_URI,
    timeout_seconds: int = 45,
    min_valid_seconds: int = 300,
) -> dict[str, Any]:
    token = read_openai_codex_oauth_token(token_path)
    access_token = str(token.get("access_token") or "")
    if access_token and not _token_expiring_soon(token, min_valid_seconds=min_valid_seconds):
        return token
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not refresh_token:
        raise OpenAICodexOAuthError("ChatGPT OAuth refresh_token이 없어 계정을 다시 연결해야 합니다.")
    fresh = _refresh_openai_codex_oauth_token(
        refresh_token=refresh_token,
        session=session,
        token_uri=token_uri,
        timeout_seconds=timeout_seconds,
    )
    merged = {**token, **fresh}
    if not fresh.get("refresh_token"):
        merged["refresh_token"] = refresh_token
    write_openai_codex_oauth_token(merged, token_path)
    return read_openai_codex_oauth_token(token_path)


def load_openai_codex_oauth_credentials(
    token_path: Path | None = None,
    *,
    session: Any | None = None,
    token_uri: str = OPENAI_CODEX_OAUTH_TOKEN_URI,
    timeout_seconds: int = 45,
) -> CodexOAuthCredentials:
    path = token_path or default_openai_codex_oauth_token_path()
    if not path.exists():
        raise OpenAICodexOAuthError(f"ChatGPT 계정 연결이 필요합니다 ({path})")
    token = refresh_openai_codex_oauth_token_if_needed(
        path,
        session=session,
        token_uri=token_uri,
        timeout_seconds=timeout_seconds,
    )
    access_token = str(token.get("access_token") or "").strip()
    if not access_token:
        raise OpenAICodexOAuthError("ChatGPT OAuth 토큰에 access_token이 없습니다.")
    return CodexOAuthCredentials(access_token=access_token, account_id=_account_id_from_token_or_payload(token))


def fetch_openai_codex_usage(
    token_path: Path | None = None,
    *,
    session: Any | None = None,
    usage_uri: str = OPENAI_CODEX_USAGE_URI,
    token_uri: str = OPENAI_CODEX_OAUTH_TOKEN_URI,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    http = session or requests.Session()
    credentials = load_openai_codex_oauth_credentials(
        token_path or default_openai_codex_oauth_token_path(),
        session=http,
        token_uri=token_uri,
        timeout_seconds=timeout_seconds,
    )
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Accept": "application/json",
        "User-Agent": "CueForge/0.1",
    }
    if credentials.account_id:
        headers["ChatGPT-Account-ID"] = credentials.account_id
    try:
        response = http.get(usage_uri, headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        raise OpenAICodexOAuthError(f"Codex 사용량 조회 실패: {exc}") from exc
    payload = _response_json(response, "Codex 사용량 응답을 읽을 수 없음")
    if getattr(response, "status_code", 200) >= 400 or payload.get("error"):
        message = _error_message(payload) or getattr(response, "text", "")
        raise OpenAICodexOAuthError(f"Codex 사용량 조회 실패 ({getattr(response, 'status_code', '?')}): {message}")
    return payload


def fetch_openai_codex_models(
    token_path: Path | None = None,
    *,
    session: Any | None = None,
    models_uri: str = OPENAI_CODEX_MODELS_URI,
    token_uri: str = OPENAI_CODEX_OAUTH_TOKEN_URI,
    client_version: str = OPENAI_CODEX_MODELS_CLIENT_VERSION,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    http = session or requests.Session()
    credentials = load_openai_codex_oauth_credentials(
        token_path or default_openai_codex_oauth_token_path(),
        session=http,
        token_uri=token_uri,
        timeout_seconds=timeout_seconds,
    )
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Accept": "application/json",
        "User-Agent": f"codex_cli_rs/{client_version}",
    }
    if credentials.account_id:
        headers["ChatGPT-Account-ID"] = credentials.account_id
    try:
        response = http.get(models_uri, headers=headers, params={"client_version": client_version}, timeout=timeout_seconds)
    except Exception as exc:
        raise OpenAICodexOAuthError(f"Codex 모델 목록 조회 실패: {exc}") from exc
    payload = _response_json(response, "Codex 모델 목록 응답을 읽을 수 없음")
    if getattr(response, "status_code", 200) >= 400 or payload.get("error"):
        message = _error_message(payload) or getattr(response, "text", "")
        raise OpenAICodexOAuthError(f"Codex 모델 목록 조회 실패 ({getattr(response, 'status_code', '?')}): {message}")
    return payload


def openai_codex_model_ids(payload: dict[str, Any]) -> list[str]:
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    ids: list[str] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        if model.get("supported_in_api") is False:
            continue
        visibility = str(model.get("visibility") or "list").casefold()
        if visibility not in {"list", "visible", ""}:
            continue
        model_id = str(model.get("slug") or model.get("id") or "").strip()
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


def format_openai_codex_usage(payload: dict[str, Any], *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    parts: list[str] = []
    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    primary = _format_usage_window("5시간", rate_limit.get("primary_window"), now)
    secondary = _format_usage_window("주간", rate_limit.get("secondary_window"), now)
    if primary:
        parts.append(primary)
    if secondary:
        parts.append(secondary)

    credits = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
    credits_text = _format_credits(credits)
    if credits_text:
        parts.append(credits_text)

    additional = _format_additional_limits(payload.get("additional_rate_limits"), now)
    if additional:
        parts.append(additional)

    account_bits = [str(payload.get("email") or "").strip(), str(payload.get("plan_type") or "").strip()]
    account = " ".join(bit for bit in account_bits if bit)
    header = f"Codex 사용량 ({account})" if account else "Codex 사용량"
    if not parts:
        return f"{header}\n표시할 quota 정보가 없습니다."
    return "\n".join([header, *(f"- {part}" for part in parts)])


def openai_codex_oauth_account_label(token: dict[str, Any] | None) -> str:
    if not token:
        return ""
    id_payload = _jwt_payload(str(token.get("id_token") or ""))
    email = str(id_payload.get("email") or "").strip()
    name = str(id_payload.get("name") or "").strip()
    if email and name:
        return f"{email} ({name})"
    if email or name:
        return email or name
    account_id = _account_id_from_token_or_payload(token)
    return account_id or str(id_payload.get("sub") or "").strip()


def _refresh_openai_codex_oauth_token(
    *,
    refresh_token: str,
    session: Any | None,
    token_uri: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    http = session or requests
    try:
        response = http.post(
            token_uri,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": OPENAI_CODEX_OAUTH_CLIENT_ID,
            },
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise OpenAICodexOAuthError(f"ChatGPT OAuth 토큰 갱신 실패: {exc}") from exc
    payload = _response_json(response, "ChatGPT OAuth 토큰 갱신 응답을 읽을 수 없음")
    if getattr(response, "status_code", 200) >= 400 or payload.get("error"):
        message = payload.get("error_description") or payload.get("error") or getattr(response, "text", "")
        raise OpenAICodexOAuthError(f"ChatGPT OAuth 토큰 갱신 실패: {message}")
    if not payload.get("access_token"):
        raise OpenAICodexOAuthError("ChatGPT OAuth 갱신 응답에 access_token이 없습니다.")
    return payload


def _response_json(response: Any, error_prefix: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise OpenAICodexOAuthError(f"{error_prefix}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OpenAICodexOAuthError("ChatGPT OAuth 응답 형식이 올바르지 않음")
    return payload


def _error_message(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error.get("type") or "").strip()
    return str(error or payload.get("message") or "").strip()


def _format_usage_window(label: str, value: Any, now: datetime) -> str:
    if not isinstance(value, dict):
        return ""
    used_percent = _float_value(value.get("used_percent"))
    if used_percent is None:
        return ""
    left_percent = max(0.0, min(100.0, 100.0 - used_percent))
    reset = _format_reset(value, now)
    suffix = f", {reset}" if reset else ""
    return f"{label} {left_percent:.0f}% 남음{suffix}"


def _format_reset(value: dict[str, Any], now: datetime) -> str:
    reset_at = _float_value(value.get("reset_at"))
    if reset_at is not None:
        if reset_at > 10_000_000_000:
            reset_at = round(reset_at / 1000)
        reset = datetime.fromtimestamp(reset_at)
        if reset.date() == now.date():
            return f"{reset:%H:%M} 재설정"
        return f"{reset:%m/%d %H:%M} 재설정"
    reset_after_seconds = _float_value(value.get("reset_after_seconds"))
    if reset_after_seconds is not None:
        return f"{_format_duration(reset_after_seconds)} 후 재설정"
    return ""


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    if days:
        return f"{days}일 {hours}시간" if hours else f"{days}일"
    if hours:
        return f"{hours}시간 {minutes}분" if minutes else f"{hours}시간"
    return f"{minutes or 1}분"


def _format_credits(credits: dict[str, Any]) -> str:
    if not credits:
        return ""
    if credits.get("unlimited") is True:
        return "크레딧 무제한"
    balance = credits.get("balance")
    if balance not in (None, ""):
        try:
            shown = f"{float(balance):.0f}"
        except (TypeError, ValueError):
            shown = str(balance)
        return f"크레딧 {shown}"
    if credits.get("has_credits") is True:
        return "크레딧 사용 가능"
    return ""


def _format_additional_limits(value: Any, now: datetime) -> str:
    if not isinstance(value, list):
        return ""
    items: list[str] = []
    for item in value[:2]:
        if not isinstance(item, dict):
            continue
        rate_limit = item.get("rate_limit")
        if not isinstance(rate_limit, dict):
            continue
        name = str(item.get("limit_name") or item.get("metered_feature") or "추가 제한").strip()
        primary = _format_usage_window(name, rate_limit.get("primary_window"), now)
        if primary:
            items.append(primary)
    return "추가 " + " / ".join(items) if items else ""


def _float_value(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _token_expiring_soon(token: dict[str, Any], *, min_valid_seconds: int) -> bool:
    expires_at = token.get("expires_at")
    try:
        if expires_at:
            return int(expires_at) <= int(time.time()) + min_valid_seconds
    except (TypeError, ValueError):
        pass
    return _jwt_expiring_soon(str(token.get("access_token") or ""), skew_seconds=min_valid_seconds)


def _account_id_from_token_or_payload(token: dict[str, Any]) -> str:
    account_id = str(token.get("account_id") or "").strip()
    if account_id:
        return account_id
    return _account_id_from_token(str(token.get("access_token") or ""))


def _account_id_from_token(token: str) -> str:
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return str(auth.get("chatgpt_account_id") or "").strip()
    return ""


def _jwt_expiring_soon(token: str, *, skew_seconds: int = 300) -> bool:
    payload = _jwt_payload(token)
    exp = payload.get("exp")
    try:
        return int(exp) <= int(time.time()) + skew_seconds
    except (TypeError, ValueError):
        return False


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pkce_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _pkce_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class _OpenAICodexOAuthCallbackServer(HTTPServer):
    allow_reuse_address = True
    result: dict[str, str] | None = None


class _OpenAICodexOAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != OPENAI_CODEX_OAUTH_LOCAL_CALLBACK_PATH:
            self.send_error(404)
            return
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
        self.server.result = query  # type: ignore[attr-defined]
        body = (
            "<!doctype html><meta charset='utf-8'>"
            "<title>CueForge ChatGPT OAuth</title>"
            "<body style='font-family:Segoe UI,sans-serif;background:#111;color:#eee;padding:32px'>"
            "<h1>CueForge ChatGPT account connected</h1>"
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
