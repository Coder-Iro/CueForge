from pathlib import Path

import pytest

from cueforge.artwork import cache_cover_url, fetch_cover_url


class FakeResponse:
    def __init__(self, content: bytes, content_type: str = "image/jpeg", status_error: Exception | None = None) -> None:
        self.content = content
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(content))}
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[str] = []

    def get(self, url: str, timeout: int) -> FakeResponse:
        self.calls.append(url)
        assert timeout == 15
        return self.response


def test_cache_cover_url_writes_valid_image_to_local_cache(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse(b"image-bytes", "image/png"))

    cached = cache_cover_url("https://example.com/cover.png?token=short", cache_dir=tmp_path, cache_key="job-1", session=session)

    assert cached.path.exists()
    assert cached.path.read_bytes() == b"image-bytes"
    assert cached.mime == "image/png"
    assert cached.path.name.startswith("job-1-")
    assert cached.path.suffix == ".png"


def test_fetch_cover_url_rejects_non_image_content() -> None:
    session = FakeSession(FakeResponse(b"<html></html>", "text/html"))

    with pytest.raises(ValueError, match="non-image"):
        fetch_cover_url("https://example.com/cover.jpg", session=session)
