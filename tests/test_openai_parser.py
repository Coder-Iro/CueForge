import base64
import json
import time
from urllib.parse import parse_qs, urlparse

from cueforge.metadata.openai_parser import OpenAIMetadataConfig, OpenAIMetadataSuggester
from cueforge.metadata.openai_oauth import (
    OPENAI_CODEX_OAUTH_REDIRECT_URI,
    build_openai_codex_oauth_authorization_url,
    fetch_openai_codex_models,
    format_openai_codex_usage,
    openai_codex_model_ids,
    write_openai_codex_oauth_token,
)
from cueforge.models import TrackMetadata


class FakeResponse:
    def __init__(self, payload: dict, *, headers: dict | None = None, status_code: int = 200, text: str = "") -> None:
        self.payload = payload
        self.headers = headers or {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeStreamingResponse(FakeResponse):
    def __init__(self, lines: list[str], *, headers: dict | None = None) -> None:
        super().__init__({}, headers=headers)
        self.lines = lines

    def iter_lines(self, decode_unicode: bool = False):
        for line in self.lines:
            yield line if decode_unicode else line.encode("utf-8")
        raise AssertionError("stream was read after terminal response event")


class FakeSession:
    def __init__(
        self,
        payload: dict,
        *,
        response_headers: dict | None = None,
        response_status: int = 200,
        response_text: str = "",
    ) -> None:
        self.payload = payload
        self.response_headers = response_headers or {}
        self.response_status = response_status
        self.response_text = response_text
        self.calls: list[dict] = []

    def post(self, url: str, *, headers: dict, json: dict, timeout: int, stream: bool = False) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout, "stream": stream})
        return FakeResponse(
            self.payload,
            headers=self.response_headers,
            status_code=self.response_status,
            text=self.response_text,
        )

    def get(self, url: str, *, headers: dict, timeout: int, params: dict | None = None) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout, "params": params})
        return FakeResponse(self.payload)


class FakeStreamingSession(FakeSession):
    def __init__(self, lines: list[str]) -> None:
        super().__init__({})
        self.lines = lines

    def post(self, url: str, *, headers: dict, json: dict, timeout: int, stream: bool = False) -> FakeStreamingResponse:
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout, "stream": stream})
        return FakeStreamingResponse(self.lines)


