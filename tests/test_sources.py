from cueforge.sources import SourcePlatform, detect_source_platform, normalize_source_url, trust_policy_for


def test_detect_source_platform_from_url() -> None:
    assert detect_source_platform("https://youtu.be/abc") == SourcePlatform.YOUTUBE
    assert detect_source_platform("youtu.be/abc") == SourcePlatform.YOUTUBE
    assert detect_source_platform("https://music.youtube.com/watch?v=abc") == SourcePlatform.YOUTUBE_MUSIC
    assert detect_source_platform("https://soundcloud.com/artist/track") == SourcePlatform.SOUNDCLOUD


def test_normalize_source_url_adds_https_for_supported_hosts() -> None:
    assert normalize_source_url("youtu.be/abc") == "https://youtu.be/abc"
    assert normalize_source_url("www.youtube.com/watch?v=abc.") == "https://www.youtube.com/watch?v=abc"
    assert normalize_source_url("music.youtube.com/watch?v=abc") == "https://music.youtube.com/watch?v=abc"
    assert normalize_source_url("soundcloud.com/artist/track") == "https://soundcloud.com/artist/track"
    assert normalize_source_url("example.com/page") == "example.com/page"


def test_detect_source_platform_from_extractor_key() -> None:
    assert detect_source_platform("https://example.com/x", {"extractor_key": "Soundcloud"}) == SourcePlatform.SOUNDCLOUD
    assert detect_source_platform("https://example.com/x", {"extractor_key": "Youtube"}) == SourcePlatform.YOUTUBE


def test_soundcloud_trust_policy_prefers_native_metadata() -> None:
    policy = trust_policy_for(SourcePlatform.SOUNDCLOUD)

    assert policy.trust_native_metadata
    assert not policy.use_youtube_music
    assert not policy.allow_external_auto_approve
