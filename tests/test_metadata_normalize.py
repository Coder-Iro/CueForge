from ytdj.metadata import build_safe_fallback, merge_metadata, parse_artist_title
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata


def test_parse_artist_title_removes_video_noise() -> None:
    artist, title = parse_artist_title("Artist Name - Track Name (Official Music Video) HD")

    assert artist == "Artist Name"
    assert title == "Track Name"


def test_safe_fallback_prefers_track_fields() -> None:
    metadata = build_safe_fallback(
        {
            "title": "Uploader Title",
            "track": "Song Title",
            "artist": "Song Artist",
            "album": "Album",
            "upload_date": "20260501",
            "webpage_url": "https://music.youtube.com/watch?v=abc",
        }
    )

    assert metadata.title == "Song Title"
    assert metadata.artist == "Song Artist"
    assert metadata.album == "Album"
    assert metadata.release_date == "2026-05-01"


def test_merge_metadata_user_values_win() -> None:
    fallback = TrackMetadata(title="Video Title", artist="Uploader")
    youtube = TrackMetadata(title="YT Title", artist="YT Artist", album="YT Album")
    musicbrainz = MetadataCandidate(
        provider="musicbrainz",
        score=0.9,
        matched_fields=("title", "artist"),
        metadata=TrackMetadata(title="MB Title", artist="MB Artist", genre="House"),
    )
    user = TrackMetadata(title="Manual Title")

    resolved, state = merge_metadata(
        user=user,
        youtube=youtube,
        candidates=[musicbrainz],
        fallback=fallback,
    )

    assert resolved.title == "Manual Title"
    assert resolved.artist == "MB Artist"
    assert resolved.album == "YT Album"
    assert resolved.genre == "House"
    assert state == ReviewState.AUTO_APPROVED


def test_low_confidence_candidate_requires_review() -> None:
    resolved, state = merge_metadata(
        candidates=[
            MetadataCandidate(
                provider="musicbrainz",
                score=0.7,
                matched_fields=("title",),
                metadata=TrackMetadata(title="A", artist="B"),
            )
        ]
    )

    assert resolved.is_minimum_viable()
    assert state == ReviewState.REVIEW_REQUIRED

