import json
import threading
from pathlib import Path

import pytest

from cueforge.metadata.gemma import (
    GEMMA_E2B_REQUIRED_FILES,
    GEMMA_E2B_PROMPT_VERSION,
    GemmaE2BConfig,
    GemmaE2BMetadataSuggester,
    _HuggingFaceDownloadProgress,
    _gemma_context_key,
    _gemma_model_dir,
    _prompt_input,
    _run_gemma_session,
    _shutdown_gemma_sessions,
    gemma_e2b_cached,
    prepare_gemma_e2b,
)
from cueforge.models import MetadataCandidate, TrackMetadata


def test_gemma_suggester_maps_json_output_to_review_candidate(tmp_path: Path) -> None:
    def runner(payload, config):
        assert payload["mode"] == "suggest"
        assert payload["model"] == config.model_repo
        return '{"title":"Correct Song","artist":"Correct Artist"}'

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
    assert "reason" not in candidates[0].raw


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


def test_gemma_suggester_logs_raw_output_when_json_parse_fails(tmp_path: Path) -> None:
    logs: list[str] = []
    suggester = GemmaE2BMetadataSuggester(
        GemmaE2BConfig(cache_dir=tmp_path),
        runner=lambda payload, config: "The likely title is Correct Song by Correct Artist.",
    )

    candidates = suggester.suggest(
        info={"title": "Correct Artist - Correct Song", "uploader": "Correct Artist"},
        reference=TrackMetadata(title="Correct Song", artist="Correct Artist"),
        candidates=[],
        log=logs.append,
    )

    assert candidates == []
    assert any("원본 출력: The likely title is Correct Song by Correct Artist." in message for message in logs)


def test_gemma_suggester_repairs_malformed_json_in_song_context(tmp_path: Path) -> None:
    calls = []

    def runner(payload, config):
        calls.append(payload)
        if payload["mode"] == "suggest":
            return "Title is Correct Song and artist is Correct Artist."
        assert payload["mode"] == "repair"
        assert payload["contextKey"] == "Youtube:abc123"
        assert payload["badOutput"] == "Title is Correct Song and artist is Correct Artist."
        return '{"title":"Correct Song","artist":"Correct Artist"}'

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path), runner=runner)

    candidates = suggester.suggest(
        info={"id": "abc123", "extractor_key": "Youtube", "title": "Correct Artist - Correct Song"},
        reference=TrackMetadata(title="Correct Song", artist="Correct Artist"),
        candidates=[],
    )

    assert len(candidates) == 1
    assert [call["mode"] for call in calls] == ["suggest", "repair"]
    assert calls[0]["contextKey"] == "Youtube:abc123"
    assert candidates[0].metadata.title == "Correct Song"
    assert candidates[0].metadata.artist == "Correct Artist"


def test_gemma_suggester_refines_noisy_video_title_candidate(tmp_path: Path) -> None:
    calls = []
    noisy_title = "출항 [抜錨(발묘) / 나나호시 관현악단] ㅣ아카네 리제(Akane Lize) 【COVER】"

    def runner(payload, config):
        calls.append(payload)
        if payload["mode"] == "suggest":
            assert payload["promptVersion"] == GEMMA_E2B_PROMPT_VERSION
            return (
                '{"title":"출항 [抜錨(발묘) / 나나호시 관현악단] ㅣ아카네 리제(Akane Lize) 【COVER】",'
                '"artist":"Akane Lize"}'
            )
        assert payload["mode"] == "refine"
        assert noisy_title in payload["badOutput"]
        return '{"title":"출항","artist":"아카네 리제"}'

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path), runner=runner)

    candidates = suggester.suggest(
        info={
            "id": "R_B4tmy2DVA",
            "extractor_key": "Youtube",
            "title": noisy_title,
            "channel": "아카네 리제 AKANE LIZE",
            "uploader": "아카네 리제 AKANE LIZE",
            "description": "Vocal 아카네 리제(Akane Lize)\nOriginal 나나호시 관현악단",
        },
        reference=TrackMetadata(title=noisy_title, artist="Akane Lize"),
        candidates=[],
    )

    assert [call["mode"] for call in calls] == ["suggest", "refine"]
    assert len(candidates) == 1
    assert candidates[0].metadata.title == "출항"
    assert candidates[0].metadata.artist == "아카네 리제"


