from cueforge.errors import user_facing_error
from cueforge.models import ErrorCategory


def test_dpapi_decrypt_failure_suggests_cookie_file() -> None:
    category, message = user_facing_error("ERROR: Failed to decrypt with DPAPI")

    assert category == ErrorCategory.COOKIE_COPY_FAILED
    assert "쿠키 파일" in message


def test_hyphenated_rate_limited_error_is_classified() -> None:
    category, message = user_facing_error("The current session has been rate-limited by YouTube")

    assert category == ErrorCategory.RATE_LIMITED
    assert "외부 서비스 제한" in message


def test_video_unavailable_error_is_non_recoverable() -> None:
    category, message = user_facing_error("ERROR: Video unavailable. This video is not available")

    assert category == ErrorCategory.VIDEO_UNAVAILABLE
    assert "다른 URL" in message
