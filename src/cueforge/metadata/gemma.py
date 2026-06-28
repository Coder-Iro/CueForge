"""Gemma E2B fallback metadata suggestions through Deno + Transformers.js."""

from __future__ import annotations

import atexit
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable

from huggingface_hub import HfApi, snapshot_download
from platformdirs import user_cache_path

from cueforge.metadata.matching import text_similarity
from cueforge.metadata.normalize import clean_metadata, squash_spaces
from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.runtime import find_executable

DEFAULT_GEMMA_E2B_MODEL_REPO = "onnx-community/gemma-4-E2B-it-ONNX"
DEFAULT_GEMMA_E2B_MARKER = "gemma-e2b-it.ready.json"
DEFAULT_GEMMA_E2B_MARKER_VERSION = 3
GEMMA_E2B_REQUIRED_FILES = (
    "chat_template.jinja",
    "config.json",
    "generation_config.json",
    "preprocessor_config.json",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "onnx/audio_encoder_q4.onnx",
    "onnx/audio_encoder_q4.onnx_data",
    "onnx/decoder_model_merged_q4.onnx",
    "onnx/decoder_model_merged_q4.onnx_data",
    "onnx/embed_tokens_q4.onnx",
    "onnx/embed_tokens_q4.onnx_data",
    "onnx/vision_encoder_q4.onnx",
    "onnx/vision_encoder_q4.onnx_data",
)
GENERIC_SUPPORT_TOKENS = {
    "artist",
    "audio",
    "cover",
    "full",
    "live",
    "lyrics",
    "music",
    "official",
    "remix",
    "song",
    "title",
    "track",
    "video",
}


@dataclass(frozen=True, slots=True)
class GemmaE2BConfig:
    enabled: bool = True
    allow_download: bool = False
    model_repo: str = DEFAULT_GEMMA_E2B_MODEL_REPO
    cache_dir: Path | None = None
    deno_path: Path | None = None
    timeout_seconds: int = 90
    max_new_tokens: int = 96


class GemmaE2BMetadataSuggester:
    def __init__(
        self,
        config: GemmaE2BConfig | None = None,
        *,
        runner: Callable[[dict[str, Any], GemmaE2BConfig], str] | None = None,
    ) -> None:
        self.config = config or GemmaE2BConfig()
        self._runner = runner or _run_gemma_session
        self._uses_default_runner = runner is None

    def suggest(
        self,
        *,
        info: dict[str, Any],
        reference: TrackMetadata,
        candidates: list[MetadataCandidate],
        log: Callable[[str], None] | None = None,
    ) -> list[MetadataCandidate]:
        if not self.config.enabled or _has_strong_external_candidate(candidates):
            return []
        if not self.config.allow_download and self._uses_default_runner and not gemma_e2b_cached(self.config):
            _log(log, "Gemma E2B 모델이 아직 준비되지 않아 fallback 후보 생성을 건너뜀")
            return []
        payload = {
            "mode": "suggest",
            "model": self.config.model_repo,
            "modelPath": _path_for_js(_gemma_model_dir(self.config)),
            "cacheDir": _path_for_js(_gemma_cache_dir(self.config)),
            "allowDownload": self.config.allow_download,
            "maxNewTokens": self.config.max_new_tokens,
            "contextKey": _gemma_context_key(info, reference),
            "input": _prompt_input(info, reference),
        }
        try:
            _log(log, "Gemma E2B fallback 후보 생성 준비")
            output = self._runner(payload, self.config)
        except Exception as exc:
            if not self.config.allow_download and _is_missing_local_model_error(exc):
                _invalidate_gemma_marker(self.config)
                _log(log, "Gemma E2B 모델 캐시가 없어 fallback 후보 생성을 건너뜀. 초기 준비에서 모델을 다시 다운로드하세요.")
                return []
            _log(log, f"Gemma E2B fallback 실행 실패: {exc}")
            raise RuntimeError(f"Gemma E2B 모델을 실행할 수 없습니다: {exc}") from exc
        parsed, metadata = _parse_suggestion_output(output, log=log, final=False)
        if not metadata:
            repair_output = self._repair_suggestion_output(payload, output, log=log)
            parsed, metadata = _parse_suggestion_output(repair_output, log=log, final=True)
        if not metadata or parsed is None:
            return []
        if not _model_metadata_is_supported(metadata, info, reference):
            _log(log, "Gemma E2B fallback 폐기: 원본과 맞지 않는 후보")
            return []
        _log(log, f"Gemma E2B fallback 후보: {metadata.artist} - {metadata.title}")
        return [
            MetadataCandidate(
                provider="gemma_e2b",
                score=0.0,
                matched_fields=("gemma_e2b", "title", "artist"),
                metadata=metadata,
                raw={
                    "model": self.config.model_repo,
                    "reason": squash_spaces(str(parsed.get("reason") or "")),
                    "review_only": True,
                    "requires_semantic_score": True,
                },
            )
        ]

    def _repair_suggestion_output(
        self,
        payload: dict[str, Any],
        output: str,
        *,
        log: Callable[[str], None] | None,
    ) -> str:
        if not output:
            return ""
        repair_payload = dict(payload)
        repair_payload["mode"] = "repair"
        repair_payload["badOutput"] = output
        _log(log, "Gemma E2B fallback JSON 복구 재시도")
        try:
            return self._runner(repair_payload, self.config)
        except Exception as exc:
            _log(log, f"Gemma E2B fallback JSON 복구 실패: {exc}")
            return ""


