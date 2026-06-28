import json
from pathlib import Path

import pytest

from cueforge.metadata.gemma import (
    GEMMA_E2B_REQUIRED_FILES,
    GemmaE2BConfig,
    GemmaE2BMetadataSuggester,
    _HuggingFaceDownloadProgress,
    _gemma_model_dir,
    _run_gemma_script,
    gemma_e2b_cached,
    prepare_gemma_e2b,
)
from cueforge.models import MetadataCandidate, TrackMetadata


def test_gemma_suggester_maps_json_output_to_review_candidate(tmp_path: Path) -> None:
    def runner(payload, config):
        assert payload["mode"] == "suggest"
        assert payload["model"] == config.model_repo
        return '{"title":"Correct Song","artist":"Correct Artist","reason":"visible in title"}'

    suggester = GemmaE2BMetadataSuggester(
        GemmaE2BConfig(cache_dir=tmp_path),
        runner=runner,
    )

    candidates = suggester.suggest(
        info={"title": "Correct Artist - Correct Song", "uploader": "Correct Artist"},
        reference=TrackMetadata(title="Correct Artist - Correct Song", artist="Correct Artist"),
        candidates=[],
    )

    assert len(candidates) == 1
    assert candidates[0].provider == "gemma_e2b"
    assert candidates[0].score == 0.0
    assert candidates[0].metadata.title == "Correct Song"
    assert candidates[0].metadata.artist == "Correct Artist"
    assert candidates[0].raw["review_only"] is True
    assert candidates[0].raw["requires_semantic_score"] is True


def test_gemma_suggester_discards_unsupported_hallucination(tmp_path: Path) -> None:
    suggester = GemmaE2BMetadataSuggester(
        GemmaE2BConfig(cache_dir=tmp_path),
        runner=lambda payload, config: '{"title":"Unrelated Song","artist":"Imagined Artist"}',
    )

    candidates = suggester.suggest(
        info={"title": "Correct Artist - Correct Song", "uploader": "Correct Artist"},
        reference=TrackMetadata(title="Correct Artist - Correct Song", artist="Correct Artist"),
        candidates=[],
    )

    assert candidates == []


def test_gemma_suggester_skips_when_strong_external_candidate_exists(tmp_path: Path) -> None:
    called = False

    def runner(payload, config):
        nonlocal called
        called = True
        return "{}"

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path), runner=runner)

    candidates = suggester.suggest(
        info={"title": "Correct Artist - Correct Song"},
        reference=TrackMetadata(title="Correct Artist - Correct Song", artist="Correct Artist"),
        candidates=[
            MetadataCandidate(
                provider="musicbrainz",
                score=0.95,
                matched_fields=("title", "artist"),
                metadata=TrackMetadata(title="Correct Song", artist="Correct Artist"),
            )
        ],
    )

    assert candidates == []
    assert called is False


def test_gemma_suggester_skips_when_required_model_is_not_cached(tmp_path: Path) -> None:
    logs: list[str] = []
    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path, allow_download=False))

    candidates = suggester.suggest(
        info={"title": "Correct Artist - Correct Song"},
        reference=TrackMetadata(title="Correct Song", artist="Correct Artist"),
        candidates=[],
        log=logs.append,
    )

    assert candidates == []
    assert any("아직 준비되지 않아" in message for message in logs)


def test_gemma_suggester_raises_when_model_execution_fails(tmp_path: Path) -> None:
    def runner(payload, config):
        raise RuntimeError("download failed")

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path), runner=runner)

    with pytest.raises(RuntimeError, match="Gemma E2B"):
        suggester.suggest(
            info={"title": "Correct Artist - Correct Song"},
            reference=TrackMetadata(title="Correct Song", artist="Correct Artist"),
            candidates=[],
        )


def test_gemma_suggester_skips_and_invalidates_missing_local_model(tmp_path: Path) -> None:
    marker = tmp_path / "gemma-e2b-it.ready.json"
    marker.write_text('{"model":"onnx-community/gemma-4-E2B-it-ONNX","marker_version":3}', encoding="utf-8")
    logs: list[str] = []

    def runner(payload, config):
        raise RuntimeError("local_files_only=true and file was not found locally at config.json")

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path, allow_download=False), runner=runner)

    candidates = suggester.suggest(
        info={"title": "Correct Artist - Correct Song"},
        reference=TrackMetadata(title="Correct Song", artist="Correct Artist"),
        candidates=[],
        log=logs.append,
    )

    assert candidates == []
    assert not marker.exists()
    assert any("모델 캐시가 없어" in message for message in logs)


def test_prepare_gemma_writes_ready_marker(tmp_path: Path) -> None:
    config = GemmaE2BConfig(cache_dir=tmp_path, allow_download=True)

    def runner(payload, resolved):
        assert payload["model"] == resolved.model_repo
        assert payload["modelPath"] == _gemma_model_dir(resolved).as_posix()
        assert payload["allowDownload"] is False
        _write_required_gemma_files(resolved)
        return '{"ok":true}'

    prepare_gemma_e2b(
        config,
        runner=runner,
    )

    assert gemma_e2b_cached(config) is True


