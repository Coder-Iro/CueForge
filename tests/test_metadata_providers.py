from pathlib import Path

import pytest

from cueforge.metadata.fingerprint import (
    AcoustIDConfig,
    AcoustIDProvider,
    AudioFingerprint,
    FingerprintUnavailable,
    FpcalcFingerprinter,
)
from cueforge.metadata.matching import score_candidate
from cueforge.metadata.musicbrainz import MusicBrainzConfig, MusicBrainzProvider
from cueforge.metadata.ytmusic import YouTubeMusicProvider, extract_video_id
from cueforge.metadata.ytmusic_auth import (
    YTMusicBrowserAuthConfig,
    YTMusicBrowserAuthError,
    build_ytmusic_browser_auth,
)
from cueforge.models import ReviewState, TrackMetadata


class FakeYTMusic:
    def get_song(self, videoId: str) -> dict:
        assert videoId == "abc"
        return {
            "videoDetails": {"title": "Song", "author": "Artist"},
            "microformat": {"microformatDataRenderer": {"publishDate": "2026-05-01"}},
        }

    def get_watch_playlist(self, videoId: str, limit: int = 25) -> dict:
        return {
            "tracks": [
                {
                    "title": "Song",
                    "artists": [{"name": "Artist"}],
                    "album": {"name": "Album"},
                    "thumbnails": [{"url": "small", "width": 100, "height": 100}, {"url": "large", "width": 500, "height": 500}],
                }
            ]
        }


class FakeYTMusicGenericPrefix:
    def get_song(self, videoId: str) -> dict:
        return {"videoDetails": {"title": "보컬로이드 -개미관찰-", "author": "토우링고"}}

    def get_watch_playlist(self, videoId: str, limit: int = 25) -> dict:
        return {
            "tracks": [
                {
                    "title": "보컬로이드 -개미관찰-",
                    "artists": [{"name": "토우링고"}],
                }
            ]
        }


class FakeCookieJar:
    def __init__(self, cookie_header: str) -> None:
        self.cookie_header = cookie_header
        self.urls: list[str] = []

    def get_cookie_header(self, url: str) -> str:
        self.urls.append(url)
        return self.cookie_header


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls = 0

    def get(self, url: str, *, params: dict[str, str], timeout: int) -> FakeResponse:
        self.calls += 1
        assert "musicbrainz.org" in url
        assert params["fmt"] == "json"
        return FakeResponse(
            {
                "recordings": [
                    {
                        "id": "rec-1",
                        "title": "Song",
                        "score": 100,
                        "length": 180000,
                        "artist-credit": [{"name": "Artist"}],
                        "releases": [{"id": "rel-1", "title": "Album", "status": "Official", "date": "2026-05-01"}],
                        "isrcs": ["USABC260001"],
                        "genres": [{"name": "house", "count": 3}],
                    }
                ]
            }
        )