def test_openai_metadata_suggester_builds_review_candidate_with_bpm(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    session = FakeSession(
        {
            "output_text": (
                '{"title":"Song","artist":"Artist","album":"Album","album_artist":null,'
                '"genre":"House","release_date":"2026-05-01","label":null,"isrc":null,'
                '"bpm":128,"cover_url":null,"confidence":0.93,'
                '"matched_fields":["title","artist","album","bpm"],'
                '"reason":"Matched official release metadata.","source_urls":["https://example.com/song"],'
                '"bpm_source_url":"https://example.com/song"}'
            )
        },
        response_headers={
            "x-codex-plan-type": "pro",
            "x-codex-primary-used-percent": "26",
            "x-codex-primary-reset-after-seconds": "12180",
            "x-codex-secondary-used-percent": "5",
            "x-codex-secondary-reset-after-seconds": "321565",
            "x-codex-credits-has-credits": "False",
            "x-codex-credits-unlimited": "False",
            "x-codex-bengalfox-primary-used-percent": "0",
            "x-codex-bengalfox-primary-reset-after-seconds": "18000",
            "x-codex-bengalfox-secondary-used-percent": "0",
            "x-codex-bengalfox-secondary-reset-after-seconds": "604800",
            "x-codex-bengalfox-limit-name": "GPT-5.3-Codex-Spark",
        },
    )
    suggester = OpenAIMetadataSuggester(
        OpenAIMetadataConfig(auth_path=token_path, model="gpt-test"),
        session=session,
    )

    candidates = suggester.suggest(
        info={"title": "Noisy video", "uploader": "Uploader", "webpage_url": "https://youtu.be/abc"},
        reference=TrackMetadata(title="Noisy video", artist="Uploader"),
        candidates=[],
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.provider == "chatgpt"
    assert candidate.metadata.title == "Song"
    assert candidate.metadata.artist == "Artist"
    assert candidate.metadata.bpm == 128
    assert candidate.score == 0.84
    assert candidate.raw["prefer_initial_metadata"] is True
    assert candidate.raw["source_urls"] == ["https://example.com/song"]
    assert "Codex 사용량 (pro)" in candidate.raw["quota_status"]
    assert "5시간 74% 남음" in candidate.raw["quota_status"]
    assert "주간 95% 남음" in candidate.raw["quota_status"]
    assert "GPT-5.3-Codex-Spark 100% 남음" in candidate.raw["quota_status"]
    assert session.calls[0]["json"]["reasoning"] == {"effort": "medium"}
    assert session.calls[0]["json"]["tools"] == [{"type": "web_search", "search_context_size": "high"}]
    assert "음악 메타데이터 편집자" in session.calls[0]["json"]["instructions"]
    assert "BPM은 중요한 DJ 태그" in session.calls[0]["json"]["instructions"]
    assert "원곡이 아니라 업로드된 버전 자체" in session.calls[0]["json"]["instructions"]
    assert "공식 싱글 또는 앨범 아트워크" in session.calls[0]["json"]["instructions"]
    assert "출처 언어 표기" in session.calls[0]["json"]["instructions"]
    assert "대표 표기 하나만" in session.calls[0]["json"]["instructions"]
    assert "album과 album_artist를 적극적으로 확인" in session.calls[0]["json"]["instructions"]
    prompt_context = json.loads(session.calls[0]["json"]["input"][0]["content"])
    assert prompt_context["task"]["goal"] == "실제로 들리는 업로드 녹음에 대한 정규화된 음악 태그 메타데이터를 반환하세요."
    assert "YouTube/웹페이지 표시 제목" in prompt_context["task"]["not_goal"]
    assert "title" in prompt_context["field_policy"]
    assert any("source.title" in rule and "그대로 반환하지 마세요" in rule for rule in prompt_context["field_policy"]["title"])
    assert any("업로드된 녹음/버전" in rule for rule in prompt_context["field_policy"]["bpm"])
    assert any("원곡/작품/곡 BPM을 복사하지 마세요" in rule for rule in prompt_context["field_policy"]["bpm"])
    assert any("공식 싱글 또는 앨범 아트워크" in rule for rule in prompt_context["field_policy"]["cover_url"])
    assert any("번역, 로마자화, 음역하지 마세요" in rule for rule in prompt_context["field_policy"]["artist"])
    assert any("텐코 시부키 TENKO SHIBUKI" in rule for rule in prompt_context["field_policy"]["artist"])
    assert any("공식 싱글 발매" in rule for rule in prompt_context["field_policy"]["album_album_artist"])
    assert any("앨범 수록곡" in rule for rule in prompt_context["field_policy"]["album_album_artist"])
    assert any('"Noisy video" "Uploader" album' in query for query in prompt_context["suggested_release_search_queries"])
    assert any('"Noisy video" "Uploader" album artist' in query for query in prompt_context["suggested_release_search_queries"])
    assert any('"Noisy video" "Uploader" Apple Music' in query for query in prompt_context["suggested_release_search_queries"])
    assert any('"Noisy video" BPM' in query for query in prompt_context["suggested_bpm_search_queries"])
    assert any("파서 가설" in rule for rule in prompt_context["candidate_policy"])
    assert "max_output_tokens" not in session.calls[0]["json"]
    assert session.calls[0]["headers"]["ChatGPT-Account-ID"] == "acct_test"
    assert session.calls[0]["stream"] is True


def test_openai_metadata_suggester_keeps_cover_url_for_cache_stage_while_prompt_discourages_temporary_urls(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    signed_url = (
        "https://tcj-image-production.s3.ap-northeast-1.amazonaws.com/u109312/r601214/ite601214.jpg"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date=20260617T113400Z"
        "&X-Amz-Expires=86400&X-Amz-Signature=deadbeef&X-Amz-SignedHeaders=host:"
    )
    session = FakeSession(
        {
            "output_text": json.dumps(
                {
                    "title": "Song",
                    "artist": "Artist",
                    "album": None,
                    "album_artist": None,
                    "genre": "Music",
                    "release_date": None,
                    "label": None,
                    "isrc": None,
                    "bpm": None,
                    "cover_url": signed_url,
                    "confidence": 0.82,
                    "matched_fields": ["title", "artist", "cover_url"],
                    "reason": "Found metadata, but artwork URL is temporary.",
                    "source_urls": ["https://example.com/song"],
                    "bpm_source_url": None,
                }
            )
        }
    )
    suggester = OpenAIMetadataSuggester(OpenAIMetadataConfig(auth_path=token_path, model="gpt-test"), session=session)

    candidates = suggester.suggest(info={"title": "Noisy"}, reference=TrackMetadata(), candidates=[])

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.metadata.cover_url == signed_url.rstrip(":")
    assert "cover_url" in candidate.matched_fields
    request = session.calls[0]["json"]
    assert "temporary 아트워크 URL은 반환하지 마세요" in request["instructions"]
    prompt_context = json.loads(request["input"][0]["content"])
    assert any("X-Amz" in rule for rule in prompt_context["field_policy"]["cover_url"])


def test_openai_metadata_suggester_includes_bpm_search_queries_in_primary_request(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    session = FakeSession(
        {
            "output_text": (
                '{"title":"Cover Song","artist":"Cover Singer","album":null,"album_artist":null,'
                '"genre":"Music","release_date":null,"label":null,"isrc":null,'
                '"bpm":null,"cover_url":null,"confidence":0.78,'
                '"matched_fields":["title","artist"],'
                '"reason":"Normalized cover upload metadata.","source_urls":["https://example.com/meta"],'
                '"bpm_source_url":null}'
            )
        }
    )
    logs: list[str] = []
    suggester = OpenAIMetadataSuggester(OpenAIMetadataConfig(auth_path=token_path, model="gpt-test"), session=session)

    candidates = suggester.suggest(
        info={
            "title": "Cover Song | Cover Singer COVER",
            "uploader": "Cover Singer",
            "webpage_url": "https://youtu.be/abc",
            "description": "Original: Original Artist - Cover Song",
        },
        reference=TrackMetadata(title="Cover Song", artist="Cover Singer"),
        candidates=[],
        log=logs.append,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.metadata.bpm is None
    assert len(session.calls) == 1
    assert not any("BPM 보강" in message for message in logs)
    prompt_context = json.loads(session.calls[0]["json"]["input"][0]["content"])
    assert any('"Cover Song" BPM' in query for query in prompt_context["suggested_bpm_search_queries"])
    assert any('"Cover Song" BPM テンポ' in query for query in prompt_context["suggested_bpm_search_queries"])
    assert any('"Cover Song" "Cover Singer" cover BPM' in query for query in prompt_context["suggested_bpm_search_queries"])
    assert prompt_context["source"]["probable_cover_upload"] is True
    assert not any("原曲BPM" in query for query in prompt_context["suggested_bpm_search_queries"])
    assert any('"Cover Song" "Cover Singer" cover release' in query for query in prompt_context["suggested_release_search_queries"])
    assert not any("Apple Music" in query for query in prompt_context["suggested_release_search_queries"])
    assert session.calls[0]["json"]["text"]["format"]["name"] == "cueforge_music_metadata"


def test_openai_bpm_lookup_prompt_includes_japanese_tempo_queries(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    session = FakeSession(
        {
            "output_text": (
                '{"title":"純恋愛のインゴット","artist":"tuki.","album":null,"album_artist":null,'
                '"genre":"J-Pop","release_date":"2025-01-08","label":null,"isrc":null,'
                '"bpm":null,"cover_url":null,"confidence":0.82,'
                '"matched_fields":["title","artist","release_date"],'
                '"reason":"Official original metadata identified.","source_urls":["https://example.com/meta"],'
                '"bpm_source_url":null}'
            )
        }
    )
    suggester = OpenAIMetadataSuggester(OpenAIMetadataConfig(auth_path=token_path, model="gpt-test"), session=session)

    suggester.suggest(
        info={
            "title": "tuki.『純恋愛のインゴット』Official Music Video",
            "uploader": "tuki.",
            "webpage_url": "https://www.youtube.com/watch?v=goCvO7uJhu8",
        },
        reference=TrackMetadata(title="純恋愛のインゴット", artist="tuki."),
        candidates=[],
    )

    assert len(session.calls) == 1
    request = session.calls[0]["json"]
    assert "ChordWiki" in request["instructions"]
    assert "KeyTube" in request["instructions"]
    prompt_context = json.loads(request["input"][0]["content"])
    queries = prompt_context["suggested_bpm_search_queries"]
    release_queries = prompt_context["suggested_release_search_queries"]
    assert '"純恋愛のインゴット" "tuki." BPM' in queries
    assert '"純恋愛のインゴット" BPM テンポ' in queries
    assert '"純恋愛のインゴット" 原曲BPM' in queries
    assert '"純恋愛のインゴット" ChordWiki BPM' in queries
    assert '"純恋愛のインゴット" KeyTube BPM' in queries
    assert '"純恋愛のインゴット" Tunebat' in queries
    assert '"純恋愛のインゴット" "tuki." album' in release_queries
    assert '"純恋愛のインゴット" "tuki." Apple Music' in release_queries
    assert '"純恋愛のインゴット" "tuki." 収録アルバム' in release_queries


def test_openai_metadata_suggester_stops_reading_stream_at_completed_event(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    output = json.dumps(
        {
            "title": "Song",
            "artist": "Artist",
            "album": None,
            "album_artist": None,
            "genre": "Music",
            "release_date": None,
            "label": None,
            "isrc": None,
            "bpm": None,
            "cover_url": None,
            "confidence": 0.77,
            "matched_fields": ["title", "artist"],
            "reason": "Normalized from source context.",
            "source_urls": [],
            "bpm_source_url": None,
        }
    )
    session = FakeStreamingSession(
        [
            'data: {"type":"response.created","response":{"status":"in_progress"}}',
            "data: " + json.dumps({"type": "response.output_text.delta", "delta": output}),
            'data: {"type":"response.completed","response":{"status":"completed"}}',
        ]
    )
    suggester = OpenAIMetadataSuggester(OpenAIMetadataConfig(auth_path=token_path), session=session)

    candidates = suggester.suggest(info={}, reference=TrackMetadata(), candidates=[])

    assert len(candidates) == 1
    assert candidates[0].provider == "chatgpt"
    assert candidates[0].metadata.title == "Song"


def test_openai_metadata_suggester_skips_without_oauth_token(tmp_path) -> None:
    logs: list[str] = []
    suggester = OpenAIMetadataSuggester(OpenAIMetadataConfig(auth_path=tmp_path / "missing.json"))

    assert suggester.suggest(info={}, reference=TrackMetadata(), candidates=[], log=logs.append) == []
    assert "ChatGPT 계정 연결이 필요합니다" in logs[0]


def test_openai_metadata_suggester_logs_codex_error_body(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    logs: list[str] = []
    suggester = OpenAIMetadataSuggester(
        OpenAIMetadataConfig(auth_path=token_path, model="gpt-test"),
        session=FakeSession({}, response_status=400, response_text='{"detail":"Unsupported parameter: max_output_tokens"}'),
    )

    assert suggester.suggest(info={}, reference=TrackMetadata(), candidates=[], log=logs.append) == []
    assert any("Unsupported parameter" in message for message in logs)


def test_openai_codex_oauth_authorization_url_uses_registered_callback() -> None:
    url = build_openai_codex_oauth_authorization_url(
        redirect_uri=OPENAI_CODEX_OAUTH_REDIRECT_URI,
        state="state-token",
        code_challenge="challenge",
    )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert query["redirect_uri"] == ["http://localhost:1455/auth/callback"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid profile email offline_access api.connectors.read api.connectors.invoke"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["id_token_add_organizations"] == ["true"]
    assert query["codex_cli_simplified_flow"] == ["true"]


def test_openai_codex_models_are_fetched_from_oauth_catalog(tmp_path) -> None:
    token_path = tmp_path / "openai_oauth.json"
    write_openai_codex_oauth_token(
        {
            "access_token": _jwt(
                {
                    "exp": int(time.time()) + 3600,
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acct_test"},
                }
            ),
            "refresh_token": "refresh-token",
        },
        token_path,
    )
    session = FakeSession(
        {
            "models": [
                {"slug": "gpt-5.5", "display_name": "GPT-5.5", "supported_in_api": True, "visibility": "list"},
                {"slug": "hidden-model", "supported_in_api": True, "visibility": "hidden"},
                {"slug": "api-disabled", "supported_in_api": False, "visibility": "list"},
                {"id": "gpt-5.4-mini", "visibility": "list"},
            ]
        }
    )

    payload = fetch_openai_codex_models(token_path, session=session)

    assert openai_codex_model_ids(payload) == ["gpt-5.5", "gpt-5.4-mini"]
    assert session.calls[0]["url"].endswith("/models")
    assert session.calls[0]["params"]["client_version"]
    assert session.calls[0]["headers"]["ChatGPT-Account-ID"] == "acct_test"


def test_openai_codex_usage_summary_formats_windows_and_credits() -> None:
    summary = format_openai_codex_usage(
        {
            "email": "user@example.com",
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 25, "reset_after_seconds": 3600},
                "secondary_window": {"used_percent": 40, "reset_after_seconds": 172800},
            },
            "credits": {"balance": "553.4"},
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "rate_limit": {"primary_window": {"used_percent": 0, "reset_after_seconds": 1200}},
                }
            ],
        }
    )

    assert "user@example.com pro" in summary
    assert "5시간 75% 남음" in summary
    assert "주간 60% 남음" in summary
    assert "크레딧 553" in summary
    assert "GPT-5.3-Codex-Spark 100% 남음" in summary
    assert "\n- 5시간" in summary
    assert "; " not in summary


def _jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}."