def test_gemma_suggester_prefers_local_artist_alias_without_refine(tmp_path: Path) -> None:
    calls = []

    def runner(payload, config):
        calls.append(payload)
        assert payload["mode"] == "suggest"
        return '{"title":"출항","artist":"Akane Lize"}'

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path), runner=runner)

    candidates = suggester.suggest(
        info={
            "id": "R_B4tmy2DVA",
            "extractor_key": "Youtube",
            "title": "출항 [抜錨(발묘) / 나나호시 관현악단] ㅣ아카네 리제(Akane Lize) 【COVER】",
            "channel": "아카네 리제 AKANE LIZE",
            "uploader": "아카네 리제 AKANE LIZE",
            "description": "Vocal & Chorus : Akane Lize",
        },
        reference=TrackMetadata(title="출항", artist="아카네 리제"),
        candidates=[],
    )

    assert [call["mode"] for call in calls] == ["suggest"]
    assert len(candidates) == 1
    assert candidates[0].metadata.title == "출항"
    assert candidates[0].metadata.artist == "아카네 리제"
    assert candidates[0].metadata.album_artist == "아카네 리제"


def test_gemma_suggester_refines_original_artist_for_cover_video(tmp_path: Path) -> None:
    calls = []

    def runner(payload, config):
        calls.append(payload)
        if payload["mode"] == "suggest":
            assert payload["promptVersion"] == GEMMA_E2B_PROMPT_VERSION
            return '{"title":"꽃에 망령","artist":"Yorushika"}'
        assert payload["mode"] == "refine"
        assert "Yorushika" in payload["badOutput"]
        return '{"title":"꽃에 망령","artist":"계화"}'

    suggester = GemmaE2BMetadataSuggester(GemmaE2BConfig(cache_dir=tmp_path), runner=runner)

    candidates = suggester.suggest(
        info={
            "id": "Bmi16BwbccE",
            "extractor_key": "Youtube",
            "title": "꽃에 망령(花に亡霊) | 계화 COVER",
            "channel": "계화",
            "uploader": "계화",
            "description": "Original :: Yorushika (요루시카) - Ghost In A Flower (ヨルシカ - 花に亡霊)",
        },
        reference=TrackMetadata(title="꽃에 망령(花に亡霊) | 계화 COVER", artist="계화"),
        candidates=[],
    )

    assert [call["mode"] for call in calls] == ["suggest", "refine"]
    assert len(candidates) == 1
    assert candidates[0].metadata.title == "꽃에 망령"
    assert candidates[0].metadata.artist == "계화"


def test_gemma_context_key_prefers_url_then_source_id() -> None:
    assert (
        _gemma_context_key(
            {"webpage_url": "https://www.youtube.com/watch?v=abc123", "id": "ignored"},
            TrackMetadata(title="Song", artist="Artist"),
        )
        == "https://www.youtube.com/watch?v=abc123"
    )
    assert _gemma_context_key({"id": "abc123", "extractor_key": "Youtube"}, TrackMetadata()) == "Youtube:abc123"