def gemma_e2b_cached(config: GemmaE2BConfig | None = None) -> bool:
    resolved = config or GemmaE2BConfig()
    marker = _gemma_marker_path(resolved)
    if not marker.exists():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("model") == resolved.model_repo
        and payload.get("marker_version") == DEFAULT_GEMMA_E2B_MARKER_VERSION
        and payload.get("model_dir") == str(_gemma_model_dir(resolved))
        and _gemma_required_files_present(resolved)
    )


def prepare_gemma_e2b(
    config: GemmaE2BConfig | None = None,
    *,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float | None], None] | None = None,
    runner: Callable[[dict[str, Any], GemmaE2BConfig], str] | None = None,
) -> None:
    resolved = config or GemmaE2BConfig(allow_download=True, timeout_seconds=600)
    payload = {
        "mode": "prepare",
        "model": resolved.model_repo,
        "modelPath": _path_for_js(_gemma_model_dir(resolved)),
        "cacheDir": _path_for_js(_gemma_cache_dir(resolved)),
        "allowDownload": False,
        "maxNewTokens": 1,
    }
    _log(log, "Gemma E2B 모델 준비 중")
    _emit_progress(progress, 0.0)
    if runner:
        runner(payload, resolved)
    else:
        _download_gemma_e2b_model(resolved, log=log, progress=progress)
        _log(log, "Gemma E2B 로컬 모델 실행 확인 중")
        _emit_progress(progress, 96.0)
        _run_gemma_session(payload, replace(resolved, allow_download=False))
    marker = _gemma_marker_path(resolved)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "model": resolved.model_repo,
                "marker_version": DEFAULT_GEMMA_E2B_MARKER_VERSION,
                "cache_dir": str(_gemma_cache_dir(resolved)),
                "model_dir": str(_gemma_model_dir(resolved)),
                "files": list(GEMMA_E2B_REQUIRED_FILES),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _emit_progress(progress, 100.0)
    _log(log, "Gemma E2B 모델 준비 완료")


_GEMMA_SESSIONS_LOCK = threading.Lock()
_GEMMA_SESSIONS: dict[tuple[str, str, str, str, bool], "_GemmaDenoSession"] = {}


def _run_gemma_session(payload: dict[str, Any], config: GemmaE2BConfig) -> str:
    session = _get_gemma_session(config)
    return session.run(payload, timeout_seconds=config.timeout_seconds)


def _get_gemma_session(config: GemmaE2BConfig) -> "_GemmaDenoSession":
    key = _gemma_session_key(config)
    with _GEMMA_SESSIONS_LOCK:
        session = _GEMMA_SESSIONS.get(key)
        if session is None or not session.is_alive():
            session = _GemmaDenoSession(config, key=key)
            _GEMMA_SESSIONS[key] = session
        return session


def _gemma_session_key(config: GemmaE2BConfig) -> tuple[str, str, str, str, bool]:
    return (
        str(_gemma_deno_path(config)),
        config.model_repo,
        str(_gemma_model_dir(config)),
        str(_gemma_cache_dir(config)),
        bool(config.allow_download),
    )


