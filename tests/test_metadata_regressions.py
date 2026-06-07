import json
from pathlib import Path
from typing import Any

import pytest

from cueforge.metadata.bpm import GetSongBpmConfig
from cueforge.metadata.resolver import MetadataResolver
from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.sources import SourcePlatform

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "metadata_regressions.json"


class FixtureYTMusicProvider:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def lookup(self, url: str) -> TrackMetadata:
        assert url == self.case["url"]
        return _metadata(self.case.get("ytmusic") or {})


class FixtureMusicBrainzProvider:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def lookup(self, reference: TrackMetadata, *, duration_ms: int | None = None) -> list[MetadataCandidate]:
        candidates: list[MetadataCandidate] = []
        for item in self.case.get("musicbrainz") or []:
            if item.get("when_title") and item["when_title"] != reference.title:
                continue
            if item.get("when_artist") and item["when_artist"] != reference.artist:
                continue
            candidates.append(_candidate(item))
        return candidates


class FixtureCoverArtProvider:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def lookup(self, release_id: str) -> str:
        return str((self.case.get("cover_art") or {}).get(release_id) or "")


class FixtureBpmProvider:
    def __init__(self, case: dict[str, Any]) -> None:
        self.case = case

    def lookup(
        self,
        reference: TrackMetadata,
        *,
        info: dict[str, Any],
        platform: SourcePlatform,
        duration_ms: int | None = None,
    ) -> list[MetadataCandidate]:
        return [_candidate(item) for item in self.case.get("bpm") or []]


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text(encoding="utf-8")), ids=lambda case: case["id"])
def test_metadata_regression_fixture(case: dict[str, Any]) -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FixtureYTMusicProvider(case),
        musicbrainz_provider_factory=lambda: FixtureMusicBrainzProvider(case),
        cover_art_provider_factory=lambda: FixtureCoverArtProvider(case),
        bpm_config=GetSongBpmConfig(client_key="fixture-key"),
        bpm_provider_factory=lambda config: FixtureBpmProvider(case),
    )

    resolution = resolver.resolve(url=case["url"], info=case["info"])
    expected = case["expected"]
    metadata = resolution.metadata

    assert metadata.title == expected["title"]
    assert metadata.artist == expected["artist"]
    assert metadata.album == expected["album"]
    assert metadata.release_date == expected["release_date"]
    assert metadata.isrc == expected["isrc"]
    assert metadata.bpm == expected["bpm"]
    assert metadata.cover_source == expected["cover_source"]
    assert resolution.state.value == expected["review_state"]
    assert _top_provider(resolution.candidates) == expected["top_candidate_provider"]


def _candidate(item: dict[str, Any]) -> MetadataCandidate:
    return MetadataCandidate(
        provider=str(item["provider"]),
        score=float(item["score"]),
        matched_fields=tuple(item.get("matched_fields") or ()),
        metadata=_metadata(item.get("metadata") or {}),
        raw=dict(item.get("raw") or {}),
    )


def _metadata(payload: dict[str, Any]) -> TrackMetadata:
    allowed = TrackMetadata.field_names()
    return TrackMetadata(**{key: value for key, value in payload.items() if key in allowed}).normalized()


def _top_provider(candidates: list[MetadataCandidate]) -> str:
    if not candidates:
        return ""
    return max(candidates, key=lambda candidate: candidate.score).provider
