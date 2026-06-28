"""ONNX-backed semantic scoring for metadata candidates."""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from cueforge.metadata.normalize import squash_spaces
from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.runtime import app_root

DEFAULT_SEMANTIC_MODEL_REPO = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_SEMANTIC_MODEL_FILE = "onnx/model_quantized.onnx"
DEFAULT_SEMANTIC_TOKENIZER_FILE = "tokenizer.json"


class SemanticEmbeddingModel(Protocol):
    def similarity(self, left: str, right: str) -> float: ...


@dataclass(frozen=True, slots=True)
class SemanticRankerConfig:
    enabled: bool = True
    allow_download: bool = False
    model_repo: str = DEFAULT_SEMANTIC_MODEL_REPO
    model_file: str = DEFAULT_SEMANTIC_MODEL_FILE
    tokenizer_file: str = DEFAULT_SEMANTIC_TOKENIZER_FILE
    model_dir: Path | None = None
    max_length: int = 128
    title_hint_cap: float = 0.84


class SemanticCandidateRanker:
    def __init__(
        self,
        config: SemanticRankerConfig | None = None,
        *,
        model: SemanticEmbeddingModel | None = None,
    ) -> None:
        self.config = config or SemanticRankerConfig()
        self._model = model

    def rerank(
        self,
        *,
        info: dict[str, Any],
        reference: TrackMetadata,
        candidates: list[MetadataCandidate],
        log: Callable[[str], None] | None = None,
    ) -> list[MetadataCandidate]:
        if not self.config.enabled or not candidates:
            return candidates
        try:
            _log(log, "MiniLM 후보 평가 준비")
            model = self._model or _load_model(self.config)
        except Exception as exc:
            _log(log, f"MiniLM 후보 평가 생략: {exc}")
            return candidates

        source_text = _source_text(info, reference)
        if not source_text:
            return candidates

        ranked = [
            _candidate_with_semantic_score(
                candidate,
                score=model.similarity(source_text, _candidate_text(candidate.metadata)),
                config=self.config,
            )
            for candidate in candidates
        ]
        ranked.sort(key=lambda candidate: candidate.score, reverse=True)
        best = ranked[0]
        if best.raw.get("semantic_score") is not None:
            _log(log, f"MiniLM 후보 평가: {best.provider} {best.raw['semantic_score']:.2f}")
        return ranked


class _OnnxEmbeddingModel:
    def __init__(self, *, model_path: Path, tokenizer_path: Path, max_length: int) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.max_length = max_length
        self.input_names = {item.name for item in self.session.get_inputs()}

    def similarity(self, left: str, right: str) -> float:
        embeddings = self._embed([left, right])
        return _cosine_similarity(embeddings[0], embeddings[1])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        encodings = self.tokenizer.encode_batch(texts)
        max_len = min(
            self.max_length,
            max(1, max((len(encoding.ids) for encoding in encodings), default=1)),
        )
        input_ids = np.zeros((len(encodings), max_len), dtype=np.int64)
        attention_mask = np.zeros((len(encodings), max_len), dtype=np.int64)
        token_type_ids = np.zeros((len(encodings), max_len), dtype=np.int64)

        for row, encoding in enumerate(encodings):
            length = min(len(encoding.ids), max_len)
            input_ids[row, :length] = encoding.ids[:length]
            attention_mask[row, :length] = encoding.attention_mask[:length] or [1] * length
            if encoding.type_ids:
                token_type_ids[row, :length] = encoding.type_ids[:length]

        inputs: dict[str, Any] = {}
        if "input_ids" in self.input_names:
            inputs["input_ids"] = input_ids
        if "attention_mask" in self.input_names:
            inputs["attention_mask"] = attention_mask
        if "token_type_ids" in self.input_names:
            inputs["token_type_ids"] = token_type_ids

        output = self.session.run(None, inputs)[0]
        if output.ndim == 2:
            embeddings = output
        else:
            mask = attention_mask[..., None].astype(np.float32)
            summed = (output * mask).sum(axis=1)
            counts = np.maximum(mask.sum(axis=1), 1e-9)
            embeddings = summed / counts
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        return (embeddings / np.maximum(norms, 1e-9)).astype(np.float32).tolist()


