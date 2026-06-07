"""Shared domain models for downloads, metadata, and tagging."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class DownloadStatus(str, Enum):
    PENDING = "pending"
    METADATA = "metadata"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


class ReviewState(str, Enum):
    AUTO_APPROVED = "auto_approved"
    REVIEW_REQUIRED = "review_required"
    MANUAL_REQUIRED = "manual_required"


@dataclass(slots=True)
class TrackMetadata:
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    release_date: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    bpm: int | None = None
    bpm_source: str = ""
    bpm_confidence: float | None = None
    label: str = ""
    isrc: str = ""
    cover_url: str = ""
    cover_source: str = ""
    source_url: str = ""
    musicbrainz_recording_id: str = ""
    musicbrainz_release_id: str = ""
    comments: str = ""

    def with_defaults_from(self, fallback: "TrackMetadata") -> "TrackMetadata":
        values = {
            field_name: getattr(self, field_name) or getattr(fallback, field_name)
            for field_name in self.field_names()
        }
        return TrackMetadata(**values)

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(cls.__dataclass_fields__.keys())

    def overlay(self, override: "TrackMetadata") -> "TrackMetadata":
        values = {
            field_name: getattr(override, field_name) or getattr(self, field_name)
            for field_name in self.field_names()
        }
        return TrackMetadata(**values)

    def normalized(self) -> "TrackMetadata":
        from cueforge.metadata.normalize import clean_metadata

        return clean_metadata(self)

    def is_minimum_viable(self) -> bool:
        return bool(self.title.strip() and self.artist.strip())


@dataclass(slots=True)
class MetadataCandidate:
    provider: str
    metadata: TrackMetadata
    score: float
    matched_fields: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def review_state(self) -> ReviewState:
        if self.score >= 0.85:
            return ReviewState.AUTO_APPROVED
        if self.score >= 0.65:
            return ReviewState.REVIEW_REQUIRED
        return ReviewState.MANUAL_REQUIRED


@dataclass(slots=True)
class DownloadJob:
    url: str
    output_dir: Path
    id: str = field(default_factory=lambda: uuid4().hex)
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    selected_metadata: TrackMetadata = field(default_factory=TrackMetadata)
    candidates: list[MetadataCandidate] = field(default_factory=list)
    downloaded_path: Path | None = None
    final_path: Path | None = None
    error: str = ""

    def transition(
        self,
        status: DownloadStatus,
        *,
        progress: float | None = None,
        error: str = "",
    ) -> "DownloadJob":
        return replace(
            self,
            status=status,
            progress=self.progress if progress is None else progress,
            error=error,
        )


@dataclass(slots=True)
class TagWriteResult:
    path: Path
    written_fields: tuple[str, ...]
    skipped_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
