from pathlib import Path

import pytest

from ytdj.metadata.fingerprint import (
    AcoustIDConfig,
    AcoustIDProvider,
    AudioFingerprint,
    FingerprintUnavailable,
    FpcalcFingerprinter,
)
from ytdj.metadata.matching import score_candidate
from ytdj.metadata.musicbrainz import MusicBrainzConfig, MusicBrainzProvider
from ytdj.metadata.ytmusic import YouTubeMusicProvider, extract_video_id
from ytdj.models import ReviewState, TrackMetadata


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


def test_fpcalc_fingerprinter_requires_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ytdj.runtime.shutil.which", lambda name: None)

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