def test_gemma_cache_rejects_marker_without_required_files(tmp_path: Path) -> None:
    config = GemmaE2BConfig(cache_dir=tmp_path)
    marker = tmp_path / "gemma-e2b-it.ready.json"
    marker.write_text(
        json.dumps(
            {
                "model": config.model_repo,
                "marker_version": 3,
                "model_dir": str(_gemma_model_dir(config)),
                "files": [],
            }
        ),
        encoding="utf-8",
    )

    assert gemma_e2b_cached(config) is False


def test_gemma_cache_rejects_legacy_marker(tmp_path: Path) -> None:
    config = GemmaE2BConfig(cache_dir=tmp_path)
    marker = tmp_path / "gemma-e2b-it.ready.json"
    marker.write_text('{"model":"onnx-community/gemma-4-E2B-it-ONNX"}', encoding="utf-8")

    assert gemma_e2b_cached(config) is False


def test_prepare_gemma_downloads_model_with_python_before_deno(monkeypatch, tmp_path: Path) -> None:
    config = GemmaE2BConfig(cache_dir=tmp_path, allow_download=True)
    model_dir = _gemma_model_dir(config)
    total_bytes = len(GEMMA_E2B_REQUIRED_FILES) * 100
    logs: list[str] = []
    progresses: list[float | None] = []
    run_payloads = []

    class RepoFile:
        def __init__(self, path: str) -> None:
            self.path = path
            self.size = 100

    class Api:
        def list_repo_tree(self, repo_id, **kwargs):
            assert repo_id == config.model_repo
            return [RepoFile(path) for path in GEMMA_E2B_REQUIRED_FILES]

    def fake_snapshot_download(repo_id, **kwargs):
        assert repo_id == config.model_repo
        assert kwargs["local_dir"] == model_dir
        assert kwargs["allow_patterns"] == list(GEMMA_E2B_REQUIRED_FILES)
        assert kwargs["max_workers"] == 4
        bar = kwargs["tqdm_class"](total=0, unit="B")
        bar.update(total_bytes // 2)
        bar.update(total_bytes - (total_bytes // 2))
        _write_required_gemma_files(config)
        return str(model_dir)

    def fake_run_gemma_script(payload, resolved, **kwargs):
        run_payloads.append(payload)
        assert resolved == config
        assert payload["model"] == config.model_repo
        assert payload["modelPath"] == model_dir.as_posix()
        assert payload["allowDownload"] is False
        return '{"ok":true}'

    monkeypatch.setattr("cueforge.metadata.gemma.HfApi", lambda: Api())
    monkeypatch.setattr("cueforge.metadata.gemma.snapshot_download", fake_snapshot_download)
    monkeypatch.setattr("cueforge.metadata.gemma._run_gemma_script", fake_run_gemma_script)

    prepare_gemma_e2b(config, log=logs.append, progress=progresses.append)

    assert run_payloads
    assert progresses[0] == 0.0
    assert any(value is not None and 40.0 <= value <= 60.0 for value in progresses)
    assert progresses[-1] == 100.0
    assert any("q4 모델 다운로드 시작" in message for message in logs)
    assert any("로컬 모델 실행 확인" in message for message in logs)
    assert gemma_e2b_cached(config) is True


def test_gemma_download_progress_reports_speed_and_eta(monkeypatch) -> None:
    ticks = iter([100.0, 102.0])
    logs: list[str] = []
    progresses: list[float | None] = []

    monkeypatch.setattr("cueforge.metadata.gemma.time.monotonic", lambda: next(ticks))

    tracker = _HuggingFaceDownloadProgress(total_bytes=1000, log=logs.append, progress=progresses.append)
    tracker.update_bytes(250)

    assert progresses == [23.75]
    assert logs == ["Gemma E2B 다운로드: 25% (250 B / 1000 B, 125 B/s, ETA 6초)"]


def test_gemma_runner_uses_deno_run_permissions(monkeypatch, tmp_path: Path) -> None:
    calls = []
    written = []
    logs = []
    progresses = []

    class Stdin:
        def write(self, value):
            written.append(value)

        def close(self):
            return None

    class Process:
        def __init__(self, args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.stdin = Stdin()
            self.stdout = ['{"ok":true}']
            self.stderr = ['{"cueforgeProgress":true,"file":"onnx/model.onnx","progress":50}\n']

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        script_path = Path(args[-1])
        assert script_path.exists()
        assert "npm:@huggingface/transformers" in script_path.read_text(encoding="utf-8")
        assert "local_files_only" in script_path.read_text(encoding="utf-8")
        assert "cache_dir" in script_path.read_text(encoding="utf-8")
        return Process(args, **kwargs)

    monkeypatch.setattr("cueforge.metadata.gemma.subprocess.Popen", fake_popen)

    output = _run_gemma_script(
        {"mode": "prepare"},
        GemmaE2BConfig(deno_path=tmp_path / "deno.exe"),
        log=logs.append,
        progress=progresses.append,
    )

    args, kwargs = calls[0]
    assert output == '{"ok":true}'
    assert args[1] == "run"
    assert "--no-prompt" not in args
    assert "--allow-env" in args
    assert "--allow-ffi" in args
    assert "--allow-net" in args
    assert written == ['{"mode": "prepare"}']
    assert progresses == [50.0]
    assert logs == ["Gemma E2B 다운로드: model.onnx 50%"]
    assert not Path(args[-1]).exists()


def _write_required_gemma_files(config: GemmaE2BConfig) -> None:
    model_dir = _gemma_model_dir(config)
    for file_name in GEMMA_E2B_REQUIRED_FILES:
        path = model_dir / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
