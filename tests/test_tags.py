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
    assert "cover" in result.written_fields
    assert not result.warnings

