from pathlib import Path

from mutagen.id3 import ID3

from ytdj.models import TrackMetadata
from ytdj.tags import RekordboxTagWriter, safe_track_filename


def test_safe_track_filename_removes_windows_invalid_chars() -> None:
    filename = safe_track_filename(TrackMetadata(artist='A/B', title='T: "Mix"'))

    assert filename == "A_B - T_ _Mix_.mp3"


def test_writer_saves_id3v23_fields(tmp_path: Path) -> None:
    target = tmp_path / "track.mp3"
    target.write_bytes(b"")
    writer = RekordboxTagWriter(cover_fetcher=lambda url: (b"image-bytes", "image/jpeg"))

    result = writer.write(
        target,
        TrackMetadata(
            title="Song",
            artist="Artist",
            album="Album",
            album_artist="Album Artist",
            genre="House",
            release_date="2026-05-01",
            track_number=7,
            disc_number=1,
            bpm=128,
            bpm_source="GetSongBPM",
            bpm_confidence=0.91,
            label="Label",
            isrc="USABC260001",
            cover_url="https://example.com/cover.jpg",
            source_url="https://music.youtube.com/watch?v=abc",
            musicbrainz_recording_id="rec-1",
            musicbrainz_release_id="rel-1",
        ),
    )

    tags = ID3(target)
    assert tags.version == (2, 3, 0)
    assert tags["TIT2"].text[0] == "Song"
    assert tags["TPE1"].text[0] == "Artist"
    assert tags["TALB"].text[0] == "Album"
    assert tags["TCON"].text[0] == "House"
    assert tags["TBPM"].text[0] == "128"
    assert tags["TRCK"].text[0] == "7"
    assert tags["TSRC"].text[0] == "USABC260001"
    assert tags["TXXX:BPM Source"].text[0] == "GetSongBPM"
    assert tags["TXXX:BPM Confidence"].text[0] == "0.910"
    pictures = tags.getall("APIC")
    assert len(pictures) == 1
    assert pictures[0].mime == "image/jpeg"
    assert pictures[0].type == 3
    assert pictures[0].desc == "Cover"
    assert pictures[0].data == b"image-bytes"
    assert "cover" in result.written_fields
    assert not result.warnings


def test_writer_omits_bpm_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "track.mp3"
    target.write_bytes(b"")

    RekordboxTagWriter().write(target, TrackMetadata(title="Song", artist="Artist", bpm_source="GetSongBPM", bpm_confidence=0.9))

    tags = ID3(target)
    assert "TBPM" not in tags
    assert "TXXX:BPM Source" not in tags
    assert "TXXX:BPM Confidence" not in tags


def test_writer_skips_non_image_cover_response(tmp_path: Path) -> None:
    target = tmp_path / "track.mp3"
    target.write_bytes(b"")
    writer = RekordboxTagWriter(cover_fetcher=lambda url: (b"<html></html>", "text/html"))

    result = writer.write(
        target,
        TrackMetadata(
            title="Song",
            artist="Artist",
            cover_url="https://example.com/cover.jpg",
        ),
    )

    tags = ID3(target)
    assert not tags.getall("APIC")
    assert "cover" in result.skipped_fields
    assert result.warnings == ("cover fetch returned non-image content type: text/html",)
