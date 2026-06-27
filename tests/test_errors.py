from cueforge.errors import user_facing_error
from cueforge.models import ErrorCategory


def test_dpapi_decrypt_failure_suggests_cookie_file() -> None:
    category, message = user_facing_error("ERROR: Failed to decrypt with DPAPI")

    assert category == ErrorCategory.COOKIE_COPY_FAILED
    assert "쿠키 파일" in message
