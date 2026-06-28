"""Gemma E2B fallback metadata suggestions through Deno + Transformers.js."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from platformdirs import user_cache_path

from cueforge.metadata.matching import text_similarity
from cueforge.metadata.normalize import clean_metadata, squash_spaces
from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.runtime import find_executable

DEFAULT_GEMMA_E2B_MODEL_REPO = "onnx-community/gemma-4-E2B-it-ONNX"
DEFAULT_GEMMA_E2B_MARKER = "gemma-e2b-it.ready.json"
DEFAULT_GEMMA_E2B_MARKER_VERSION = 2
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
        self._runner = runner or _run_gemma_script

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
        payload = {
            "mode": "suggest",
            "model": self.config.model_repo,
            "cacheDir": str(_gemma_cache_dir(self.config)),
            "allowDownload": self.config.allow_download,
            "maxNewTokens": self.config.max_new_tokens,
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
        try:
            parsed = _parse_model_json(output)
            metadata = _metadata_from_model_json(parsed)
        except Exception as exc:
            _log(log, f"Gemma E2B fallback 생략: {exc}")
            return []
        if not metadata or not _model_metadata_is_supported(metadata, info, reference):
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
    return payload.get("model") == resolved.model_repo and payload.get("marker_version") == DEFAULT_GEMMA_E2B_MARKER_VERSION


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
        "cacheDir": str(_gemma_cache_dir(resolved)),
        "allowDownload": resolved.allow_download,
        "maxNewTokens": 1,
        "emitProgress": True,
    }
    _log(log, "Gemma E2B 모델 준비 중")
    _emit_progress(progress, 0.0)
    if runner:
        runner(payload, resolved)
    else:
        _run_gemma_script(payload, resolved, log=log, progress=progress)
    marker = _gemma_marker_path(resolved)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "model": resolved.model_repo,
                "marker_version": DEFAULT_GEMMA_E2B_MARKER_VERSION,
                "cache_dir": str(_gemma_cache_dir(resolved)),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _emit_progress(progress, 100.0)
    _log(log, "Gemma E2B 모델 준비 완료")


def _run_gemma_script(
    payload: dict[str, Any],
    config: GemmaE2BConfig,
    *,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float | None], None] | None = None,
) -> str:
    deno = config.deno_path or find_executable("deno").path
    if not deno:
        raise RuntimeError("Deno executable not found")
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as script_file:
        script_file.write(_GEMMA_DENO_SCRIPT)
        script_path = Path(script_file.name)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    returncode = -1
    try:
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
                str(script_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        stdout_thread = threading.Thread(target=_read_stream, args=(process.stdout, stdout_lines), daemon=True)
        stderr_thread = threading.Thread(
            target=_read_gemma_stderr,
            args=(process.stderr, stderr_lines, log, progress),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        if process.stdin:
            process.stdin.write(json.dumps(payload, ensure_ascii=False))
            process.stdin.close()
        try:
            returncode = process.wait(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise RuntimeError(f"Deno timed out after {config.timeout_seconds}s") from exc
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    if returncode != 0:
        message = (stderr or stdout).strip()
        raise RuntimeError(message or f"Deno exited with {returncode}")
    return stdout


def _read_stream(stream: Any, lines: list[str]) -> None:
    if not stream:
        return
    for line in stream:
        lines.append(line)


def _read_gemma_stderr(
    stream: Any,
    lines: list[str],
    log: Callable[[str], None] | None,
    progress: Callable[[float | None], None] | None,
) -> None:
    if not stream:
        return
    for line in stream:
        if _handle_gemma_progress_line(line.strip(), log=log, progress=progress):
            continue
        lines.append(line)


def _handle_gemma_progress_line(
    line: str,
    *,
    log: Callable[[str], None] | None,
    progress: Callable[[float | None], None] | None,
) -> bool:
    if not line:
        return False
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict) or payload.get("cueforgeProgress") is not True:
        return False
    percent = _progress_percent(payload)
    if percent is not None:
        _emit_progress(progress, percent)
    file_label = _progress_file_label(payload)
    status = str(payload.get("status") or "").strip()
    if file_label and percent is not None:
        _log(log, f"Gemma E2B 다운로드: {file_label} {percent:.0f}%")
    elif file_label:
        _log(log, f"Gemma E2B 다운로드: {file_label}")
    elif status:
        _log(log, f"Gemma E2B 다운로드: {status}")
    return True


def _progress_percent(payload: dict[str, Any]) -> float | None:
    value = _to_float(payload.get("progress"))
    if value is not None:
        return max(0.0, min(value, 100.0))
    loaded = _to_float(payload.get("loaded"))
    total = _to_float(payload.get("total"))
    if loaded is not None and total and total > 0:
        return max(0.0, min((loaded / total) * 100.0, 100.0))
    return None


def _progress_file_label(payload: dict[str, Any]) -> str:
    value = squash_spaces(str(payload.get("file") or payload.get("name") or ""))
    if not value:
        return ""
    return re.split(r"[\\/]", value)[-1] or value


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
    return {
        "video_title": squash_spaces(str(info.get("fulltitle") or info.get("title") or info.get("track") or reference.title)),
        "channel": squash_spaces(str(info.get("channel") or info.get("uploader") or info.get("creator") or reference.artist)),
        "description": _description_excerpt(str(info.get("description") or "")),
        "current_title": reference.title,
        "current_artist": reference.artist,
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


def _metadata_from_model_json(payload: dict[str, Any]) -> TrackMetadata | None:
    title = squash_spaces(str(payload.get("title") or ""))
    artist = squash_spaces(str(payload.get("artist") or ""))
    if not title or not artist:
        return None
    metadata = clean_metadata(TrackMetadata(title=title, artist=artist, album_artist=artist))
    return metadata if metadata.is_minimum_viable() else None


def _model_metadata_is_supported(metadata: TrackMetadata, info: dict[str, Any], reference: TrackMetadata) -> bool:
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
                reference.title,
                reference.artist,
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


def _gemma_marker_path(config: GemmaE2BConfig) -> Path:
    return _gemma_cache_dir(config) / DEFAULT_GEMMA_E2B_MARKER


def _description_excerpt(description: str, *, limit: int = 900) -> str:
    lines = [squash_spaces(line) for line in description.splitlines()]
    return " ".join(line for line in lines if line)[:limit].rstrip()


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)


_GEMMA_DENO_SCRIPT = r"""
import { env, pipeline } from "npm:@huggingface/transformers";