class FakePostSession:
    def __init__(self, score: float) -> None:
        self.headers: dict[str, str] = {}
        self.score = score
        self.calls: list[dict[str, str]] = []

    def post(self, url: str, *, data: dict[str, str], timeout: int) -> FakeResponse:
        assert "api.acoustid.org" in url
        assert timeout == 15
        self.calls.append(data)
        return FakeResponse(
            {
                "results": [
                    {
                        "id": "acoustid-1",
                        "score": self.score,
                        "recordings": [
                            {
                                "id": "rec-1",
                                "title": "Recognized Song",
                                "artists": [{"name": "Recognized Artist"}],
                                "releases": [
                                    {
                                        "id": "rel-1",
                                        "title": "Recognized Album",
                                        "status": "Official",
                                        "date": "2026-05-01",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )


class FakeFingerprinter:
    def fingerprint(self, audio_path: Path) -> AudioFingerprint:
        assert audio_path.name == "track.mp3"
        return AudioFingerprint(duration_seconds=180, fingerprint="abcdef")


class FakeRunResult:
    def __init__(self, *, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_extract_video_id_from_youtube_music_url() -> None:
    assert extract_video_id("https://music.youtube.com/watch?v=abc&list=xyz") == "abc"
    assert extract_video_id("abc") == "abc"


def test_youtube_music_provider_maps_track_metadata() -> None:
    provider = YouTubeMusicProvider(client=FakeYTMusic())

    metadata = provider.lookup("https://music.youtube.com/watch?v=abc")

    assert metadata.title == "Song"
    assert metadata.artist == "Artist"
    assert metadata.album == "Album"
    assert metadata.release_date == "2026-05-01"
    assert metadata.cover_url == "large"


def test_youtube_music_provider_uses_generic_prefix_as_title_cleanup_only() -> None:
    provider = YouTubeMusicProvider(client=FakeYTMusicGenericPrefix())

    metadata = provider.lookup("https://music.youtube.com/watch?v=abc")

    assert metadata.title == "개미관찰"
    assert metadata.artist == "토우링고"


def test_youtube_music_browser_auth_builds_ytmusicapi_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    unlock_calls: list[bool] = []
    monkeypatch.setattr("cueforge.metadata.ytmusic_auth.set_chromium_cookie_unlock_enabled", unlock_calls.append)
    jar = FakeCookieJar("SID=sid; __Secure-3PAPISID=sapisid; LOGIN_INFO=login")

    auth = build_ytmusic_browser_auth(
        YTMusicBrowserAuthConfig(cookie_browser="chrome", unlock_browser_cookie_database=True),
        cookie_jar_loader=lambda browser: jar,
    )

    assert unlock_calls == [True]
    assert jar.urls == ["https://music.youtube.com/"]
    assert auth is not None
    assert auth["Cookie"] == "SID=sid; __Secure-3PAPISID=sapisid; LOGIN_INFO=login"
    assert auth["Authorization"].startswith("SAPISIDHASH ")
    assert auth["x-origin"] == "https://music.youtube.com"


def test_youtube_music_browser_auth_requires_sapisid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cueforge.metadata.ytmusic_auth.set_chromium_cookie_unlock_enabled", lambda enabled: None)

    with pytest.raises(YTMusicBrowserAuthError, match="__Secure-3PAPISID"):
        build_ytmusic_browser_auth(
            YTMusicBrowserAuthConfig(cookie_browser="firefox"),
            cookie_jar_loader=lambda browser: FakeCookieJar("SID=sid"),
        )


def test_youtube_music_provider_uses_browser_cookie_auth_when_json_is_absent() -> None:
    auth_payload = {"Cookie": "__Secure-3PAPISID=sapisid", "Authorization": "SAPISIDHASH 0_0"}
    calls: list[dict[str, str] | None] = []

    provider = YouTubeMusicProvider(
        cookie_browser="chrome",
        browser_auth_builder=lambda: auth_payload,
        client_factory=lambda auth: calls.append(auth) or FakeYTMusic(),
    )

    metadata = provider.lookup("https://music.youtube.com/watch?v=abc")

    assert calls == [auth_payload]
    assert metadata.title == "Song"


def test_youtube_music_provider_prefers_manual_auth_json(tmp_path: Path) -> None:
    auth_path = tmp_path / "browser.json"
    auth_path.write_text("{}", encoding="utf-8")
    calls: list[object] = []

    provider = YouTubeMusicProvider(
        auth_path=auth_path,
        cookie_browser="chrome",
        browser_auth_builder=lambda: {"Cookie": "__Secure-3PAPISID=sapisid"},
        client_factory=lambda auth: calls.append(auth) or FakeYTMusic(),
    )

    provider.lookup("abc")

    assert calls == [str(auth_path)]


def test_youtube_music_provider_falls_back_when_browser_cookie_auth_fails() -> None:
    logs: list[str] = []
    calls: list[object] = []

    provider = YouTubeMusicProvider(
        cookie_browser="chrome",
        browser_auth_builder=lambda: (_ for _ in ()).throw(YTMusicBrowserAuthError("no sapisid")),
        client_factory=lambda auth: calls.append(auth) or FakeYTMusic(),
        log=logs.append,
    )

    provider.lookup("abc")

    assert calls == [None]
    assert logs == ["YTMusic 브라우저 쿠키 인증 생략: no sapisid"]


def test_musicbrainz_provider_scores_and_caches(tmp_path: Path) -> None:
    session = FakeSession()
    provider = MusicBrainzProvider(
        MusicBrainzConfig(cache_path=tmp_path / "mb.sqlite", rate_limit_seconds=0),
        session=session,
    )
    reference = TrackMetadata(title="Song", artist="Artist", album="Album", release_date="2026")

    first = provider.lookup(reference, duration_ms=181000)
    second = provider.lookup(reference, duration_ms=181000)

    assert session.calls == 1
    assert first[0].score >= 0.85
    assert first[0].review_state == ReviewState.AUTO_APPROVED
    assert first[0].metadata.genre == "house"
    assert second[0].metadata.musicbrainz_recording_id == "rec-1"


def test_score_candidate_marks_low_confidence_manual() -> None:
    candidate = score_candidate(
        TrackMetadata(title="Song A", artist="Artist A"),
        TrackMetadata(title="Different", artist="Artist B"),
        provider_score=0.2,
    )

    assert candidate.review_state == ReviewState.MANUAL_REQUIRED


def test_fpcalc_fingerprinter_parses_json(tmp_path: Path) -> None:
    fpcalc = tmp_path / "fpcalc.exe"
    fpcalc.write_text("", encoding="utf-8")

    def runner(args: list[str], *, capture_output: bool, text: bool, check: bool) -> FakeRunResult:
        assert args == [str(fpcalc), "-json", "track.mp3"]
        assert capture_output is True
        assert text is True
        assert check is False
        return FakeRunResult(stdout='{"duration": 180.4, "fingerprint": "abcdef"}')

    fingerprint = FpcalcFingerprinter(fpcalc, runner=runner).fingerprint(Path("track.mp3"))

    assert fingerprint.duration_seconds == 180
    assert fingerprint.fingerprint == "abcdef"


def test_fpcalc_fingerprinter_requires_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cueforge.runtime.shutil.which", lambda name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local-app-data"))

    with pytest.raises(FingerprintUnavailable, match="fpcalc"):
        FpcalcFingerprinter().fingerprint(Path("track.mp3"))


def test_acoustid_provider_maps_high_confidence_candidate() -> None:
    session = FakePostSession(score=0.96)
    provider = AcoustIDProvider(
        AcoustIDConfig(client_key="client-key", rate_limit_seconds=0),
        session=session,
        fingerprinter=FakeFingerprinter(),
    )

    candidates = provider.lookup(Path("track.mp3"))

    assert session.calls[0]["client"] == "client-key"
    assert session.calls[0]["duration"] == "180"
    assert candidates[0].review_state == ReviewState.AUTO_APPROVED
    assert candidates[0].metadata.title == "Recognized Song"
    assert candidates[0].metadata.artist == "Recognized Artist"
    assert candidates[0].metadata.musicbrainz_recording_id == "rec-1"
    assert candidates[0].raw["acoustid_score"] == 0.96


def test_acoustid_provider_keeps_mid_confidence_in_review() -> None:
    provider = AcoustIDProvider(
        AcoustIDConfig(client_key="client-key", rate_limit_seconds=0),
        session=FakePostSession(score=0.89),
        fingerprinter=FakeFingerprinter(),
    )

    candidate = provider.lookup(Path("track.mp3"))[0]

    assert candidate.score == 0.84
    assert candidate.review_state == ReviewState.REVIEW_REQUIRED


def test_acoustid_provider_requires_client_key() -> None:
    provider = AcoustIDProvider(
        AcoustIDConfig(client_key="", rate_limit_seconds=0),
        session=FakePostSession(score=0.96),
        fingerprinter=FakeFingerprinter(),
    )

    with pytest.raises(FingerprintUnavailable, match="client key"):
        provider.lookup(Path("track.mp3"))
