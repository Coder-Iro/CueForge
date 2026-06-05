from ytdj.metadata.resolver import MetadataResolver
from ytdj.models import MetadataCandidate, ReviewState, TrackMetadata
from ytdj.sources import SourcePlatform


class FailingYTMusicProvider:
    def lookup(self, url: str) -> TrackMetadata:
        raise AssertionError("YTMusic should not be called for SoundCloud")


class FakeMusicBrainzProvider:
    def lookup(self, reference: TrackMetadata, *, duration_ms: int | None = None) -> list[MetadataCandidate]:
        if reference.title == "明日の私に幸あれ" and reference.artist == "ナナヲアカリ":
            return [
                MetadataCandidate(
                    provider="musicbrainz",
                    score=0.871,
                    matched_fields=("title", "artist", "duration"),
                    metadata=TrackMetadata(
                        title="明日の私に幸あれ (Anime Size)",
                        artist="ナナヲアカリ",
                        album="明日の私に幸あれ",
                        genre="Anison",
                        release_date="2025-02-19",
                        isrc="JPU902500162",
                    ),
                )
            ]
        return [
            MetadataCandidate(
                provider="musicbrainz",
                score=0.97,
                matched_fields=("title", "artist"),
                metadata=TrackMetadata(title="Canonical Title", artist="Canonical Artist", album="Official Release"),
            )
        ]


class FakeYTMusicProvider:
    def lookup(self, url: str) -> TrackMetadata:
        return TrackMetadata(title="YT Title", artist="YT Artist")


def test_soundcloud_resolver_trusts_native_metadata_and_downgrades_external() -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FailingYTMusicProvider(),
        musicbrainz_provider_factory=FakeMusicBrainzProvider,
    )

    resolution = resolver.resolve(
        url="https://soundcloud.com/dj/anime-song-bootleg",
        info={
            "extractor_key": "Soundcloud",
            "title": "DJ Name - Anime Song (Bootleg Remix) [Free DL]",
            "uploader": "DJ Name",
            "genre": "J-Core",
            "webpage_url": "https://soundcloud.com/dj/anime-song-bootleg",
            "duration": 180,
        },
    )

    assert resolution.platform == SourcePlatform.SOUNDCLOUD
    assert resolution.state == ReviewState.AUTO_APPROVED
    assert resolution.metadata.title == "DJ Name - Anime Song (Bootleg Remix) [Free DL]"
    assert resolution.candidates[0].provider == "soundcloud"
    assert resolution.candidates[1].provider == "musicbrainz_reference"
    assert resolution.candidates[1].score == 0.84
    assert resolution.candidates[1].raw["reference_only"] is True


def test_youtube_resolver_still_uses_ytmusic_and_musicbrainz() -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FakeYTMusicProvider(),
        musicbrainz_provider_factory=FakeMusicBrainzProvider,
    )

    resolution = resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={
            "extractor_key": "Youtube",
            "title": "Fallback",
            "uploader": "Uploader",
            "webpage_url": "https://music.youtube.com/watch?v=abc",
        },
    )

    assert resolution.platform == SourcePlatform.YOUTUBE_MUSIC
    assert resolution.metadata.title == "Canonical Title"
    assert resolution.metadata.artist == "Canonical Artist"
    assert resolution.state == ReviewState.AUTO_APPROVED


def test_youtube_resolver_uses_description_theme_hints_before_fallback() -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FakeYTMusicProvider(),
        musicbrainz_provider_factory=FakeMusicBrainzProvider,
    )

    resolution = resolver.resolve(
        url="https://youtu.be/VEb3rctB3dc",
        info={
            "extractor_key": "Youtube",
            "title": "アニメの長いノンクレジットエンディング映像",
            "uploader": "アニプレックス チャンネル",
            "description": "▮オープニングテーマ\n310「パーフェクトデイ」\n▮エンディングテーマ\nナナヲアカリ「明日の私に幸あれ」",
            "duration": 90,
            "webpage_url": "https://www.youtube.com/watch?v=VEb3rctB3dc",
        },
    )

    assert resolution.metadata.title == "明日の私に幸あれ (Anime Size)"
    assert resolution.metadata.artist == "ナナヲアカリ"
    assert resolution.metadata.album == "明日の私に幸あれ"
    assert resolution.metadata.isrc == "JPU902500162"
    assert resolution.state == ReviewState.AUTO_APPROVED
    assert resolution.candidates[0].provider == "musicbrainz_from_description_エンディングテーマ"
