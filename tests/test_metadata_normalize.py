from cueforge.metadata import build_safe_fallback, merge_metadata, parse_artist_title
from cueforge.metadata.normalize import sanitize_cover_url
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata


def test_parse_artist_title_removes_video_noise() -> None:
    artist, title = parse_artist_title("Artist Name - Track Name (Official Music Video) HD")

    assert artist == "Artist Name"
    assert title == "Track Name"


def test_parse_artist_title_removes_leading_mv_badge() -> None:
    artist, title = parse_artist_title("【MV】Artist Name - Track Name")

    assert artist == "Artist Name"
    assert title == "Track Name"


def test_parse_artist_title_accepts_compact_dash_for_user_uploads() -> None:
    artist, title = parse_artist_title("린-개미관찰")

    assert artist == "린"
    assert title == "개미관찰"


def test_parse_artist_title_keeps_generic_vocaloid_prefix_out_of_artist() -> None:
    artist, title = parse_artist_title("보컬로이드 -개미관찰-")

    assert artist == ""
    assert title == "개미관찰"


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


def test_safe_fallback_preserves_source_title_before_parsed_hint() -> None:
    metadata = build_safe_fallback(
        {
            "title": "린-개미관찰",
            "uploader": "윤다희",
            "upload_date": "20140427",
        }
    )

    assert metadata.title == "린-개미관찰"
    assert metadata.artist == "윤다희"
    assert metadata.release_date == "2014-04-27"


def test_safe_fallback_uses_uploader_when_title_prefix_is_generic() -> None:
    metadata = build_safe_fallback(
        {
            "title": "보컬로이드 -개미관찰-",
            "uploader": "토우링고",
            "upload_date": "20140729",
        }
    )

    assert metadata.title == "보컬로이드 -개미관찰"
    assert metadata.artist == "토우링고"
    assert metadata.release_date == "2014-07-29"


def test_safe_fallback_prefers_non_official_creator_from_mixed_creator_credit() -> None:
    metadata = build_safe_fallback(
        {
            "title": "【Official MV】Ex-Otogibanashi (Anime ver.) - ryo (supercell)",
            "creator": "『超かぐや姫 ! 』公式, ryo (supercell)",
            "uploader": "『超かぐや姫 ! 』公式",
        }
    )

    assert metadata.artist == "ryo (supercell)"
    assert metadata.album_artist == "ryo (supercell)"


def test_safe_fallback_does_not_treat_official_as_artist_name_noise() -> None:
    metadata = build_safe_fallback(
        {
            "title": "Official髭男dism - Pretender",
            "creator": "Official髭男dism",
            "uploader": "Official髭男dism",
        }
    )

    assert metadata.artist == "Official髭男dism"


def test_sanitize_cover_url_trims_copy_paste_punctuation_without_rejecting_signed_urls() -> None:
    signed_url = "https://example.com/cover.jpg?X-Amz-Signature=deadbeef"

    assert sanitize_cover_url(signed_url) == signed_url
    assert sanitize_cover_url("https://example.com/cover.jpg:") == "https://example.com/cover.jpg"


def test_clean_metadata_keeps_cached_cover_path_as_tagging_source() -> None:
    metadata = TrackMetadata(cover_path=" C:/cache/cover.jpg ", cover_source="cached")

    assert metadata.normalized().cover_path == "C:/cache/cover.jpg"
    assert metadata.normalized().cover_source == "cached"


def test_clean_metadata_preserves_artist_alias_text_from_provider_output() -> None:
    assert TrackMetadata(artist="텐코 시부키 TENKO SHIBUKI").normalized().artist == "텐코 시부키 TENKO SHIBUKI"
    assert TrackMetadata(artist="Charming Jo (조매력)").normalized().artist == "Charming Jo (조매력)"
    assert TrackMetadata(artist="조매력 (Charming Jo)").normalized().artist == "조매력 (Charming Jo)"
    assert TrackMetadata(artist="ryo (supercell)").normalized().artist == "ryo (supercell)"


def test_merge_metadata_user_values_win() -> None:
    fallback = TrackMetadata(title="Video Title", artist="Uploader")
    youtube = TrackMetadata(title="YT Title", artist="YT Artist", album="YT Album")
    external = MetadataCandidate(
        provider="external",
        score=0.9,
        matched_fields=("title", "artist"),
        metadata=TrackMetadata(title="MB Title", artist="MB Artist", genre="House"),
    )
    user = TrackMetadata(title="Manual Title")

    resolved, state = merge_metadata(
        user=user,
        youtube=youtube,
        candidates=[external],
        fallback=fallback,
    )

    assert resolved.title == "Manual Title"
    assert resolved.artist == "MB Artist"
    assert resolved.album == "YT Album"
    assert resolved.genre == "House"
    assert state == ReviewState.AUTO_APPROVED


def test_merge_metadata_prefers_chatgpt_when_candidate_scores_tie() -> None:
    hint = MetadataCandidate(
        provider="title_cover",
        score=0.78,
        matched_fields=("title", "artist"),
        metadata=TrackMetadata(title="Hint Title", artist="Hint Artist"),
    )
    chatgpt = MetadataCandidate(
        provider="chatgpt",
        score=0.78,
        matched_fields=("title", "artist"),
        metadata=TrackMetadata(title="ChatGPT Title", artist="ChatGPT Artist"),
    )

    resolved, state = merge_metadata(candidates=[hint, chatgpt])

    assert resolved.title == "ChatGPT Title"
    assert resolved.artist == "ChatGPT Artist"
    assert state == ReviewState.REVIEW_REQUIRED


def test_low_confidence_candidate_requires_review() -> None:
    resolved, state = merge_metadata(
        candidates=[
            MetadataCandidate(
                provider="external",
                score=0.7,
                matched_fields=("title",),
                metadata=TrackMetadata(title="A", artist="B"),
            )
        ]
    )

    assert resolved.is_minimum_viable()
    assert state == ReviewState.REVIEW_REQUIRED
