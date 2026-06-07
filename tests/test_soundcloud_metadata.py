from ytdj.metadata.soundcloud import build_soundcloud_metadata, build_soundcloud_native_candidate


def test_soundcloud_metadata_preserves_remix_title_and_source() -> None:
    info = {
        "title": "DJ Name - Anime Song (Bootleg Remix) [Free DL]",
        "uploader": "DJ Name",
        "creator": "DJ Name",
        "genre": "J-Core",
        "description": "Download link and set notes",
        "thumbnail": "https://i1.sndcdn.com/artworks-test.jpg",
        "webpage_url": "https://soundcloud.com/dj/anime-song-bootleg",
        "upload_date": "20260203",
        "bpm": "174",
    }

    metadata = build_soundcloud_metadata(info)

    assert metadata.title == "DJ Name - Anime Song (Bootleg Remix) [Free DL]"
    assert metadata.artist == "DJ Name"
    assert metadata.album_artist == "DJ Name"
    assert metadata.genre == "J-Core"
    assert metadata.release_date == "2026-02-03"
    assert metadata.bpm == 174
    assert metadata.bpm_source == "native:soundcloud"
    assert metadata.source_url == "https://soundcloud.com/dj/anime-song-bootleg"
    assert "Download link" in metadata.comments


def test_soundcloud_candidate_is_trusted_native_metadata() -> None:
    candidate = build_soundcloud_native_candidate(
        {
            "title": "Producer - Track (Mashup Edit)",
            "uploader": "Producer",
            "tags": ["Future Funk", "Anime"],
            "tempo": 64,
            "webpage_url": "https://soundcloud.com/producer/track",
        }
    )

    assert candidate.provider == "soundcloud"
    assert candidate.score == 0.99
    assert "remix_title_preserved" in candidate.matched_fields
    assert "bpm" in candidate.matched_fields
    assert candidate.raw["trusted_native"] is True
    assert candidate.metadata.genre == "Future Funk"
    assert candidate.metadata.bpm == 64
