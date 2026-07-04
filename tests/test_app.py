import sys

from cueforge import app as cueforge_app
from cueforge.app import _finish_cli, _is_cli_utility_mode, _print_cli_output, _smoke_metadata, _write_cli_output
from cueforge.download import DownloadConfig
from cueforge.metadata.resolver import MetadataResolution
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata
from cueforge.sources import SourcePlatform


class FakeDownloader:
    def __init__(self, config: DownloadConfig) -> None:
        self.config = config

    def fetch_info(self, url: str) -> dict:
        assert url == "https://youtu.be/abc"
        return {"id": "abc", "title": "Video"}


class FakeResolver:
    def resolve(self, *, url: str, info: dict, log) -> MetadataResolution:
        log("cover art fallback: platform thumbnail")
        return MetadataResolution(
            metadata=TrackMetadata(
                title="Song",
                artist="Artist",
                album="Album",
                cover_url="https://img.youtube.com/cover.jpg",
                cover_source="platform thumbnail",
            ),
            state=ReviewState.AUTO_APPROVED,
            candidates=[
                MetadataCandidate(
                    provider="ytmusic",
                    score=0.97,
                    matched_fields=("title", "artist"),
                    metadata=TrackMetadata(title="Song", artist="Artist"),
                )
            ],
            platform=SourcePlatform.YOUTUBE,
        )


def test_smoke_metadata_reports_resolved_metadata(monkeypatch) -> None:
    monkeypatch.setattr("cueforge.app.format_diagnostics", lambda: "diagnostics")

    payload = _smoke_metadata(
        "https://youtu.be/abc",
        downloader_factory=FakeDownloader,
        resolver_factory=FakeResolver,
    )

    assert payload["state"] == "auto_approved"
    assert payload["platform"] == "youtube"
    assert payload["metadata"]["title"] == "Song"
    assert payload["metadata"]["cover_source"] == "platform thumbnail"
    assert payload["candidates"][0]["provider"] == "ytmusic"
    assert payload["logs"] == ["cover art fallback: platform thumbnail"]
    assert payload["diagnostics"] == "diagnostics"


def test_main_writes_metadata_smoke_failures(tmp_path, monkeypatch) -> None:
    output = tmp_path / "smoke.json"

    def failing_smoke(url: str) -> dict:
        raise RuntimeError("metadata failed")

    monkeypatch.setattr(cueforge_app, "_smoke_metadata", failing_smoke)
    monkeypatch.setattr(cueforge_app, "format_diagnostics", lambda: "diagnostics")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cueforge", "--smoke-metadata-url", "https://youtu.be/abc", "--diagnose-file", str(output)],
    )

    assert cueforge_app.main() == 2
    text = output.read_text(encoding="utf-8")
    assert "metadata failed" in text
    assert "diagnostics" in text


def test_cli_utility_modes_force_entrypoint_exit() -> None:
    assert _is_cli_utility_mode(["cueforge", "--diagnose"])
    assert _is_cli_utility_mode(["cueforge", "--diagnose-file", "diagnostics.txt"])
    assert _is_cli_utility_mode(["cueforge", "--smoke-gui"])
    assert _is_cli_utility_mode(["cueforge", "--smoke-metadata-url", "https://youtu.be/abc"])
    assert not _is_cli_utility_mode(["cueforge"])


def test_frozen_cli_output_writes_file_without_printing(tmp_path, monkeypatch, capsys) -> None:
    output = tmp_path / "diagnostics.txt"

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _write_cli_output("diagnostics", output)

    assert output.read_text(encoding="utf-8") == "diagnostics\n"
    assert capsys.readouterr().out == ""


def test_finish_cli_returns_code_when_not_frozen(monkeypatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert _finish_cli(7) == 7


def test_cli_output_falls_back_to_utf8_buffer(monkeypatch) -> None:
    class Buffer:
        def __init__(self) -> None:
            self.payload = b""

        def write(self, payload: bytes) -> None:
            self.payload += payload

        def flush(self) -> None:
            pass

    class Stdout:
        encoding = "cp949"

        def __init__(self) -> None:
            self.buffer = Buffer()

        def write(self, value: str) -> None:
            raise UnicodeEncodeError("cp949", value, 0, 1, "blocked")

        def flush(self) -> None:
            pass

    stdout = Stdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    _print_cli_output("様")

    assert stdout.buffer.payload == "様\n".encode("utf-8")
