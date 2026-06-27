from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
    YTMusicCookieAuthConfig,
    YTMusicCookieAuthError,
    YTMusicOAuthAccount,
    YTMusicOAuthClient,
    YTMusicOAuthError,
    build_ytmusic_oauth_authorization_url,
    build_ytmusic_cookie_auth,
    exchange_ytmusic_oauth_code,
    fetch_ytmusic_oauth_account,
    google_oauth_account_label,
    load_ytmusic_oauth_client,
    read_ytmusic_oauth_account,
    write_ytmusic_oauth_account,
    write_ytmusic_oauth_token,
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


def test_youtube_music_cookie_file_auth_builds_headers(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".music.youtube.com\tTRUE\t/\tTRUE\t2147483647\t__Secure-3PAPISID\tsapisid\n"
        ".music.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsid\n",
        encoding="utf-8",
    )

    auth = build_ytmusic_cookie_auth(YTMusicCookieAuthConfig(cookie_file=cookie_file))

    assert "__Secure-3PAPISID=sapisid" in auth["Cookie"]
    assert auth["Authorization"].startswith("SAPISIDHASH ")
    assert auth["x-origin"] == "https://music.youtube.com"


def test_youtube_music_cookie_file_auth_requires_sapisid(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        ".music.youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tsid\n",
        encoding="utf-8",
    )

    with pytest.raises(YTMusicCookieAuthError, match="__Secure-3PAPISID"):
        build_ytmusic_cookie_auth(YTMusicCookieAuthConfig(cookie_file=cookie_file))


