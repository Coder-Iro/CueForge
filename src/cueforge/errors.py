"""User-facing error classification for job failures."""

from __future__ import annotations

from cueforge.models import ErrorCategory


def classify_error(error: object) -> ErrorCategory:
    message = str(error or "").casefold()
    if not message:
        return ErrorCategory.UNKNOWN
    if "unsupported" in message or "not a valid url" in message or "지원하지" in message:
        return ErrorCategory.UNSUPPORTED_URL
    if "ffmpeg" in message or "fpcalc" in message or "executable" in message and "not found" in message:
        return ErrorCategory.MISSING_DEPENDENCY
    if "sign in" in message or "login" in message or "private video" in message or "auth" in message:
        return ErrorCategory.AUTH_REQUIRED
    if "could not copy" in message and "cookie" in message and "database" in message:
        return ErrorCategory.COOKIE_COPY_FAILED
    if "failed to decrypt" in message and "dpapi" in message:
        return ErrorCategory.COOKIE_COPY_FAILED
    if "timeout" in message or "timed out" in message or "read timed out" in message:
        return ErrorCategory.NETWORK_TIMEOUT
    if "429" in message or "too many requests" in message or "rate limit" in message:
        return ErrorCategory.RATE_LIMITED
    if "file exists" in message or "permission denied" in message or "access is denied" in message:
        return ErrorCategory.FILE_CONFLICT
    if "tag" in message or "id3" in message or "mutagen" in message or "cover fetch" in message:
        return ErrorCategory.TAG_FAILED
    if "download" in message or "yt-dlp" in message or "youtube" in message or "soundcloud" in message:
        return ErrorCategory.DOWNLOAD_FAILED
    return ErrorCategory.UNKNOWN


def action_hint(category: ErrorCategory | str) -> str:
    value = ErrorCategory(category) if isinstance(category, str) and category in ErrorCategory._value2member_map_ else category
    return {
        ErrorCategory.UNSUPPORTED_URL: "지원하는 YouTube, YouTube Music, SoundCloud URL인지 확인하세요.",
        ErrorCategory.MISSING_DEPENDENCY: "설정에서 ffmpeg/fpcalc 경로를 확인하거나 초기 설정을 다시 열어 주세요.",
        ErrorCategory.AUTH_REQUIRED: "계정 접근이 필요하면 설정에서 Google 계정을 연결하거나 쿠키 파일/YTMusic 인증 JSON을 지정한 뒤 재시도하세요.",
        ErrorCategory.COOKIE_COPY_FAILED: "브라우저 쿠키 직접 읽기는 지원하지 않습니다. Netscape 형식 쿠키 파일을 지정한 뒤 재시도하세요.",
        ErrorCategory.NETWORK_TIMEOUT: "네트워크 상태를 확인하고 잠시 뒤 재시도하세요.",
        ErrorCategory.RATE_LIMITED: "외부 서비스 제한에 걸렸습니다. 잠시 기다린 뒤 재시도하세요.",
        ErrorCategory.DOWNLOAD_FAILED: "URL 접근 가능 여부와 yt-dlp/쿠키 파일 설정을 확인하세요.",
        ErrorCategory.TAG_FAILED: "커버 URL과 파일 권한을 확인한 뒤 재시도하세요.",
        ErrorCategory.FILE_CONFLICT: "출력 폴더 권한, 파일명 충돌, 열려 있는 파일을 확인하세요.",
        ErrorCategory.UNKNOWN: "진단 정보를 복사해 원인 메시지와 함께 확인하세요.",
    }.get(value, "진단 정보를 복사해 원인 메시지와 함께 확인하세요.")


def user_facing_error(error: object) -> tuple[ErrorCategory, str]:
    category = classify_error(error)
    message = str(error or "").strip() or "알 수 없는 오류"
    return category, f"{message}\n{action_hint(category)}"
