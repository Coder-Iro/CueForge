import json
from pathlib import Path
from typing import Any

import pytest

from cueforge.metadata.resolver import MetadataResolver
from cueforge.models import TrackMetadata

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "metadata_regressions.json"


class FixtureYTMusicProvider:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def lookup(self, url: str) -> TrackMetadata:
        assert url == self.case["url"]
        return _metadata(self.case.get("ytmusic") or {})


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text(encoding="utf-8")), ids=lambda case: case["id"])
def test_metadata_regression_fixture(case: dict[str, Any]) -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FixtureYTMusicProvider(case))

    resolution = resolver.resolve(url=case["url"], info=case["info"])
    expected = case["expected"]
    metadata = resolution.metadata

    assert metadata.title == expected["title"]
    assert metadata.artist == expected["artist"]
    assert metadata.album == expected["album"]
    assert metadata.release_date == expected["release_date"]
    assert metadata.isrc == expected["isrc"]
    assert metadata.cover_source == expected["cover_source"]
    assert resolution.state.value == expected["review_state"]
    assert _top_provider(resolution.candidates) == expected["top_candidate_provider"]


def _metadata(payload: dict[str, Any]) -> TrackMetadata:
    allowed = TrackMetadata.field_names()
    return TrackMetadata(**{key: value for key, value in payload.items() if key in allowed}).normalized()


def _top_provider(candidates) -> str:
    if not candidates:
        return ""
    return max(candidates, key=lambda candidate: candidate.score).provider