const input = JSON.parse(await new Response(Deno.stdin.readable).text());
env.cacheDir = input.cacheDir;
env.allowLocalModels = true;
env.allowRemoteModels = Boolean(input.allowDownload);

function reportProgress(payload) {
  if (!input.emitProgress) {
    return;
  }
  const loaded = Number(payload?.loaded ?? 0);
  const total = Number(payload?.total ?? 0);
  let progress = Number(payload?.progress);
  if (!Number.isFinite(progress) && total > 0) {
    progress = (loaded / total) * 100;
  }
  console.error(JSON.stringify({
    cueforgeProgress: true,
    status: payload?.status ?? "",
    file: payload?.file ?? payload?.name ?? "",
    progress: Number.isFinite(progress) ? progress : null,
    loaded: Number.isFinite(loaded) ? loaded : null,
    total: Number.isFinite(total) ? total : null,
  }));
}

const generator = await pipeline("text-generation", input.model, {
  dtype: "q4",
  progress_callback: reportProgress,
});

if (input.mode === "prepare") {
  console.log(JSON.stringify({ ok: true }));
  Deno.exit(0);
}

const prompt = [
  {
    role: "system",
    content:
      "Extract music metadata from noisy YouTube text. Return only compact JSON with title, artist, and reason. Do not invent values that are not supported by the provided text.",
  },
  {
    role: "user",
    content: JSON.stringify(input.input),
  },
];

const result = await generator(prompt, {
  max_new_tokens: input.maxNewTokens ?? 96,
  do_sample: false,
  temperature: 0,
  return_full_text: false,
});

function generatedText(value) {
  const item = Array.isArray(value) ? value[0] : value;
  const generated = item?.generated_text ?? item;
  if (Array.isArray(generated)) {
    const last = generated[generated.length - 1];
    return typeof last === "string" ? last : (last?.content ?? JSON.stringify(last));
  }
  return String(generated ?? "");
}

console.log(generatedText(result));
"""