_MODEL_CACHE: dict[tuple[Any, ...], _OnnxEmbeddingModel] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _load_model(config: SemanticRankerConfig) -> _OnnxEmbeddingModel:
    key = (
        config.model_repo,
        config.model_file,
        config.tokenizer_file,
        str(config.model_dir or ""),
        config.max_length,
        config.allow_download,
    )
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached:
            return cached
        model_path, tokenizer_path = _resolve_model_files(config)
        model = _OnnxEmbeddingModel(model_path=model_path, tokenizer_path=tokenizer_path, max_length=config.max_length)
        _MODEL_CACHE[key] = model
        return model


def _resolve_model_files(config: SemanticRankerConfig) -> tuple[Path, Path]:
    model_dir = config.model_dir or _bundled_model_dir()
    if model_dir:
        model_path = model_dir / config.model_file
        tokenizer_path = model_dir / config.tokenizer_file
        if model_path.exists() and tokenizer_path.exists():
            return model_path, tokenizer_path

    from huggingface_hub import hf_hub_download

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    model_path = _hf_cached_or_downloaded(
        hf_hub_download,
        repo_id=config.model_repo,
        filename=config.model_file,
        allow_download=config.allow_download,
    )
    tokenizer_path = _hf_cached_or_downloaded(
        hf_hub_download,
        repo_id=config.model_repo,
        filename=config.tokenizer_file,
        allow_download=config.allow_download,
    )
    return model_path, tokenizer_path


def _hf_cached_or_downloaded(
    hf_hub_download: Callable[..., str],
    *,
    repo_id: str,
    filename: str,
    allow_download: bool,
) -> Path:
    try:
        return Path(hf_hub_download(repo_id=repo_id, filename=filename, local_files_only=True))
    except Exception:
        if not allow_download:
            raise
    return Path(hf_hub_download(repo_id=repo_id, filename=filename, local_files_only=False))


def _bundled_model_dir() -> Path:
    return app_root() / "models" / "semantic-ranker"


def _candidate_with_semantic_score(
    candidate: MetadataCandidate,
    *,
    score: float,
    config: SemanticRankerConfig,
) -> MetadataCandidate:
    score = max(0.0, min(float(score), 1.0))
    adjusted = round((candidate.score * 0.82) + (score * 0.18), 3)
    if _is_unverified_title_hint(candidate):
        adjusted = min(adjusted, config.title_hint_cap)
    matched = list(candidate.matched_fields)
    if score >= 0.70 and "semantic" not in matched:
        matched.append("semantic")
    return MetadataCandidate(
        provider=candidate.provider,
        metadata=candidate.metadata,
        score=max(0.0, min(adjusted, 1.0)),
        matched_fields=tuple(matched),
        raw={
            **candidate.raw,
            "semantic_score": round(score, 3),
            "semantic_model": config.model_repo,
        },
    )


def _is_unverified_title_hint(candidate: MetadataCandidate) -> bool:
    provider = candidate.provider.casefold()
    return provider.startswith(("title_", "description_"))


def _source_text(info: dict[str, Any], reference: TrackMetadata) -> str:
    description = _description_excerpt(str(info.get("description") or ""))
    parts = [
        f"video title: {info.get('fulltitle') or info.get('title') or info.get('track') or reference.title}",
        f"channel: {info.get('channel') or info.get('uploader') or info.get('creator') or reference.artist}",
    ]
    if description:
        parts.append(f"description: {description}")
    return squash_spaces(" | ".join(str(part) for part in parts if part))


def _candidate_text(metadata: TrackMetadata) -> str:
    return squash_spaces(
        " | ".join(
            part
            for part in (
                f"title: {metadata.title}" if metadata.title else "",
                f"artist: {metadata.artist}" if metadata.artist else "",
                f"album: {metadata.album}" if metadata.album else "",
            )
            if part
        )
    )


def _description_excerpt(description: str, *, limit: int = 600) -> str:
    lines = [squash_spaces(line) for line in description.splitlines()]
    excerpt = " ".join(line for line in lines if line)
    return excerpt[:limit].rstrip()


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(dot / (left_norm * right_norm), 1.0))


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if log:
        log(message)
