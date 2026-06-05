import sys

from ytdj import app as ytdj_app
from ytdj.app import _smoke_metadata
from ytdj.download import DownloadConfig
from ytdj.metadata.resolver import MetadataResolution
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata
from ytdj.sources import SourcePlatform


class FakeDownloader:
    def __init__(self, config: DownloadConfig) -> None:
        self.config = config

    def fetch_info(self, url: str) -> dict:
        assert url == "https://youtu.be/abc"
        return {"id": "abc", "title": "Video"}


class FakeResolver:
    def resolve(self, *, url: str, info: dict, log) -> MetadataResolution:
        log("cover art: Cover Art Archive 500px")
        return MetadataResolution(
            metadata=TrackMetadata(
                title="Song",
                artist="Artist",
                album="Album",
                cover_url="https://coverartarchive.org/release/rel/front-500.jpg",
                cover_source="Cover Art Archive",
            ),
            state=ReviewState.AUTO_APPROVED,
            candidates=[
                MetadataCandidate(
                    provider="musicbrainz",
                    score=0.97,
                    matched_fields=("title", "artist"),
                    metadata=TrackMetadata(title="Song", artist="Artist", musicbrainz_release_id="rel-1"),
                )
            ],
            platform=SourcePlatform.YOUTUBE,
        )


def test_smoke_metadata_reports_resolved_metadata(monkeypatch) -> None:
    monkeypatch.setattr("ytdj.app.format_diagnostics", lambda: "diagnostics")

    payload = _smoke_metadata(
        "https://youtu.be/abc",
        downloader_factory=FakeDownloader,
        resolver_factory=FakeResolver,
    )

    assert payload["state"] == "auto_approved"
    assert payload["platform"] == "youtube"
    assert payload["metadata"]["title"] == "Song"
    assert payload["metadata"]["cover_source"] == "Cover Art Archive"
    assert payload["candidates"][0]["provider"] == "musicbrainz"
    assert payload["logs"] == ["cover art: Cover Art Archive 500px"]
    assert payload["diagnostics"] == "diagnostics"


def test_main_writes_metadata_smoke_failures(tmp_path, monkeypatch) -> None:
    output = tmp_path / "smoke.json"

    def failing_smoke(url: str) -> dict:
        raise RuntimeError("metadata failed")

    monkeypatch.setattr(ytdj_app, "_smoke_metadata", failing_smoke)
    monkeypatch.setattr(ytdj_app, "format_diagnostics", lambda: "diagnostics")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ytdj", "--smoke-metadata-url", "https://youtu.be/abc", "--diagnose-file", str(output)],
    )

    assert ytdj_app.main() == 2
    text = output.read_text(encoding="utf-8")
    assert "metadata failed" in text
    assert "diagnostics" in text
