from pathlib import Path

import pytest

from cueforge.metadata.cover_art import CoverArtConfig, CoverArtProvider


class FakeResponse:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: int) -> FakeResponse:
        assert timeout == 15
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def test_cover_art_provider_selects_front_500_thumbnail(tmp_path: Path) -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "images": [
                        {
                            "front": False,
                            "image": "https://coverartarchive.org/release/rel/back.jpg",
                            "thumbnails": {"500": "https://coverartarchive.org/release/rel/back-500.jpg"},
                        },
                        {
                            "front": True,
                            "image": "https://coverartarchive.org/release/rel/front.jpg",
                            "thumbnails": {
                                "250": "https://coverartarchive.org/release/rel/front-250.jpg",
                                "500": "https://coverartarchive.org/release/rel/front-500.jpg",
                            },
                        },
                    ]
                }
            )
        ]
    )
    provider = CoverArtProvider(CoverArtConfig(cache_path=tmp_path / "cover.sqlite"), session=session)

    cover_url = provider.lookup("rel-1")

    assert cover_url == "https://coverartarchive.org/release/rel/front-500.jpg"
    assert session.headers["User-Agent"] == "CueForge/0.1.0"
    assert session.calls == ["https://coverartarchive.org/release/rel-1/"]


def test_cover_art_provider_falls_back_to_original_image(tmp_path: Path) -> None:
    session = FakeSession([FakeResponse({"images": [{"front": True, "image": "https://example.com/front.png"}]})])
    provider = CoverArtProvider(CoverArtConfig(cache_path=tmp_path / "cover.sqlite"), session=session)

    assert provider.lookup("rel-1") == "https://example.com/front.png"


def test_cover_art_provider_caches_missing_releases(tmp_path: Path) -> None:
    cache_path = tmp_path / "cover.sqlite"
    session = FakeSession([FakeResponse({}, status_code=404)])
    provider = CoverArtProvider(CoverArtConfig(cache_path=cache_path), session=session)

    first = provider.lookup("missing-release")
    second = provider.lookup("missing-release")

    assert first == ""
    assert second == ""
    assert len(session.calls) == 1


def test_cover_art_provider_raises_transient_errors_without_caching(tmp_path: Path) -> None:
    cache_path = tmp_path / "cover.sqlite"
    session = FakeSession([FakeResponse({}, status_code=503), FakeResponse({"images": []})])
    provider = CoverArtProvider(CoverArtConfig(cache_path=cache_path), session=session)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        provider.lookup("rel-1")
    assert provider.lookup("rel-1") == ""
    assert len(session.calls) == 2
