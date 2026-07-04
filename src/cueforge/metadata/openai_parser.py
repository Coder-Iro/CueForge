"""OpenAI Codex OAuth-backed review candidate generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from cueforge.metadata.normalize import clean_metadata
from cueforge.metadata.openai_oauth import (
    OPENAI_CODEX_OAUTH_TOKEN_URI,
    CodexOAuthCredentials,
    OpenAICodexOAuthError,
    default_openai_codex_oauth_token_path,
    format_openai_codex_usage,
    load_openai_codex_oauth_credentials,
)
from cueforge.models import MetadataCandidate, TrackMetadata

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_OPENAI_REASONING_EFFORT = "medium"
DEFAULT_OPENAI_SEARCH_CONTEXT_SIZE = "high"
OPENAI_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
OPENAI_SEARCH_CONTEXT_SIZES = {"low", "medium", "high"}


@dataclass(frozen=True, slots=True)
class OpenAIMetadataConfig:
    model: str = DEFAULT_OPENAI_MODEL
    reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT
    search_context_size: str = DEFAULT_OPENAI_SEARCH_CONTEXT_SIZE
    auth_path: Path | None = None
    base_url: str = DEFAULT_CODEX_BASE_URL
    refresh_token_url: str = OPENAI_CODEX_OAUTH_TOKEN_URI
    timeout_seconds: int = 45

    @property
    def resolved_model(self) -> str:
        return (self.model or DEFAULT_OPENAI_MODEL).strip()

    @property
    def resolved_reasoning_effort(self) -> str:
        value = (self.reasoning_effort or DEFAULT_OPENAI_REASONING_EFFORT).strip().lower()
        return value if value in OPENAI_REASONING_EFFORTS else DEFAULT_OPENAI_REASONING_EFFORT

    @property
    def resolved_search_context_size(self) -> str:
        value = (self.search_context_size or DEFAULT_OPENAI_SEARCH_CONTEXT_SIZE).strip().lower()
        return value if value in OPENAI_SEARCH_CONTEXT_SIZES else DEFAULT_OPENAI_SEARCH_CONTEXT_SIZE


class OpenAIMetadataSuggester:
    def __init__(self, config: OpenAIMetadataConfig, *, session: Any | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def suggest(
        self,
        *,
        info: dict[str, Any],
        reference: TrackMetadata,
        candidates: list[MetadataCandidate],
        log: Callable[[str], None] | None = None,
    ) -> list[MetadataCandidate]:
        credentials = self._load_codex_oauth(log)
        if not credentials:
            return []
        try:
            payload = self._request_payload(info=info, reference=reference, candidates=candidates)
            _log(
                log,
                "ChatGPT 메타데이터 파서 호출: "
                f"Codex OAuth / {self.config.resolved_model} / "
                f"reasoning {self.config.resolved_reasoning_effort} / "
                f"web {self.config.resolved_search_context_size}",
            )
            response = self.session.post(
                f"{self.config.base_url.rstrip('/')}/responses",
                headers=_codex_headers(credentials),
                json=payload,
                timeout=self.config.timeout_seconds,
                stream=True,
            )
            _raise_for_status(response)
            response_payload = _response_payload(response)
            parsed = _parse_response_json(response_payload)
            candidate = _candidate_from_payload(parsed, _response_source_urls(response_payload))
            if not candidate:
                _log(log, f"ChatGPT 메타데이터 파서 결과 비어 있음: {_empty_response_detail(response_payload, parsed)}")
                return []
            quota_status = _response_quota_status(response, response_payload)
            if quota_status:
                candidate.raw["quota_status"] = quota_status
            source_count = len(candidate.raw.get("source_urls") or [])
            source_note = f", sources={source_count}" if source_count else ""
            _log(log, f"ChatGPT 후보 생성: {candidate.metadata.artist} - {candidate.metadata.title} ({candidate.score:.2f}{source_note})")
            return [candidate]
        except Exception as exc:
            _log(log, f"ChatGPT 메타데이터 파서 실패: {exc}")
            return []

    def _load_codex_oauth(self, log: Callable[[str], None] | None) -> CodexOAuthCredentials | None:
        try:
            return load_openai_codex_oauth_credentials(
                self.config.auth_path or default_openai_codex_oauth_token_path(),
                session=self.session,
                token_uri=self.config.refresh_token_url,
                timeout_seconds=self.config.timeout_seconds,
            )
        except OpenAICodexOAuthError as exc:
            _log(log, f"ChatGPT 메타데이터 파서 생략: {exc}")
            return None

    def _request_payload(
        self,
        *,
        info: dict[str, Any],
        reference: TrackMetadata,
        candidates: list[MetadataCandidate],
    ) -> dict[str, Any]:
        return {
            "model": self.config.resolved_model,
            "store": False,
            "stream": True,
            "reasoning": {"effort": self.config.resolved_reasoning_effort},
            "instructions": _system_instructions(),
            "input": [
                {
                    "role": "user",
                    "content": json.dumps(_prompt_context(info, reference, candidates), ensure_ascii=False),
                },
            ],
            "tools": [{"type": "web_search", "search_context_size": self.config.resolved_search_context_size}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "cueforge_music_metadata",
                    "schema": _RESPONSE_SCHEMA,
                    "strict": True,
                }
            },
        }


def _system_instructions() -> str:
    return (
        "당신은 DJ 태깅 앱의 음악 메타데이터 편집자입니다. "
        "해야 할 일은 실제로 들리는 녹음/버전을 식별하고 정규화된 태그 필드를 반환하는 것입니다. "
        "원본 페이지 제목, 업로더, 채널, 설명은 노이즈가 많은 증거일 뿐 그대로 복사할 필드가 아닙니다. "
        "플랫폼 표시 문구보다 Rekordbox/ID3에 들어갈 간결한 태그를 우선하세요. "
        "BPM은 중요한 DJ 태그입니다. 웹 검색으로 업로드된 녹음 자체의 템포 값을 찾으세요. "
        "suggested_bpm_search_queries를 시도하기 전에는 BPM을 알 수 없다고 결론내리지 마세요. "
        "커버, 라이브, 리믹스, 공연 업로드의 BPM은 원곡이 아니라 업로드된 버전 자체를 설명해야 하며, 원곡 BPM만 확인되면 null을 반환하세요. "
        "원곡 일본어 음원에서는 BPM（テンポ）, テンポ, 原曲BPM 같은 일본어 템포 용어도 검색하세요. "
        "ChordWiki, KeyTube, Chord Rinne, Tunebat, SongBPM, 코드/악보 페이지는 템포 근거로 사용할 수 있습니다. "
        "원곡/공식 발매 녹음으로 보이면 suggested_release_search_queries를 사용해 album과 album_artist를 적극적으로 확인하세요. "
        "원곡/공식 발매물의 cover_url은 YouTube/플랫폼 썸네일보다 공식 싱글 또는 앨범 아트워크를 우선하세요. "
        "AWS S3 X-Amz-* 링크처럼 만료되거나 서명된 presigned/tokenized/temporary 아트워크 URL은 반환하지 마세요. "
        "커버 업로드에서는 원곡 아티스트나 원곡 feat./featuring 크레딧을 실제 커버 참여자처럼 보존하지 마세요. "
        "원본 제목, 채널, 업로더에 출처 언어 표기가 신뢰 가능하게 나타나면 그 아티스트 표기를 보존하세요. 로마자 크레딧이 함께 있어도 임의로 로마자화하거나 번역하지 마세요. "
        "artist에는 대표 표기 하나만 넣고 원어명, 로마자명, 번역명, 괄호 별칭을 함께 이어 붙이지 마세요. "
        "공식 크레딧, 발매일, ISRC, 레이블, BPM 같은 사실은 웹 검색으로 검증하세요. "
        "알 수 없는 값은 null을 반환하세요. 사실을 지어내거나 관련 없는 원곡 발매 정보로 필드를 채우지 마세요. "
        "근거가 애매하면 가장 그럴듯한 정규화 후보를 낮은 confidence로 반환하고 reason에 불확실성을 설명하세요."
    )


def _raise_for_status(response: Any) -> None:
    status_code = getattr(response, "status_code", 200)
    if status_code < 400:
        return
    body = str(getattr(response, "text", "") or "").strip()
    detail = f": {body[:500]}" if body else ""
    raise RuntimeError(f"Codex Responses 요청 실패 ({status_code}){detail}")


def _prompt_context(info: dict[str, Any], reference: TrackMetadata, candidates: list[MetadataCandidate]) -> dict[str, Any]:
    return {
        "task": {
            "goal": "실제로 들리는 업로드 녹음에 대한 정규화된 음악 태그 메타데이터를 반환하세요.",
            "not_goal": "YouTube/웹페이지 표시 제목을 그대로 받아쓰거나 태그로 보존하지 마세요.",
            "decision_order": [
                "실제로 업로드된 오디오 녹음/버전이 무엇인지 식별하세요.",
                "음악적 정체성과 플랫폼 표시 문구를 분리하세요.",
                "파싱된 후보는 정규화된 title/artist에 대한 가설로만 사용하고, source context와 웹 근거로 검증하세요.",
                "근거가 있는 필드만 채우고 불확실한 필드는 null로 두세요.",
            ],
        },
        "source": {
            "url": str(info.get("webpage_url") or info.get("original_url") or ""),
            "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
            "id": str(info.get("id") or ""),
            "probable_cover_upload": _probable_cover_upload(info),
            "title": str(info.get("title") or ""),
            "fulltitle": str(info.get("fulltitle") or ""),
            "track": str(info.get("track") or ""),
            "artist": str(info.get("artist") or ""),
            "uploader": str(info.get("uploader") or ""),
            "channel": str(info.get("channel") or ""),
            "creator": str(info.get("creator") or ""),
            "duration_seconds": info.get("duration"),
            "upload_date": str(info.get("upload_date") or ""),
            "release_date": str(info.get("release_date") or ""),
            "description": _truncate(str(info.get("description") or ""), 6000),
        },
        "suggested_release_search_queries": _release_search_queries(info, reference, candidates),
        "suggested_bpm_search_queries": _bpm_search_queries(info, reference, candidates),
        "field_policy": {
            "title": [
                "곡/작품/녹음 제목만 사용하세요.",
                "업로드 라벨, 형식 라벨, 채널/업로더명, 제목 바깥의 아티스트 크레딧, 홍보 문구 같은 플랫폼 표시 텍스트를 제거하세요.",
                "cover/performance/remix/live/MV/lyrics 같은 설명어는 실제 발매 제목 또는 버전 제목의 일부가 아니면 포함하지 마세요.",
                "커버 업로드 제목의 원곡 아티스트, 원곡 feat./featuring 크레딧, Original 표기는 실제 커버 제목의 일부가 아니면 제거하세요.",
                "source.title 또는 source.fulltitle이 이미 깨끗한 태그 제목인 경우가 아니면 그대로 반환하지 마세요.",
            ],
            "artist": [
                "이 업로드 오디오의 녹음 아티스트/퍼포머를 사용하세요.",
                "커버 또는 보컬 퍼포먼스는 신뢰 가능할 때 커버 퍼포머를 우선하세요. 업로드가 원곡 녹음이 아닌데 원곡 아티스트로 바꾸지 마세요.",
                "커버 제목의 'Covered by X'나 설명의 보컬 크레딧은 channel/uploader보다 강한 커버 퍼포머 근거입니다.",
                "uploader/channel/creator는 자동 정답이 아니라 근거로만 사용하세요.",
                "source title/channel/uploader에 나타난 아티스트 표기가 신뢰 가능하면 그 표기를 보존하세요. 신뢰 가능한 발매 메타데이터가 일관되게 다른 표기를 쓰는 경우가 아니면 번역, 로마자화, 음역하지 마세요.",
                "한 필드에 여러 표기를 병기하지 마세요. 예: '텐코 시부키 TENKO SHIBUKI'가 아니라 '텐코 시부키', 'Charming Jo (조매력)'가 아니라 source에서 신뢰되는 대표 표기 하나만 반환하세요.",
            ],
            "album_album_artist": [
                "원곡/공식 발매 녹음으로 보이면 suggested_release_search_queries로 공식 발매명과 앨범 아티스트를 적극적으로 찾으세요.",
                "공식 싱글 발매라면 album에는 그 싱글/릴리즈명을 넣고, album_artist에는 공식 발매의 주 아티스트를 넣으세요.",
                "앨범 수록곡이면 album에는 수록 앨범명을 넣고, album_artist에는 그 앨범의 공식 앨범 아티스트를 넣으세요.",
                "Apple Music, Spotify, YouTube Music, Bandcamp, SoundCloud, 공식 레이블/아티스트 페이지처럼 발매 단위 메타데이터를 보여주는 출처를 우선하세요.",
                "이 정확한 녹음에 대한 신뢰 가능한 발매 근거가 있을 때만 앨범 또는 컬렉션을 사용하세요.",
                "단독 커버, 라이브, 공연 업로드는 공식 발매/앨범이 식별되지 않으면 album과 album_artist를 null로 두세요.",
            ],
            "release_date": [
                "알려진 경우 이 녹음/버전의 발매일을 사용하세요.",
                "업로드 자체가 발매물이고 더 나은 날짜가 없을 때가 아니면 YouTube 업로드 날짜를 release_date로 쓰지 마세요.",
            ],
            "bpm": [
                "업로드된 녹음/버전 자체의 BPM/템포 값을 적극적으로 검색하세요.",
                "bpm을 null로 반환하기 전에 suggested_bpm_search_queries를 시도하세요.",
                "가능하면 업로드된 녹음/버전과 정확히 일치하는 BPM을 우선하세요.",
                "source.probable_cover_upload가 true이면 이 커버/업로드/공연/버전을 설명하는 신뢰 가능한 근거가 있을 때만 bpm을 반환하세요.",
                "커버, 라이브 공연, 리믹스, 기타 비원곡 업로드에 원곡/작품/곡 BPM을 복사하지 마세요.",
                "목표 BPM 검색 결과가 원곡 BPM뿐이거나, 신뢰 가능한 업로드 버전 BPM이 없거나, 출처 간 값이 크게 충돌하면 bpm을 null로 두세요.",
            ],
            "cover_url": [
                "원곡/공식 발매 녹음은 신뢰 가능한 음악 출처에서 공식 싱글 또는 앨범 아트워크를 적극적으로 찾으세요.",
                "YouTube 비디오 썸네일보다 Apple Music, Spotify, 공식 레이블/아티스트 페이지, Bandcamp, SoundCloud 아트워크 또는 기타 발매 아트워크를 우선하세요.",
                "신뢰 가능한 발매 아트워크가 없거나 업로드가 원곡/공식 발매가 아닐 때만 YouTube/플랫폼 썸네일을 사용하세요.",
                "X-Amz-* 쿼리 파라미터가 있는 URL을 포함해 만료되거나 서명된 presigned/tokenized/temporary 이미지 URL은 반환하지 마세요.",
                "사용 가능한 이미지가 관련 없는 팬 썸네일이거나 신뢰도가 낮은 아트워크뿐이면 cover_url을 null로 두세요.",
            ],
        },
        "reference_metadata": _metadata_payload(reference),
        "existing_candidates": [
            {
                "provider": candidate.provider,
                "score": candidate.score,
                "metadata": _metadata_payload(candidate.metadata),
                "matched_fields": list(candidate.matched_fields),
            }
            for candidate in candidates[:5]
        ],
        "candidate_policy": [
            "existing_candidates는 최종 정답이 아니라 파서 가설로 취급하세요.",
            "플랫폼 표시 텍스트를 제거한 더 짧은 후보는 보통 원본 source title보다 나은 title 가설입니다.",
            "후보를 버리고 source title을 선택한다면, reason에 그 source title이 표시 제목이 아니라 실제 태그 제목이라고 볼 근거를 적으세요.",
        ],
        "confidence_policy": [
            "title과 artist가 source context 또는 신뢰 가능한 웹 근거로 뒷받침될 때만 높은 confidence를 사용하세요.",
            "사람의 검수가 여전히 필요하면 confidence를 0.84 이하로 두세요.",
            "노이즈가 많은 플랫폼 제목의 일부를 유지할 때는 confidence를 낮추세요.",
        ],
    }


def _bpm_search_queries(info: dict[str, Any], reference: TrackMetadata, candidates: list[MetadataCandidate]) -> list[str]:
    titles = _search_titles(info, reference, candidates)
    artists = _search_artists(info, reference, candidates)
    is_cover_upload = _probable_cover_upload(info)
    queries: list[str] = []
    for title in titles[:3]:
        if not title:
            continue
        if artists:
            queries.append(f'"{title}" "{artists[0]}" BPM')
            if is_cover_upload:
                queries.append(f'"{title}" "{artists[0]}" cover BPM')
                queries.append(f'"{title}" "{artists[0]}" 歌ってみた BPM')
        queries.extend(
            [
                f'"{title}" BPM',
                f'"{title}" BPM テンポ',
                f'"{title}" ChordWiki BPM',
                f'"{title}" KeyTube BPM',
                f'"{title}" Tunebat',
            ]
        )
        if not is_cover_upload:
            queries.append(f'"{title}" 原曲BPM')
    return _dedupe_preserving_order(queries)[:12]


def _release_search_queries(info: dict[str, Any], reference: TrackMetadata, candidates: list[MetadataCandidate]) -> list[str]:
    titles = _search_titles(info, reference, candidates)
    artists = _search_artists(info, reference, candidates)
    is_cover_upload = _probable_cover_upload(info)
    queries: list[str] = []
    for title in titles[:3]:
        if not title:
            continue
        if artists:
            artist = artists[0]
            if is_cover_upload:
                queries.extend(
                    [
                        f'"{title}" "{artist}" cover release',
                        f'"{title}" "{artist}" 歌ってみた 配信',
                    ]
                )
            else:
                queries.extend(
                    [
                        f'"{title}" "{artist}" album',
                        f'"{title}" "{artist}" album artist',
                        f'"{title}" "{artist}" single',
                        f'"{title}" "{artist}" Apple Music',
                        f'"{title}" "{artist}" Spotify',
                        f'"{title}" "{artist}" YouTube Music',
                        f'"{title}" "{artist}" 収録アルバム',
                        f'"{title}" "{artist}" 配信',
                    ]
                )
        elif not is_cover_upload:
            queries.extend([f'"{title}" album', f'"{title}" single', f'"{title}" Apple Music'])
    return _dedupe_preserving_order(queries)[:12]


def _search_titles(info: dict[str, Any], reference: TrackMetadata, candidates: list[MetadataCandidate]) -> list[str]:
    titles = _dedupe_preserving_order(
        [
            reference.title,
            *[candidate.metadata.title for candidate in candidates[:5]],
            str(info.get("track") or ""),
            _searchable_source_title(str(info.get("title") or "")),
            _searchable_source_title(str(info.get("fulltitle") or "")),
        ]
    )
    return titles


def _search_artists(info: dict[str, Any], reference: TrackMetadata, candidates: list[MetadataCandidate]) -> list[str]:
    return _dedupe_preserving_order(
        [
            reference.artist,
            *[candidate.metadata.artist for candidate in candidates[:5]],
            str(info.get("artist") or ""),
            str(info.get("creator") or ""),
            str(info.get("uploader") or ""),
            str(info.get("channel") or ""),
        ]
    )


def _probable_cover_upload(info: dict[str, Any]) -> bool:
    source = " ".join(
        str(info.get(key) or "")
        for key in ("fulltitle", "title", "track", "description")
    ).casefold()
    return any(token in source for token in ("cover", "커버", "歌ってみた", "covered by"))


def _searchable_source_title(value: str) -> str:
    text = _string(value)
    for marker in (
        "Official Music Video",
        "Official MV",
        "Music Video",
        "MV",
        "Lyrics",
        "歌詞",
        "COVER",
        "Cover",
        "cover",
        "|",
    ):
        if marker in text:
            text = text.split(marker, 1)[0]
    return text.strip(" -_｜|「」『』[]()（）")


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _string(value)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _metadata_payload(metadata: TrackMetadata) -> dict[str, Any]:
    return {field: getattr(metadata, field) for field in metadata.field_names()}


def _parse_response_json(data: dict[str, Any]) -> dict[str, Any]:
    text = _response_text(data)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _empty_response_detail(data: dict[str, Any], parsed: dict[str, Any]) -> str:
    text = _response_text(data)
    status = _response_status_detail(data)
    suffix = f" ({status})" if status else ""
    if not text:
        return f"응답 텍스트 없음{suffix}"
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"JSON 파싱 실패: {exc.msg}{suffix}"
    if not isinstance(loaded, dict):
        return f"JSON 객체 아님: {type(loaded).__name__}{suffix}"
    if not parsed:
        return f"JSON 객체 비어 있음{suffix}"
    keys = ", ".join(sorted(str(key) for key in parsed.keys())[:8])
    title = _string(parsed.get("title"))
    artist = _string(parsed.get("artist"))
    matched = ", ".join(_string_list(parsed.get("matched_fields"))[:8])
    return f"후보 필드 부족: keys={keys or '-'}, title={title or '-'}, artist={artist or '-'}, matched_fields={matched or '-'}{suffix}"


def _response_status_detail(data: dict[str, Any]) -> str:
    response = data.get("response") if isinstance(data.get("response"), dict) else data
    parts: list[str] = []
    event_type = _string(data.get("type"))
    status = _string(response.get("status")) if isinstance(response, dict) else ""
    if event_type:
        parts.append(f"type={event_type}")
    if status:
        parts.append(f"status={status}")
    if isinstance(response, dict):
        incomplete = response.get("incomplete_details")
        if isinstance(incomplete, dict):
            detail = _compact_json(incomplete)
            if detail:
                parts.append(f"incomplete={detail}")
        error = response.get("error") or data.get("error")
    else:
        error = data.get("error")
    if isinstance(error, dict):
        detail = _compact_json(error)
        if detail:
            parts.append(f"error={detail}")
    elif error:
        parts.append(f"error={_string(error)}")
    return ", ".join(parts)


def _compact_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return text[:240]


def _response_payload(response: Any) -> dict[str, Any]:
    if hasattr(response, "iter_lines"):
        events = _sse_events(response)
        if events:
            return _payload_from_sse_events(events)
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sse_events(response: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in response.iter_lines(decode_unicode=True):
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line or "")
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
            event_type = str(event.get("type") or "")
            if event_type in {"response.completed", "response.failed", "response.incomplete", "error"}:
                break
    return events


def _payload_from_sse_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    completed: dict[str, Any] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_parts.append(event["delta"])
        if event_type == "response.completed" and isinstance(event.get("response"), dict):
            completed = event["response"]
    if text_parts:
        payload = dict(completed)
        payload["output_text"] = "".join(text_parts)
        return payload
    return completed or (events[-1] if events else {})


def _response_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _response_source_urls(data: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations") or []:
                if not isinstance(annotation, dict):
                    continue
                url = (
                    annotation.get("url")
                    or (annotation.get("url_citation") or {}).get("url")
                    or annotation.get("uri")
                    or ""
                )
                if url:
                    urls.append(str(url))
    return list(dict.fromkeys(urls))


def _response_quota_status(response: Any, payload: dict[str, Any]) -> str:
    if isinstance(payload.get("rate_limit"), dict) or isinstance(payload.get("credits"), dict):
        try:
            return format_openai_codex_usage(payload)
        except Exception:
            pass
    return _quota_status_from_headers(getattr(response, "headers", {}) or {})


def _quota_status_from_headers(headers: Any) -> str:
    codex_payload = _codex_usage_payload_from_headers(headers)
    if codex_payload:
        try:
            return format_openai_codex_usage(codex_payload)
        except Exception:
            pass
    request_remaining = _header_value(headers, "x-ratelimit-remaining-requests")
    request_limit = _header_value(headers, "x-ratelimit-limit-requests")
    request_reset = _header_value(headers, "x-ratelimit-reset-requests")
    token_remaining = _header_value(headers, "x-ratelimit-remaining-tokens")
    token_limit = _header_value(headers, "x-ratelimit-limit-tokens")
    token_reset = _header_value(headers, "x-ratelimit-reset-tokens")
    parts: list[str] = []
    if request_remaining:
        item = f"요청 {request_remaining}"
        if request_limit:
            item += f"/{request_limit}"
        item += " 남음"
        if request_reset:
            item += f", 재설정 {request_reset}"
        parts.append(item)
    if token_remaining:
        item = f"토큰 {token_remaining}"
        if token_limit:
            item += f"/{token_limit}"
        item += " 남음"
        if token_reset:
            item += f", 재설정 {token_reset}"
        parts.append(item)
    return " · ".join(parts)


def _codex_usage_payload_from_headers(headers: Any) -> dict[str, Any]:
    plan_type = _header_value(headers, "x-codex-plan-type")
    primary = _codex_window_from_headers(headers, "x-codex-primary")
    secondary = _codex_window_from_headers(headers, "x-codex-secondary")
    credits = {
        "has_credits": _bool_header(headers, "x-codex-credits-has-credits"),
        "balance": _header_value(headers, "x-codex-credits-balance"),
        "unlimited": _bool_header(headers, "x-codex-credits-unlimited"),
    }
    additional = _additional_codex_limits_from_headers(headers)
    if not any((plan_type, primary, secondary, additional, any(value not in (None, "") for value in credits.values()))):
        return {}
    payload: dict[str, Any] = {
        "plan_type": plan_type,
        "rate_limit": {
            "primary_window": primary,
            "secondary_window": secondary,
        },
        "credits": {key: value for key, value in credits.items() if value not in (None, "")},
    }
    if additional:
        payload["additional_rate_limits"] = additional
    return payload


def _codex_window_from_headers(headers: Any, prefix: str) -> dict[str, Any]:
    used_percent = _header_value(headers, f"{prefix}-used-percent")
    reset_after = _header_value(headers, f"{prefix}-reset-after-seconds")
    reset_at = _header_value(headers, f"{prefix}-reset-at")
    window_minutes = _header_value(headers, f"{prefix}-window-minutes")
    value: dict[str, Any] = {}
    if used_percent:
        value["used_percent"] = used_percent
    if reset_after:
        value["reset_after_seconds"] = reset_after
    if reset_at:
        value["reset_at"] = reset_at
    if window_minutes:
        value["window_minutes"] = window_minutes
    return value


def _additional_codex_limits_from_headers(headers: Any) -> list[dict[str, Any]]:
    prefixes: set[str] = set()
    keys = headers.keys() if hasattr(headers, "keys") else []
    for key in keys:
        lowered = str(key).lower()
        if not lowered.startswith("x-codex-") or not lowered.endswith("-primary-used-percent"):
            continue
        middle = lowered.removeprefix("x-codex-").removesuffix("-primary-used-percent")
        if middle in {"", "primary", "secondary"}:
            continue
        prefixes.add(f"x-codex-{middle}")
    limits: list[dict[str, Any]] = []
    for prefix in sorted(prefixes):
        name = _header_value(headers, f"{prefix}-limit-name") or prefix.removeprefix("x-codex-")
        primary = _codex_window_from_headers(headers, f"{prefix}-primary")
        secondary = _codex_window_from_headers(headers, f"{prefix}-secondary")
        if primary or secondary:
            limits.append(
                {
                    "limit_name": name,
                    "rate_limit": {
                        "primary_window": primary,
                        "secondary_window": secondary,
                    },
                }
            )
    return limits


def _bool_header(headers: Any, name: str) -> bool | None:
    value = _header_value(headers, name).casefold()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    return None


def _header_value(headers: Any, name: str) -> str:
    if not hasattr(headers, "get"):
        return ""
    for key in (name, name.lower(), name.upper()):
        value = headers.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _candidate_from_payload(payload: dict[str, Any], citation_urls: list[str]) -> MetadataCandidate | None:
    if not payload:
        return None
    metadata = clean_metadata(
        TrackMetadata(
            title=_string(payload.get("title")),
            artist=_string(payload.get("artist")),
            album=_string(payload.get("album")),
            album_artist=_string(payload.get("album_artist")),
            genre=_string(payload.get("genre")),
            release_date=_string(payload.get("release_date")),
            label=_string(payload.get("label")),
            isrc=_string(payload.get("isrc")),
            bpm=_optional_int(payload.get("bpm")),
            cover_url=_string(payload.get("cover_url")),
        )
    )
    matched_fields = tuple(_matched_fields(payload, metadata))
    if not matched_fields:
        return None
    confidence = _confidence(payload.get("confidence"))
    source_urls = list(dict.fromkeys([*_string_list(payload.get("source_urls")), *citation_urls]))
    raw = {
        "review_only": True,
        "prefer_initial_metadata": bool(metadata.title and metadata.artist),
        "reason": _string(payload.get("reason")),
        "source_urls": source_urls,
        "bpm_source_url": _string(payload.get("bpm_source_url")),
    }
    return MetadataCandidate(
        provider="chatgpt",
        metadata=metadata,
        score=min(confidence, 0.84),
        matched_fields=matched_fields,
        raw=raw,
    )


def _matched_fields(payload: dict[str, Any], metadata: TrackMetadata) -> list[str]:
    explicit = [field for field in _string_list(payload.get("matched_fields")) if field in TrackMetadata.field_names() or field == "bpm"]
    if explicit:
        return [field for field in explicit if _metadata_field_present(metadata, field)]
    fields = []
    for field in ("title", "artist", "album", "album_artist", "genre", "release_date", "label", "isrc", "bpm", "cover_url"):
        if getattr(metadata, field):
            fields.append(field)
    return fields


def _metadata_field_present(metadata: TrackMetadata, field: str) -> bool:
    return bool(getattr(metadata, field, None))


def _string(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _string(item))]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return numeric if 20 <= numeric <= 300 else None


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.72
    return max(0.0, min(confidence, 0.84))


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit].rstrip() + "..."


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)


def _codex_headers(credentials: CodexOAuthCredentials) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.0.1",
    }
    if credentials.account_id:
        headers["ChatGPT-Account-ID"] = credentials.account_id
    return headers


_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": ["string", "null"]},
        "artist": {"type": ["string", "null"]},
        "album": {"type": ["string", "null"]},
        "album_artist": {"type": ["string", "null"]},
        "genre": {"type": ["string", "null"]},
        "release_date": {"type": ["string", "null"], "description": "YYYY, YYYY-MM, or YYYY-MM-DD when known"},
        "label": {"type": ["string", "null"]},
        "isrc": {"type": ["string", "null"]},
        "bpm": {"type": ["integer", "null"]},
        "cover_url": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "matched_fields": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "source_urls": {"type": "array", "items": {"type": "string"}},
        "bpm_source_url": {"type": ["string", "null"]},
    },
    "required": [
        "title",
        "artist",
        "album",
        "album_artist",
        "genre",
        "release_date",
        "label",
        "isrc",
        "bpm",
        "cover_url",
        "confidence",
        "matched_fields",
        "reason",
        "source_urls",
        "bpm_source_url",
    ],
    "additionalProperties": False,
}