def test_gemma_prompt_input_includes_only_source_video_fields() -> None:
    payload = _prompt_input(
        {
            "fulltitle": "Game OST - Actual Song (Official MV)",
            "channel": "Official Music Channel",
            "uploader": "Uploader Name",
            "creator": "Composer Name",
            "description": "Track: Actual Song\nArtist: Real Artist\nAlbum: Soundtrack",
        },
        TrackMetadata(title="Bad Guess", artist="Wrong Artist"),
    )

    assert payload == {
        "video_title": "Game OST - Actual Song (Official MV)",
        "video_channel": "Official Music Channel",
        "video_uploader": "Uploader Name",
        "video_creator": "Composer Name",
        "video_description": "Track: Actual Song\nArtist: Real Artist\nAlbum: Soundtrack",
    }


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

    def fake_run_gemma_session(payload, resolved):
        run_payloads.append(payload)
        assert resolved == GemmaE2BConfig(cache_dir=tmp_path, allow_download=False)
        assert payload["model"] == config.model_repo
        assert payload["modelPath"] == model_dir.as_posix()
        assert payload["allowDownload"] is False
        assert payload["promptVersion"] == GEMMA_E2B_PROMPT_VERSION
        return '{"ok":true}'

    monkeypatch.setattr("cueforge.metadata.gemma.HfApi", lambda: Api())
    monkeypatch.setattr("cueforge.metadata.gemma.snapshot_download", fake_snapshot_download)
    monkeypatch.setattr("cueforge.metadata.gemma._run_gemma_session", fake_run_gemma_session)

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

    class Stream:
        def __init__(self) -> None:
            self.items = []
            self.condition = threading.Condition()
            self.closed = False

        def push(self, value: str) -> None:
            with self.condition:
                self.items.append(value)
                self.condition.notify()

        def close(self) -> None:
            with self.condition:
                self.closed = True
                self.condition.notify()

        def __iter__(self):
            return self

        def __next__(self):
            with self.condition:
                while not self.items and not self.closed:
                    self.condition.wait(timeout=1)
                if self.items:
                    return self.items.pop(0)
                raise StopIteration

    class Stdin:
        def __init__(self, process):
            self.process = process

        def write(self, value):
            written.append(value)
            request = json.loads(value)
            self.process.stdout.push(
                json.dumps(
                    {
                        "cueforgeResponse": True,
                        "requestId": request["requestId"],
                        "ok": True,
                        "output": '{"ok":true}',
                    }
                )
                + "\n"
            )

        def flush(self):
            return None

        def close(self):
            return None

    class Process:
        def __init__(self, args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.stdout = Stream()
            self.stderr = Stream()
            self.stdin = Stdin(self)
            self.killed = False

        def poll(self):
            return None if not self.killed else -9

        def kill(self):
            self.killed = True
            self.stdout.close()
            self.stderr.close()
            return None

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        script_path = Path(args[-1])
        assert script_path.exists()
        script_text = script_path.read_text(encoding="utf-8")
        assert "npm:@huggingface/transformers" in script_text
        assert "Use VIDEO DESCRIPTION line by line" in script_text
        assert "do not treat every credit line as the final tag artist/title" in script_text
        assert "For cover or performance videos, tag the performed recording" in script_text
        assert "Cover artist priority" in script_text
        assert "Do not copy the entire VIDEO TITLE" in script_text
        assert "Your previous JSON was valid" in script_text
        assert "prefer the local-script display name" in script_text
        assert "description credit names a performer only with a romanized alias" in script_text
        assert "VIDEO CHANNEL or VIDEO UPLOADER shows that same performer" in script_text
        assert "Original/원곡/原曲 source-work artist" in script_text
        assert "negative evidence for tag artist" in script_text
        assert 'Use this exact schema: {\\"title\\":\\"...\\",\\"artist\\":\\"...\\"}' in script_text
        assert '\\"reason\\"' not in script_text
        assert "Do not use Markdown, prose, code fences, comments, arrays, or extra keys" in script_text
        assert "CURRENT GUESS" not in script_text
        assert "renderPromptInput(input.input)" in script_text
        assert "VIDEO DESCRIPTION:" in script_text
        assert "local_files_only" in script_text
        assert "cache_dir" in script_text
        return Process(args, **kwargs)

    monkeypatch.setattr("cueforge.metadata.gemma.subprocess.Popen", fake_popen)
    monkeypatch.setattr("cueforge.metadata.gemma._gemma_deno_path", lambda config: tmp_path / "deno.exe")
    _shutdown_gemma_sessions()

    script_path = None
    try:
        output = _run_gemma_session({"mode": "prepare"}, GemmaE2BConfig(cache_dir=tmp_path))

        args, kwargs = calls[0]
        script_path = Path(args[-1])
        assert output == '{"ok":true}'
        assert args[1] == "run"
        assert "--no-prompt" not in args
        assert "--allow-env" in args
        assert "--allow-ffi" in args
        assert "--allow-net" in args
        assert json.loads(written[0])["mode"] == "prepare"
    finally:
        _shutdown_gemma_sessions()
    assert script_path is not None
    assert not script_path.exists()


def test_gemma_session_runner_reuses_warm_process(monkeypatch, tmp_path: Path) -> None:
    created = []

    class FakeSession:
        def __init__(self, config, *, key) -> None:
            self.config = config
            self.key = key
            self.alive = True
            created.append(self)

        def is_alive(self) -> bool:
            return self.alive

        def run(self, payload, *, timeout_seconds):
            assert timeout_seconds == 90
            return '{"title":"Correct Song","artist":"Correct Artist"}'

        def stop(self) -> None:
            self.alive = False

    monkeypatch.setattr("cueforge.metadata.gemma._GemmaDenoSession", FakeSession)
    monkeypatch.setattr("cueforge.metadata.gemma._gemma_deno_path", lambda config: tmp_path / "deno.exe")
    _shutdown_gemma_sessions()

    config = GemmaE2BConfig(cache_dir=tmp_path)
    try:
        first = _run_gemma_session({"mode": "suggest"}, config)
        second = _run_gemma_session({"mode": "suggest"}, config)

        assert first == second
        assert len(created) == 1
    finally:
        _shutdown_gemma_sessions()


def _write_required_gemma_files(config: GemmaE2BConfig) -> None:
    model_dir = _gemma_model_dir(config)
    for file_name in GEMMA_E2B_REQUIRED_FILES:
        path = model_dir / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
