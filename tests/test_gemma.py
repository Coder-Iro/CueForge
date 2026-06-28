from pathlib import Path

import pytest

from cueforge.metadata.gemma import (
    GemmaE2BConfig,
    GemmaE2BMetadataSuggester,
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


def test_prepare_gemma_writes_ready_marker(tmp_path: Path) -> None:
    config = GemmaE2BConfig(cache_dir=tmp_path, allow_download=True)

    prepare_gemma_e2b(
        config,
        runner=lambda payload, resolved: '{"ok":true}',
    )

    assert gemma_e2b_cached(config) is True


def test_gemma_runner_uses_deno_run_permissions(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class Result:
        returncode = 0
        stdout = '{"ok":true}'
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        script_path = Path(args[-1])
        assert script_path.exists()
        assert "npm:@huggingface/transformers" in script_path.read_text(encoding="utf-8")
        return Result()

    monkeypatch.setattr("cueforge.metadata.gemma.subprocess.run", fake_run)

    output = _run_gemma_script({"mode": "prepare"}, GemmaE2BConfig(deno_path=tmp_path / "deno.exe"))

    args, kwargs = calls[0]
    assert output == '{"ok":true}'
    assert args[1] == "run"
    assert "--no-prompt" not in args
    assert "--allow-env" in args
    assert "--allow-ffi" in args
    assert "--allow-net" in args
    assert kwargs["input"] == '{"mode": "prepare"}'
    assert not Path(args[-1]).exists()
