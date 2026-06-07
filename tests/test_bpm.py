from cueforge.metadata.bpm import BpmProvider, BpmProviderConfig, native_bpm_candidate, parse_bpm
from cueforge.models import TrackMetadata
from cueforge.sources import SourcePlatform


def test_parse_bpm_passthrough_and_decimal_rounding() -> None:
    assert parse_bpm(64) == 64
    assert parse_bpm(220) == 220
    assert parse_bpm(174) == 174
    assert parse_bpm(128.6) == 129


def test_parse_bpm_extracts_first_numeric_value_from_text() -> None:
    assert parse_bpm("BPM: 172.4") == 172
    assert parse_bpm("no tempo") is None


def test_native_bpm_candidate_uses_platform_source() -> None:
    candidate = native_bpm_candidate({"bpm": 128}, SourcePlatform.SOUNDCLOUD)

    assert candidate is not None
    assert candidate.provider == "bpm_native"
    assert candidate.metadata.bpm == 128
    assert candidate.metadata.bpm_source == "native:soundcloud"
    assert candidate.metadata.bpm_confidence == 1.0


def test_native_bpm_candidate_reads_nested_track_bpm() -> None:
    candidate = native_bpm_candidate({"track": {"bpm": 174}}, SourcePlatform.SOUNDCLOUD)

    assert candidate is not None
    assert candidate.metadata.bpm == 174
    assert candidate.raw["field"] == "track.bpm"


def test_bpm_provider_returns_native_candidate_without_external_lookup() -> None:
    provider = BpmProvider(BpmProviderConfig())

    candidates = provider.lookup(
        TrackMetadata(title="Song", artist="Artist"),
        info={"track": {"tempo": 220}},
        platform=SourcePlatform.SOUNDCLOUD,
    )

    assert len(candidates) == 1
    assert candidates[0].provider == "bpm_native"
    assert candidates[0].metadata.bpm == 220


def test_bpm_provider_returns_empty_when_source_has_no_native_bpm() -> None:
    provider = BpmProvider(BpmProviderConfig())

    candidates = provider.lookup(
        TrackMetadata(title="Song", artist="Artist"),
        info={"title": "Song"},
        platform=SourcePlatform.YOUTUBE,
    )

    assert candidates == []