def test_youtube_music_provider_uses_cookie_file_auth_when_json_is_absent(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    auth_payload = {"Cookie": "__Secure-3PAPISID=sapisid", "Authorization": "SAPISIDHASH 0_0"}
    calls: list[dict[str, str] | None] = []

    provider = YouTubeMusicProvider(
        cookie_file=cookie_file,
        browser_auth_builder=lambda: auth_payload,
        client_factory=lambda auth: calls.append(auth) or FakeYTMusic(),
    )

    metadata = provider.lookup("https://music.youtube.com/watch?v=abc")

    assert calls == [auth_payload]
    assert metadata.title == "Song"


def test_youtube_music_provider_prefers_manual_auth_json(tmp_path: Path) -> None:
    auth_path = tmp_path / "browser.json"
    auth_path.write_text("{}", encoding="utf-8")
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    calls: list[object] = []

    provider = YouTubeMusicProvider(
        auth_path=auth_path,
        cookie_file=cookie_file,
        browser_auth_builder=lambda: {"Cookie": "__Secure-3PAPISID=sapisid"},
        client_factory=lambda auth: calls.append(auth) or FakeYTMusic(),
    )

    provider.lookup("abc")

    assert calls == [str(auth_path)]


def test_youtube_music_provider_prefers_oauth_token(tmp_path: Path) -> None:
    oauth_client_file = tmp_path / "google_oauth_client.json"
    oauth_client_file.write_text(
        '{"installed": {"client_id": "client.apps.googleusercontent.com", "client_secret": "secret"}}',
        encoding="utf-8",
    )
    oauth_token_file = tmp_path / "ytmusic_oauth_token.json"
    oauth_token_file.write_text(
        '{"access_token": "token", "refresh_token": "refresh", "expires_in": 3600, "expires_at": 9999999999, "scope": "https://www.googleapis.com/auth/youtube", "token_type": "Bearer"}',
        encoding="utf-8",
    )
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    calls: list[tuple[object, object]] = []

    provider = YouTubeMusicProvider(
        oauth_client_file=oauth_client_file,
        oauth_token_file=oauth_token_file,
        cookie_file=cookie_file,
        browser_auth_builder=lambda: {"Cookie": "__Secure-3PAPISID=sapisid"},
        client_factory=lambda auth, oauth_credentials=None: calls.append((auth, oauth_credentials)) or FakeYTMusic(),
    )

    provider.lookup("abc")

    assert calls[0][0] == str(oauth_token_file)
    assert calls[0][1].client_id == "client.apps.googleusercontent.com"


def test_youtube_music_oauth_client_loader_accepts_google_client_json(tmp_path: Path) -> None:
    client_file = tmp_path / "google_oauth_client.json"
    client_file.write_text(
        '{"installed": {"client_id": "client.apps.googleusercontent.com", "client_secret": "secret", "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}',
        encoding="utf-8",
    )

    client = load_ytmusic_oauth_client(client_file)

    assert client.client_id == "client.apps.googleusercontent.com"
    assert client.client_secret == "secret"
    assert client.auth_uri == "https://accounts.google.com/o/oauth2/v2/auth"
    assert client.token_uri == "https://oauth2.googleapis.com/token"


def test_youtube_music_oauth_authorization_url_uses_desktop_callback() -> None:
    client = YTMusicOAuthClient(
        client_id="client.apps.googleusercontent.com",
        client_secret="secret",
        source_path=Path("client.json"),
    )

    url = build_ytmusic_oauth_authorization_url(
        client,
        redirect_uri="http://127.0.0.1:12345/oauth/callback",
        state="state-token",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["client.apps.googleusercontent.com"]
    assert query["redirect_uri"] == ["http://127.0.0.1:12345/oauth/callback"]
    scopes = set(query["scope"][0].split())
    assert "https://www.googleapis.com/auth/youtube" in scopes
    assert "openid" in scopes
    assert "email" in scopes
    assert "profile" in scopes
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["state"] == ["state-token"]


def test_youtube_music_oauth_code_exchange_posts_desktop_payload() -> None:
    client = YTMusicOAuthClient(
        client_id="client.apps.googleusercontent.com",
        client_secret="secret",
        source_path=Path("client.json"),
        token_uri="https://oauth2.googleapis.com/token",
    )
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "token_type": "Bearer"}

    class Session:
        def post(self, url: str, *, data: dict[str, object], timeout: int) -> Response:
            calls.append({"url": url, "data": data, "timeout": timeout})
            return Response()

    token = exchange_ytmusic_oauth_code(
        client,
        code="auth-code",
        redirect_uri="http://127.0.0.1:12345/oauth/callback",
        session=Session(),
    )

    assert token["refresh_token"] == "refresh"
    assert calls == [
        {
            "url": "https://oauth2.googleapis.com/token",
            "data": {
                "code": "auth-code",
                "client_id": "client.apps.googleusercontent.com",
                "client_secret": "secret",
                "redirect_uri": "http://127.0.0.1:12345/oauth/callback",
                "grant_type": "authorization_code",
            },
            "timeout": 30,
        }
    ]


def test_youtube_music_oauth_code_exchange_reports_google_error() -> None:
    client = YTMusicOAuthClient(
        client_id="client.apps.googleusercontent.com",
        client_secret="secret",
        source_path=Path("client.json"),
    )

    class Response:
        status_code = 400
        text = ""

        def json(self) -> dict[str, str]:
            return {"error": "invalid_grant", "error_description": "Bad code"}

    class Session:
        def post(self, url: str, *, data: dict[str, object], timeout: int) -> Response:
            return Response()

    with pytest.raises(YTMusicOAuthError, match="Bad code"):
        exchange_ytmusic_oauth_code(
            client,
            code="bad-code",
            redirect_uri="http://127.0.0.1:12345/oauth/callback",
            session=Session(),
        )


def test_youtube_music_oauth_token_writer_adds_expiration(tmp_path: Path) -> None:
    token_file = tmp_path / "ytmusic_oauth_token.json"

    write_ytmusic_oauth_token(
        {
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/youtube",
            "token_type": "Bearer",
        },
        token_file,
    )

    payload = token_file.read_text(encoding="utf-8")

    assert '"expires_at"' in payload
    assert '"refresh_token": "refresh"' in payload


def test_youtube_music_oauth_token_writer_filters_openid_extras(tmp_path: Path) -> None:
    token_file = tmp_path / "ytmusic_oauth_token.json"

    write_ytmusic_oauth_token(
        {
            "access_token": "token",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/youtube openid email profile",
            "token_type": "Bearer",
            "id_token": "openid-token",
        },
        token_file,
    )

    payload = token_file.read_text(encoding="utf-8")

    assert '"id_token"' not in payload
    assert '"scope": "https://www.googleapis.com/auth/youtube openid email profile"' in payload


def test_youtube_music_oauth_account_fetch_and_label() -> None:
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {
                "email": "dj@example.com",
                "name": "Cue DJ",
                "picture": "https://example.com/avatar.png",
                "sub": "123",
            }

    class Session:
        def get(self, url: str, *, headers: dict[str, str], timeout: int) -> Response:
            calls.append({"url": url, "headers": headers, "timeout": timeout})
            return Response()

    account = fetch_ytmusic_oauth_account({"access_token": "access"}, session=Session())

    assert account.email == "dj@example.com"
    assert google_oauth_account_label(account) == "dj@example.com (Cue DJ)"
    assert calls[0]["headers"] == {"Authorization": "Bearer access"}


def test_youtube_music_oauth_account_round_trip(tmp_path: Path) -> None:
    account_file = tmp_path / "ytmusic_oauth_account.json"
    account = YTMusicOAuthAccount(email="dj@example.com", name="Cue DJ", picture="", sub="123")

    write_ytmusic_oauth_account(account, account_file)

    loaded = read_ytmusic_oauth_account(account_file)
    assert loaded == account


def test_youtube_music_provider_falls_back_when_cookie_file_auth_fails(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    logs: list[str] = []
    calls: list[object] = []

    provider = YouTubeMusicProvider(
        cookie_file=cookie_file,
        browser_auth_builder=lambda: (_ for _ in ()).throw(YTMusicCookieAuthError("no sapisid")),
        client_factory=lambda auth: calls.append(auth) or FakeYTMusic(),
        log=logs.append,
    )

    provider.lookup("abc")

    assert calls == [None]
    assert "YouTube Music 조회 시작: abc" in logs
    assert "YTMusic 인증: 쿠키 파일 읽는 중" in logs
    assert "YTMusic 쿠키 파일 인증 생략: no sapisid" in logs
    assert "YouTube Music 조회 완료" in logs


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
