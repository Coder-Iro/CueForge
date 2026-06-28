from pathlib import Path

from mutagen.id3 import ID3

from cueforge.models import TrackMetadata
from cueforge.tags import MAX_COVER_BYTES, RekordboxTagWriter, safe_track_filename


def test_safe_track_filename_removes_windows_invalid_chars() -> None:
    filename = safe_track_filename(TrackMetadata(artist='A/B', title='T: "Mix"'))

    assert filename == "A_B - T_ _Mix_.mp3"


def test_safe_track_filename_appends_source_id() -> None:
    filename = safe_track_filename(TrackMetadata(artist="Artist", title="Song"), source_id="ab:c/12")

    assert filename == "Artist - Song [ab_c_12].mp3"


def test_safe_track_filename_limits_length() -> None:
    long_name = safe_track_filename(TrackMetadata(artist="A" * 300, title="Title"), source_id="abc123")

    assert len(long_name) <= 184
    assert long_name.endswith(" [abc123].mp3")


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
    assert tags["TRCK"].text[0] == "7"
    assert tags["TSRC"].text[0] == "USABC260001"
    pictures = tags.getall("APIC")
    assert len(pictures) == 1
    assert pictures[0].mime == "image/jpeg"
    assert pictures[0].type == 3
    assert pictures[0].desc == "Cover"
    assert pictures[0].data == b"image-bytes"
    assert "cover" in result.written_fields
    assert not result.warnings

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


def test_writer_skips_oversized_cover_response(tmp_path: Path) -> None:
    target = tmp_path / "track.mp3"
    target.write_bytes(b"")
    writer = RekordboxTagWriter(cover_fetcher=lambda url: (b"x" * (MAX_COVER_BYTES + 1), "image/jpeg"))

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
    assert result.warnings == ("cover fetch returned image larger than 8 MiB",)
