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
                _log(log, "ChatGPT 메타데이터 파서 결과 비어 있음")
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
        "You are a music metadata editor for a DJ tagging application. "
        "Your job is to identify the audible recording and return normalized tag fields. "
        "The source page title, uploader, channel, and description are noisy evidence, not fields to copy. "
        "Prefer concise tags a DJ would want in Rekordbox/ID3 over platform display text. "
        "BPM is an important DJ tag: make a dedicated effort to find a practical tempo value with web search. "
        "Never conclude that BPM is unknown before trying the suggested_bpm_search_queries from the user context. "
        "For Japanese songs, search Japanese tempo terms such as BPM（テンポ）, テンポ, and 原曲BPM; "
        "tempo sources such as ChordWiki, KeyTube, Chord Rinne, Tunebat, SongBPM, and chord/score pages are acceptable evidence. "
        "For original recordings, prefer official single or album artwork over YouTube/platform thumbnails when returning cover_url. "
        "Do not return expiring, signed, presigned, tokenized, or temporary artwork URLs such as AWS S3 X-Amz-* links. "
        "Preserve artist names in the source-language display spelling when that spelling appears in source title, channel, or uploader; do not romanize or transliterate names just because credits also contain romanized text. "
        "Use web search to verify facts such as official credits, release dates, ISRC, label, and BPM. "
        "Return null for unknown values. Do not invent facts or fill metadata from unrelated original releases. "
        "If evidence is ambiguous, return the best normalized review candidate with lower confidence and explain the uncertainty in reason."
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
            "goal": "Return normalized music tag metadata for the audible uploaded recording.",
            "not_goal": "Do not transcribe or preserve YouTube/webpage display titles as tags.",
            "decision_order": [
                "Identify what audio recording/version is actually uploaded.",
                "Separate musical identity from platform presentation text.",
                "Use parsed candidates as hypotheses for normalized title/artist, then verify with source context and web evidence.",
                "Fill only fields supported by evidence; leave uncertain fields null.",
            ],
        },
        "source": {
            "url": str(info.get("webpage_url") or info.get("original_url") or ""),
            "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
            "id": str(info.get("id") or ""),
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
        "suggested_bpm_search_queries": _bpm_search_queries(info, reference, candidates),
        "field_policy": {
            "title": [
                "Use the song/work/recording title only.",
                "Remove platform presentation text: upload labels, format labels, channel/uploader names, artist credits outside the title, and promotional wording.",
                "Do not include descriptors such as cover/performance/remix/live/MV/lyrics unless they are part of the actual released title or version title.",
                "Do not return source.title or source.fulltitle verbatim unless it is already a clean tag title.",
            ],
            "artist": [
                "Use the recording artist/performer for this uploaded audio.",
                "For covers or vocal performances, prefer the cover performer when credible; do not replace them with the original artist unless the upload is the original recording.",
                "Use uploader/channel/creator only as evidence, not as automatic truth.",
                "Preserve the artist spelling as shown in source title/channel/uploader when it is credible; do not translate, romanize, or transliterate that spelling unless credible release metadata consistently uses another spelling.",
            ],
            "album_album_artist": [
                "Use an album or collection only when it is supported by credible evidence for this exact recording.",
                "For standalone covers, leave album null unless a release/album is identified.",
            ],
            "release_date": [
                "Use the release date of this recording/version when known.",
                "Do not use the YouTube upload date as release_date unless the upload itself is the release and no better date exists.",
            ],
            "bpm": [
                "Actively search for a BPM/tempo value; do not omit BPM just because the source is a YouTube cover.",
                "Try suggested_bpm_search_queries before returning bpm null.",
                "Prefer the exact uploaded recording/version BPM when available.",
                "For covers or performances, if exact BPM is unavailable but the original/work BPM is widely agreed and the upload gives no evidence of a tempo change, return that practical BPM and explain the basis in reason.",
                "Leave bpm null only when targeted BPM searches find no credible value or credible sources materially disagree.",
            ],
            "cover_url": [
                "For original recordings, actively look for official single or album artwork from credible music sources.",
                "Prefer Apple Music, Spotify, official label/artist pages, Bandcamp, SoundCloud artwork, or other release artwork over YouTube video thumbnails.",
                "Use YouTube/platform thumbnails only when no credible release artwork is available or the upload is not an original release.",
                "Do not return expiring, signed, presigned, tokenized, or temporary image URLs, including URLs with X-Amz-* query parameters.",
                "Leave cover_url null if the only available image is an unrelated fan thumbnail or low-confidence artwork.",
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
            "Treat existing_candidates as parser hypotheses, not final truth.",
            "A candidate that is shorter and removes platform presentation text is usually a better title hypothesis than the raw source title.",
            "If you reject a candidate in favor of the source title, reason must say what evidence makes the source title a real tag title rather than a display title.",
        ],
        "confidence_policy": [
            "Use high confidence only when title and artist are supported by source context or credible web evidence.",
            "Use <=0.84 whenever human review is still needed.",
            "Lower confidence when keeping any part of a noisy platform title.",
        ],
    }


def _bpm_search_queries(info: dict[str, Any], reference: TrackMetadata, candidates: list[MetadataCandidate]) -> list[str]:
    titles = _dedupe_preserving_order(
        [
            reference.title,
            *[candidate.metadata.title for candidate in candidates[:5]],
            str(info.get("track") or ""),
            _searchable_source_title(str(info.get("title") or "")),
            _searchable_source_title(str(info.get("fulltitle") or "")),
        ]
    )
    artists = _dedupe_preserving_order(
        [
            reference.artist,
            *[candidate.metadata.artist for candidate in candidates[:5]],
            str(info.get("artist") or ""),
            str(info.get("creator") or ""),
            str(info.get("uploader") or ""),
            str(info.get("channel") or ""),
        ]
    )
    queries: list[str] = []
    for title in titles[:3]:
        if not title:
            continue
        if artists:
            queries.append(f'"{title}" "{artists[0]}" BPM')
        queries.extend(
            [
                f'"{title}" BPM',
                f'"{title}" BPM テンポ',
                f'"{title}" 原曲BPM',
                f'"{title}" ChordWiki BPM',
                f'"{title}" KeyTube BPM',
                f'"{title}" Tunebat',
            ]
        )
    return _dedupe_preserving_order(queries)[:12]


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
