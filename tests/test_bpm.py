import pytest

from cueforge.metadata.bpm import (
    GetSongBpmAuthenticationError,
    GetSongBpmConfig,
    GetSongBpmProvider,
    native_bpm_candidate,
    parse_bpm,
)
from cueforge.models import TrackMetadata
from cueforge.sources import SourcePlatform


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeStatusResponse(FakeResponse):
    def __init__(self, status_code: int) -> None:
        super().__init__({})
        self.status_code = status_code

    def raise_for_status(self) -> None:
        raise AssertionError("custom auth error should be raised before raw HTTPError")


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.headers: dict[str, str] = {}
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str], int]] = []

    def get(self, url: str, *, params: dict[str, str], timeout: int) -> FakeResponse:
        self.calls.append((url, params, timeout))
        return FakeResponse(self.payload)


class FakeStatusSession(FakeSession):
    def __init__(self, status_code: int) -> None:
        super().__init__({})
        self.status_code = status_code

    def get(self, url: str, *, params: dict[str, str], timeout: int) -> FakeStatusResponse:
        self.calls.append((url, params, timeout))
        return FakeStatusResponse(self.status_code)


class FakeRetrySession(FakeSession):
    def __init__(self) -> None:
        super().__init__(
            {
                "search": [
                    {
                        "song_title": "Song",
                        "artist": "Artist",
                        "tempo": 128,
                    }
                ]
            }
        )

    def get(self, url: str, *, params: dict[str, str], timeout: int) -> FakeResponse:
        self.calls.append((url, params, timeout))
        if len(self.calls) == 1:
            return FakeStatusResponse(401)
        return FakeResponse(self.payload)


def test_parse_bpm_passthrough_and_decimal_rounding() -> None:
    assert parse_bpm(64) == 64
    assert parse_bpm(220) == 220
    assert parse_bpm(174) == 174
    assert parse_bpm(128.6) == 129


def test_native_bpm_candidate_uses_platform_source() -> None:
    candidate = native_bpm_candidate({"bpm": 128}, SourcePlatform.SOUNDCLOUD)

    assert candidate is not None
    assert candidate.metadata.bpm == 128
    assert candidate.metadata.bpm_source == "native:soundcloud"
    assert candidate.metadata.bpm_confidence == 1.0


def test_getsongbpm_strict_match_returns_candidate() -> None:
    session = FakeSession(
        {
            "search": [
                {
                    "song_title": "Song",
                    "artist": {"name": "Artist"},
                    "album": {"name": "Album"},
                    "year": "2026",
                    "tempo": 128.6,
                }
            ]
        }
    )
    provider = GetSongBpmProvider(GetSongBpmConfig(client_key="api-key", timeout_seconds=7), session=session)

    candidates = provider.lookup(
        TrackMetadata(title="Song", artist="Artist", album="Album", release_date="2026"),
        info={},
        platform=SourcePlatform.YOUTUBE,
    )

    assert session.headers["X-API-KEY"] == "api-key"
    assert session.calls[0][1]["type"] == "both"
    assert session.calls[0][1]["lookup"] == "song:Song artist:Artist"
    assert session.calls[0][1]["limit"] == "5"
    assert session.calls[0][2] == 7
    assert candidates[0].score >= 0.85
    assert candidates[0].metadata.bpm == 129
    assert candidates[0].metadata.bpm_source == "GetSongBPM"
    assert candidates[0].metadata.bpm_confidence == candidates[0].score


def test_getsongbpm_low_match_remains_below_strict_threshold() -> None:
    provider = GetSongBpmProvider(
        GetSongBpmConfig(client_key="api-key"),
        session=FakeSession({"search": [{"song_title": "Different", "artist": "Other", "tempo": 140}]}),
    )

    candidates = provider.lookup(TrackMetadata(title="Song", artist="Artist"), info={}, platform=SourcePlatform.YOUTUBE)

    assert candidates
    assert candidates[0].score < 0.85


def test_getsongbpm_missing_key_does_not_request() -> None:
    session = FakeSession({"search": [{"song_title": "Song", "artist": "Artist", "tempo": 128}]})
    provider = GetSongBpmProvider(GetSongBpmConfig(client_key=""), session=session)

    candidates = provider.lookup(TrackMetadata(title="Song", artist="Artist"), info={}, platform=SourcePlatform.YOUTUBE)

    assert candidates == []
    assert session.calls == []


def test_getsongbpm_auth_failure_has_actionable_error() -> None:
    session = FakeStatusSession(401)
    provider = GetSongBpmProvider(GetSongBpmConfig(client_key="bad-key"), session=session)

    with pytest.raises(GetSongBpmAuthenticationError, match="API 키"):
        provider.lookup(TrackMetadata(title="Song", artist="Artist"), info={}, platform=SourcePlatform.YOUTUBE)

    assert "api_key" not in session.calls[0][1]
    assert session.calls[1][1]["api_key"] == "bad-key"


def test_getsongbpm_retries_with_api_key_param_when_header_is_rejected() -> None:
    session = FakeRetrySession()
    provider = GetSongBpmProvider(GetSongBpmConfig(client_key="api-key"), session=session)

    candidates = provider.lookup(TrackMetadata(title="Song", artist="Artist"), info={}, platform=SourcePlatform.YOUTUBE)

    assert "api_key" not in session.calls[0][1]
    assert session.calls[1][1]["api_key"] == "api-key"
    assert candidates[0].metadata.bpm == 128


def test_native_bpm_wins_over_getsongbpm() -> None:
    session = FakeSession({"search": [{"song_title": "Song", "artist": "Artist", "tempo": 140}]})
    provider = GetSongBpmProvider(GetSongBpmConfig(client_key="api-key"), session=session)

    candidates = provider.lookup(
        TrackMetadata(title="Song", artist="Artist"),
        info={"track": {"bpm": 174}},
        platform=SourcePlatform.SOUNDCLOUD,
    )

    assert candidates[0].provider == "bpm_native"
    assert candidates[0].metadata.bpm == 174
    assert session.calls == []
