from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from cueforge.metadata.ytmusic import YouTubeMusicProvider, extract_video_id
from cueforge.metadata.ytmusic_auth import (
    YTMusicOAuthAccount,
    YTMusicOAuthClient,
    YTMusicOAuthError,
    build_ytmusic_oauth_authorization_url,
    exchange_ytmusic_oauth_code,
    fetch_ytmusic_oauth_account,
    find_ytmusic_oauth_client_file,
    google_oauth_account_label,
    load_ytmusic_oauth_client,
    read_ytmusic_oauth_account,
    write_ytmusic_oauth_account,
    write_ytmusic_oauth_token,
)
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


class FakeYTMusicSignedCover:
    def get_song(self, videoId: str) -> dict:
        return {"videoDetails": {"title": "Song", "author": "Artist"}}

    def get_watch_playlist(self, videoId: str, limit: int = 25) -> dict:
        return {
            "tracks": [
                {
                    "title": "Song",
                    "artists": [{"name": "Artist"}],
                    "thumbnails": [
                        {
                            "url": (
                                "https://tcj-image-production.s3.ap-northeast-1.amazonaws.com/u109312/r601214/ite601214.jpg"
                                "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20260617T113400Z"
                                "&X-Amz-Expires=86400&X-Amz-Signature=deadbeef&X-Amz-SignedHeaders=host:"
                            ),
                            "width": 1200,
                            "height": 1200,
                        }
                    ],
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


def test_youtube_music_provider_preserves_signed_cover_url_for_cache_stage() -> None:
    logs: list[str] = []
    provider = YouTubeMusicProvider(client=FakeYTMusicSignedCover(), log=logs.append)

    metadata = provider.lookup("https://music.youtube.com/watch?v=abc")

    assert metadata.title == "Song"
    assert metadata.artist == "Artist"
    assert "X-Amz-Signature" in metadata.cover_url
    assert metadata.cover_source == "YouTube Music thumbnail"
    assert "YouTube Music 조회 완료" in logs


def test_youtube_music_provider_skips_oauth_token_for_ytmusicapi(tmp_path: Path) -> None:
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
    calls: list[tuple[object, object]] = []
    logs: list[str] = []

    provider = YouTubeMusicProvider(
        oauth_client_file=oauth_client_file,
        oauth_token_file=oauth_token_file,
        client_factory=lambda auth, oauth_credentials=None: calls.append((auth, oauth_credentials)) or FakeYTMusic(),
        log=logs.append,
    )

    provider.lookup("abc")

    assert calls[0] == (None, None)
    assert "Google OAuth는 YouTube Data API 전용" in logs[1]


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


def test_youtube_music_oauth_client_finder_checks_pyinstaller_internal_config(tmp_path: Path) -> None:
    root = tmp_path / "CueForge"
    client_file = root / "_internal" / "config" / "google_oauth_client.json"
    client_file.parent.mkdir(parents=True)
    client_file.write_text(
        '{"installed": {"client_id": "client.apps.googleusercontent.com", "client_secret": "secret"}}',
        encoding="utf-8",
    )

    assert find_ytmusic_oauth_client_file(root) == client_file.resolve()


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
