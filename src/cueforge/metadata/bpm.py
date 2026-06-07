"""BPM metadata helpers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from cueforge.models import MetadataCandidate, TrackMetadata
from cueforge.sources import SourcePlatform


@dataclass(frozen=True, slots=True)
class BpmProviderConfig:
    user_agent: str = "CueForge/0.1.0"


class BpmProvider:
    def __init__(self, config: BpmProviderConfig | None = None) -> None:
        self.config = config or BpmProviderConfig()

    def lookup(
        self,
        reference: TrackMetadata,
        *,
        info: dict[str, Any],
        platform: SourcePlatform,
        duration_ms: int | None = None,
    ) -> list[MetadataCandidate]:
        del reference, duration_ms
        native = native_bpm_candidate(info, platform)
        return [native] if native else []


def native_bpm_candidate(info: dict[str, Any], platform: SourcePlatform) -> MetadataCandidate | None:
    for key, value in _native_bpm_fields(info):
        bpm = parse_bpm(value)
        if bpm:
            source = f"native:{_platform_value(platform)}"
            return MetadataCandidate(
                provider="bpm_native",
                metadata=TrackMetadata(bpm=bpm, bpm_source=source, bpm_confidence=1.0).normalized(),
                score=1.0,
                matched_fields=("bpm", "native_metadata"),
                raw={"field": key, "value": value, "source": source},
            )
    return None


def parse_bpm(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if not match:
            return None
        value = match.group(0)
    try:
        bpm = math.floor(float(value) + 0.5)
    except (TypeError, ValueError):
        return None
    return bpm if bpm > 0 else None


def _native_bpm_fields(info: dict[str, Any]) -> list[tuple[str, Any]]:
    fields = [("bpm", info.get("bpm")), ("tempo", info.get("tempo"))]
    track = info.get("track")
    if isinstance(track, dict):
        fields.extend([("track.bpm", track.get("bpm")), ("track.tempo", track.get("tempo"))])
    return fields


def _platform_value(platform: SourcePlatform | str) -> str:
    if isinstance(platform, SourcePlatform):
        if platform == SourcePlatform.UNKNOWN:
            return "source"
        return platform.value
    return str(platform) or "source"