def _gemma_deno_path(config: GemmaE2BConfig) -> Path:
    deno = config.deno_path or find_executable("deno").path
    if not deno:
        raise RuntimeError("Deno executable not found")
    return Path(deno)


class _GemmaDenoSession:
    def __init__(self, config: GemmaE2BConfig, *, key: tuple[str, str, str, str, bool]) -> None:
        self.config = config
        self.key = key
        self._lock = threading.Lock()
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._request_index = 0
        self._process: subprocess.Popen[str] | None = None
        self._script_path: Path | None = None
        self._start()

    def is_alive(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def run(self, payload: dict[str, Any], *, timeout_seconds: int) -> str:
        with self._lock:
            if not self.is_alive():
                self.stop()
                self._start()
            process = self._process
            if not process or not process.stdin:
                raise RuntimeError("Gemma E2B session is not writable")
            self._request_index += 1
            request_id = f"req-{self._request_index}"
            request = dict(payload)
            request["requestId"] = request_id
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except Exception as exc:
                self.stop()
                raise RuntimeError(f"Gemma E2B session write failed: {exc}") from exc
            return self._read_response(request_id, timeout_seconds=timeout_seconds)

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process and process.poll() is None:
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
            try:
                process.kill()
            except Exception:
                pass
        if self._script_path:
            try:
                self._script_path.unlink()
            except OSError:
                pass
            self._script_path = None

    def _start(self) -> None:
        deno = _gemma_deno_path(self.config)
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as script_file:
            script_file.write(_GEMMA_DENO_SESSION_SCRIPT)
            self._script_path = Path(script_file.name)
        process = subprocess.Popen(
            [
                str(deno),
                "run",
                "--quiet",
                "--allow-env",
                "--allow-ffi",
                "--allow-net",
                "--allow-read",
                "--allow-write",
                str(self._script_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._process = process
        threading.Thread(target=self._read_stdout, args=(process.stdout,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process.stderr,), daemon=True).start()

    def _read_response(self, request_id: str, *, timeout_seconds: int) -> str:
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop()
                raise RuntimeError(f"Deno timed out after {timeout_seconds}s")
            try:
                line = self._stdout.get(timeout=min(0.1, remaining))
            except queue.Empty:
                if self._process and self._process.poll() is not None:
                    raise RuntimeError(self._session_error_message())
                continue
            if line is None:
                raise RuntimeError(self._session_error_message())
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_lines.append(line)
                continue
            if not isinstance(response, dict) or response.get("cueforgeResponse") is not True:
                continue
            if response.get("requestId") != request_id:
                continue
            if response.get("ok") is False:
                raise RuntimeError(str(response.get("error") or self._session_error_message()))
            return str(response.get("output") or "")

    def _read_stdout(self, stream: Any) -> None:
        if not stream:
            self._stdout.put(None)
            return
        try:
            for line in stream:
                self._stdout.put(line)
        finally:
            self._stdout.put(None)

    def _read_stderr(self, stream: Any) -> None:
        if not stream:
            return
        for line in stream:
            self._stderr_lines.append(line)

    def _session_error_message(self) -> str:
        message = "".join(self._stderr_lines[-20:]).strip()
        if message:
            return message
        if self._process and self._process.poll() is not None:
            return f"Deno exited with {self._process.poll()}"
        return "Gemma E2B session stopped before returning a response"


def _shutdown_gemma_sessions() -> None:
    with _GEMMA_SESSIONS_LOCK:
        sessions = list(_GEMMA_SESSIONS.values())
        _GEMMA_SESSIONS.clear()
    for session in sessions:
        session.stop()


atexit.register(_shutdown_gemma_sessions)


def _download_gemma_e2b_model(
    config: GemmaE2BConfig,
    *,
    log: Callable[[str], None] | None,
    progress: Callable[[float | None], None] | None,
) -> Path:
    if not config.allow_download:
        return _gemma_model_dir(config)
    model_dir = _gemma_model_dir(config)
    model_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = _gemma_required_remote_bytes(config)
    if total_bytes:
        _log(log, f"Gemma E2B q4 모델 다운로드 시작 ({_format_bytes(total_bytes)})")
    else:
        _log(log, "Gemma E2B q4 모델 다운로드 시작")
    tracker = _HuggingFaceDownloadProgress(total_bytes=total_bytes, log=log, progress=progress)
    snapshot_download(
        config.model_repo,
        local_dir=model_dir,
        allow_patterns=list(GEMMA_E2B_REQUIRED_FILES),
        max_workers=4,
        tqdm_class=tracker.tqdm_class(),
    )
    missing = _missing_gemma_required_files(config)
    if missing:
        raise RuntimeError(f"Gemma E2B 모델 파일이 누락되었습니다: {', '.join(missing[:3])}")
    _emit_progress(progress, 95.0)
    _log(log, "Gemma E2B q4 모델 다운로드 완료")
    return model_dir


class _HuggingFaceDownloadProgress:
    def __init__(
        self,
        *,
        total_bytes: int,
        log: Callable[[str], None] | None,
        progress: Callable[[float | None], None] | None,
    ) -> None:
        self.total_bytes = max(total_bytes, 0)
        self.downloaded_bytes = 0
        self.log = log
        self.progress = progress
        self.lock = threading.Lock()
        self.started_at = time.monotonic()
        self.last_log_at = self.started_at
        self.last_percent = -1.0
        self.next_log_percent = 5

    def tqdm_class(self) -> type:
        tracker = self

        class ProgressBar:
            _lock = threading.RLock()

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.iterable = args[0] if args else None
                self.unit = str(kwargs.get("unit") or "")
                self.total = kwargs.get("total") or 0
                self.n = kwargs.get("initial") or 0
                if self.unit == "B" and self.n:
                    tracker.update_bytes(self.n)

            @classmethod
            def get_lock(cls) -> threading.RLock:
                return cls._lock

            @classmethod
            def set_lock(cls, lock: threading.RLock) -> None:
                cls._lock = lock

            def __iter__(self):
                if self.iterable is None:
                    return
                for item in self.iterable:
                    yield item
                    self.update(1)

            def __enter__(self):
                return self

            def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
                return None

            def update(self, n: int | float | None = 1) -> None:
                amount = _to_float(n) or 0.0
                self.n += amount
                if self.unit == "B" and amount:
                    tracker.update_bytes(amount)

            def refresh(self) -> None:
                return None

            def close(self) -> None:
                return None

            def set_description(self, _description: str) -> None:
                return None

        return ProgressBar

    def update_bytes(self, amount: int | float) -> None:
        with self.lock:
            self.downloaded_bytes += int(amount)
            if self.total_bytes <= 0:
                _emit_progress(self.progress, None)
                return
            now = time.monotonic()
            elapsed = max(now - self.started_at, 0.001)
            speed = self.downloaded_bytes / elapsed
            remaining_bytes = max(self.total_bytes - self.downloaded_bytes, 0)
            eta_seconds = remaining_bytes / speed if speed > 0 else None
            percent = max(0.0, min((self.downloaded_bytes / self.total_bytes) * 95.0, 95.0))
            if percent < self.last_percent:
                return
            if percent - self.last_percent >= 0.5 or percent >= 95.0:
                self.last_percent = percent
                _emit_progress(self.progress, percent)
            display_percent = int((percent / 95.0) * 100.0)
            should_log = (
                display_percent >= self.next_log_percent
                or now - self.last_log_at >= 2.0
                or percent >= 95.0
            )
            if should_log:
                self.last_log_at = now
                _log(
                    self.log,
                    f"Gemma E2B 다운로드: {display_percent}% "
                    f"({_format_bytes(self.downloaded_bytes)} / {_format_bytes(self.total_bytes)}, "
                    f"{_format_transfer_rate(speed)}, ETA {_format_eta(eta_seconds)})",
                )
                while self.next_log_percent <= display_percent:
                    self.next_log_percent += 5


def _emit_progress(progress: Callable[[float | None], None] | None, value: float | None) -> None:
    if progress:
        progress(value)


def _to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _prompt_input(info: dict[str, Any], reference: TrackMetadata) -> dict[str, str]:
    del reference
    return {
        "video_title": squash_spaces(str(info.get("fulltitle") or info.get("title") or info.get("track") or "")),
        "video_channel": squash_spaces(str(info.get("channel") or "")),
        "video_uploader": squash_spaces(str(info.get("uploader") or "")),
        "video_creator": squash_spaces(str(info.get("creator") or "")),
        "video_description": _description_excerpt(str(info.get("description") or "")),
    }


def _parse_model_json(output: str) -> dict[str, Any]:
    text = squash_spaces(output)
    if not text:
        raise ValueError("empty Gemma output")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Gemma output did not contain a JSON object")


def _parse_suggestion_output(
    output: str,
    *,
    log: Callable[[str], None] | None,
    final: bool,
) -> tuple[dict[str, Any] | None, TrackMetadata | None]:
    try:
        parsed = _parse_model_json(output)
        metadata = _metadata_from_model_json(parsed)
    except Exception as exc:
        action = "생략" if final else "JSON 파싱 실패"
        _log(log, f"Gemma E2B fallback {action}: {exc}; 원본 출력: {_model_output_excerpt(output)}")
        return None, None
    if not metadata:
        action = "생략" if final else "JSON 필드 누락"
        _log(log, f"Gemma E2B fallback {action}: title/artist 누락; 원본 출력: {_model_output_excerpt(output)}")
        return parsed, None
    return parsed, metadata


def _model_output_excerpt(output: str, *, limit: int = 500) -> str:
    text = squash_spaces(output)
    if not text:
        return "<empty>"
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _gemma_context_key(info: dict[str, Any], reference: TrackMetadata) -> str:
    for key in ("webpage_url", "original_url", "url"):
        value = squash_spaces(str(info.get(key) or ""))
        if value:
            return value
    extractor = squash_spaces(str(info.get("extractor_key") or info.get("extractor") or ""))
    source_id = squash_spaces(str(info.get("id") or info.get("display_id") or ""))
    if source_id:
        return f"{extractor}:{source_id}" if extractor else source_id
    fallback = "|".join(
        part
        for part in (
            reference.artist,
            reference.title,
            squash_spaces(str(info.get("channel") or info.get("uploader") or "")),
        )
        if part
    )
    return fallback[:500]


def _metadata_from_model_json(payload: dict[str, Any]) -> TrackMetadata | None:
    title = squash_spaces(str(payload.get("title") or ""))
    artist = squash_spaces(str(payload.get("artist") or ""))
    if not title or not artist:
        return None
    metadata = clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist))
    return metadata if metadata.is_minimum_viable() else None


def _model_metadata_is_supported(metadata: TrackMetadata, info: dict[str, Any], reference: TrackMetadata) -> bool:
    del reference
    source = squash_spaces(
        " ".join(
            str(value or "")
            for value in (
                info.get("fulltitle"),
                info.get("title"),
                info.get("track"),
                info.get("channel"),
                info.get("uploader"),
                info.get("creator"),
                info.get("description"),
            )
        )
    )
    if not source:
        return False
    return _supported_text(metadata.title, source) and _supported_text(metadata.artist, source)


def _supported_text(value: str, source: str) -> bool:
    value = squash_spaces(value)
    if not value:
        return False
    if value.casefold() in source.casefold():
        return True
    tokens = [token for token in re.split(r"[\s,;/|()\[\]{}<>\"'「」『』:：-]+", value) if len(token) >= 2]
    meaningful_tokens = [token for token in tokens if token.casefold() not in GENERIC_SUPPORT_TOKENS]
    if meaningful_tokens and any(token.casefold() in source.casefold() for token in meaningful_tokens):
        return True
    return text_similarity(value, source) >= 0.55


def _has_strong_external_candidate(candidates: list[MetadataCandidate]) -> bool:
    for candidate in candidates:
        provider = candidate.provider.casefold()
        if candidate.score >= 0.85 and (
            "musicbrainz" in provider or "acoustid" in provider or "ytmusic" in provider or provider == "soundcloud"
        ):
            return True
    return False


def _is_missing_local_model_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return (
        "local_files_only=true" in message
        or "env.allowremotemodels=false" in message
        or "file was not found locally" in message
    )


def _invalidate_gemma_marker(config: GemmaE2BConfig) -> None:
    try:
        _gemma_marker_path(config).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _gemma_cache_dir(config: GemmaE2BConfig) -> Path:
    return config.cache_dir or user_cache_path("CueForge") / "transformersjs"


def _gemma_model_dir(config: GemmaE2BConfig) -> Path:
    return _gemma_cache_dir(config) / "models" / config.model_repo


def _gemma_marker_path(config: GemmaE2BConfig) -> Path:
    return _gemma_cache_dir(config) / DEFAULT_GEMMA_E2B_MARKER


def _gemma_required_remote_bytes(config: GemmaE2BConfig) -> int:
    try:
        files = HfApi().list_repo_tree(config.model_repo, recursive=True, expand=True)
    except Exception:
        return 0
    total = 0
    for file_info in files:
        path = str(getattr(file_info, "path", "") or "")
        if not _is_required_gemma_file(path):
            continue
        try:
            size = int(getattr(file_info, "size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        total += max(size, 0)
    return total


def _is_required_gemma_file(path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in GEMMA_E2B_REQUIRED_FILES)


def _gemma_required_files_present(config: GemmaE2BConfig) -> bool:
    return not _missing_gemma_required_files(config)


def _missing_gemma_required_files(config: GemmaE2BConfig) -> list[str]:
    model_dir = _gemma_model_dir(config)
    return [file_name for file_name in GEMMA_E2B_REQUIRED_FILES if not (model_dir / file_name).is_file()]


def _path_for_js(path: Path) -> str:
    return path.as_posix()


def _format_bytes(value: int | float) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def _format_transfer_rate(bytes_per_second: int | float) -> str:
    if bytes_per_second <= 0:
        return "속도 계산 중"
    return f"{_format_bytes(bytes_per_second)}/s"


def _format_eta(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return "계산 중"
    rounded = int(round(seconds))
    if rounded < 60:
        return f"{rounded}초"
    minutes, secs = divmod(rounded, 60)
    if minutes < 60:
        return f"{minutes}분 {secs:02d}초"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes:02d}분"


def _description_excerpt(description: str, *, limit: int = 4000) -> str:
    lines = [squash_spaces(line) for line in description.splitlines()]
    return "\n".join(line for line in lines if line)[:limit].rstrip()


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)


_GEMMA_DENO_SESSION_SCRIPT = r"""
import { env, pipeline } from "npm:@huggingface/transformers";

let generatorPromise = null;
let generatorKey = "";
const songContexts = new Map();
const maxSongContexts = 128;

async function generatorFor(input) {
  env.cacheDir = input.cacheDir;
  env.allowLocalModels = true;
  env.allowRemoteModels = Boolean(input.allowDownload);
  const modelPath = input.modelPath || input.model;
  const key = JSON.stringify([modelPath, input.cacheDir, Boolean(input.allowDownload)]);
  if (!generatorPromise || generatorKey !== key) {
    generatorKey = key;
    generatorPromise = pipeline("text-generation", modelPath, {
      dtype: "q4",
      cache_dir: input.cacheDir,
      local_files_only: !Boolean(input.allowDownload),
    });
  }
  try {
    return await generatorPromise;
  } catch (error) {
    generatorPromise = null;
    generatorKey = "";
    throw error;
  }
}

async function handleRequest(input) {
  const generator = await generatorFor(input);
  if (input.mode === "prepare") {
    return JSON.stringify({ ok: true });
  }

  const prompt = promptFor(input);
  const result = await generator(prompt, {
    max_new_tokens: input.maxNewTokens ?? 96,
    do_sample: false,
    temperature: 0,
    return_full_text: false,
  });
  const output = generatedText(result);
  rememberContext(input, [...prompt, { role: "assistant", content: output }]);
  return output;
}

function promptFor(input) {
  if (input.mode === "repair") {
    const contextKey = String(input.contextKey ?? "");
    const previous = contextKey ? songContexts.get(contextKey) : null;
    const base = previous ?? buildBasePrompt(input);
    return [
      ...base,
      {
        role: "user",
        content:
          "Your previous answer for this same track was not valid compact JSON or missed title/artist. Using only the same source text and your previous answer, return exactly one compact JSON object and nothing else. Use this exact schema: {\"title\":\"...\",\"artist\":\"...\",\"reason\":\"...\"}. Do not use Markdown, prose, code fences, comments, arrays, or extra keys.\n\nPREVIOUS ANSWER:\n" +
          String(input.badOutput ?? ""),
      },
    ];
  }
  return buildBasePrompt(input);
}

function buildBasePrompt(input) {
  const prompt = [
    {
      role: "system",
      content:
        "Extract track metadata from one noisy YouTube video. Read VIDEO DESCRIPTION line by line and use it together with VIDEO TITLE as the primary evidence. Consider credit-like lines in the description, including title, artist, vocal, chorus, cover, performer, singer, music, composer, lyrics, and arrangement credits, but do not assume every such line is the final artist/title. For cover or performance videos, extract metadata for the performed recording, not for the original source work. Original, music, lyrics, composer, and arrangement credits may describe the source work or production credits; do not use those credits as the track artist when an explicit vocal, chorus, cover, performed by, singer, channel, or uploader performer is present. Prefer explicit performer or artist credits for the track artist when they are present and consistent with the rest of the text; use composer/music credits only if no performer is identified. Prefer a concise display song title. When the video title starts with a localized/display title and later adds bracketed original titles, alternate-language titles, composer/original-artist names, performer names, COVER/MV/live labels, or other packaging text, keep only the display title. Treat channel, uploader, project names, franchise names, album/OST section names, MV labels, and other packaging text as context, not as the track artist or title, unless the source text clearly credits them that way. When a performer name appears in local script with a romanized alias in parentheses or a trailing uppercase alias, prefer the local-script display name unless only the romanized form is present. Do not swap artist and title. Output must be exactly one compact JSON object and nothing else. Use this exact schema: {\"title\":\"...\",\"artist\":\"...\",\"reason\":\"...\"}. Do not use Markdown, prose, code fences, comments, arrays, or extra keys. Do not omit any key. Do not invent values that are not supported by the provided text.",
    },
    {
      role: "user",
      content: renderPromptInput(input.input),
    },
  ];
  return prompt;
}

function rememberContext(input, messages) {
  const contextKey = String(input.contextKey ?? "");
  if (!contextKey) {
    return;
  }
  if (!songContexts.has(contextKey) && songContexts.size >= maxSongContexts) {
    const oldest = songContexts.keys().next().value;
    songContexts.delete(oldest);
  }
  songContexts.set(contextKey, messages);
}

function renderPromptInput(value) {
  return [
    `VIDEO TITLE:\n${value?.video_title ?? ""}`,
    `VIDEO CHANNEL:\n${value?.video_channel ?? ""}`,
    `VIDEO UPLOADER:\n${value?.video_uploader ?? ""}`,
    `VIDEO CREATOR:\n${value?.video_creator ?? ""}`,
    `VIDEO DESCRIPTION:\n${value?.video_description ?? ""}`,
  ].join("\n\n");
}

function generatedText(value) {
  const item = Array.isArray(value) ? value[0] : value;
  const generated = item?.generated_text ?? item;
  if (Array.isArray(generated)) {
    const last = generated[generated.length - 1];
    return typeof last === "string" ? last : (last?.content ?? JSON.stringify(last));
  }
  return String(generated ?? "");
}

function sendResponse(input, payload) {
  console.log(JSON.stringify({
    cueforgeResponse: true,
    requestId: input?.requestId ?? "",
    ...payload,
  }));
}

async function handleLine(line) {
  let input;
  try {
    input = JSON.parse(line);
  } catch (error) {
    sendResponse({ requestId: "" }, { ok: false, error: String(error?.message ?? error) });
    return;
  }
  try {
    const output = await handleRequest(input);
    sendResponse(input, { ok: true, output });
  } catch (error) {
    sendResponse(input, {
      ok: false,
      error: String(error?.stack ?? error?.message ?? error),
    });
  }
}

let buffer = "";
for await (const chunk of Deno.stdin.readable.pipeThrough(new TextDecoderStream())) {
  buffer += chunk;
  let newlineIndex = buffer.indexOf("\n");
  while (newlineIndex >= 0) {
    const line = buffer.slice(0, newlineIndex).trim();
    buffer = buffer.slice(newlineIndex + 1);
    if (line) {
      await handleLine(line);
    }
    newlineIndex = buffer.indexOf("\n");
  }
}
const tail = buffer.trim();
if (tail) {
  await handleLine(tail);
}
"""
