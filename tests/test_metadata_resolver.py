from pathlib import Path

from cueforge.metadata.resolver import MetadataResolver
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata
from cueforge.sources import SourcePlatform


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


class FakeYTMusicWithCoverProvider:
    def lookup(self, url: str) -> TrackMetadata:
        return TrackMetadata(title="YT Title", artist="YT Artist", cover_url="https://img.youtube.com/yt-thumb.jpg")


class FakeReleaseMusicBrainzProvider:
    def lookup(self, reference: TrackMetadata, *, duration_ms: int | None = None) -> list[MetadataCandidate]:
        return [
            MetadataCandidate(
                provider="musicbrainz",
                score=0.97,
                matched_fields=("title", "artist"),
                metadata=TrackMetadata(
                    title="Canonical Title",
                    artist="Canonical Artist",
                    album="Official Release",
                    musicbrainz_release_id="rel-1",
                ),
            )
        ]


class EmptyMusicBrainzProvider:
    def lookup(self, reference: TrackMetadata, *, duration_ms: int | None = None) -> list[MetadataCandidate]:
        return []


class FakeGemmaSuggester:
    def suggest(
        self,
        *,
        info: dict,
        reference: TrackMetadata,
        candidates: list[MetadataCandidate],
        log=None,
    ) -> list[MetadataCandidate]:
        return [
            MetadataCandidate(
                provider="gemma_e2b",
                score=0.0,
                matched_fields=("gemma_e2b", "title", "artist"),
                metadata=TrackMetadata(title="Gemma Song", artist="Gemma Artist"),
                raw={"review_only": True, "requires_semantic_score": True},
            )
        ]


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


def test_youtube_resolver_passes_cookie_file_auth_option(tmp_path: Path) -> None:
    calls: list[dict] = []
    cookie_file = tmp_path / "cookies.txt"

    def factory(**kwargs: object) -> FakeYTMusicProvider:
        calls.append(kwargs)
        return FakeYTMusicProvider()

    resolver = MetadataResolver(
        ytmusic_provider_factory=factory,
        musicbrainz_provider_factory=EmptyMusicBrainzProvider,
    )

    resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={"extractor_key": "Youtube", "title": "Fallback"},
        ytmusic_cookie_file=cookie_file,
    )

    assert calls[0]["cookie_file"] == cookie_file


def test_youtube_resolver_passes_oauth_auth_options(tmp_path: Path) -> None:
    calls: list[dict] = []
    oauth_client_file = tmp_path / "google_oauth_client.json"
    oauth_token_file = tmp_path / "ytmusic_oauth_token.json"

    def factory(**kwargs: object) -> FakeYTMusicProvider:
        calls.append(kwargs)
        return FakeYTMusicProvider()

    resolver = MetadataResolver(
        ytmusic_provider_factory=factory,
        musicbrainz_provider_factory=EmptyMusicBrainzProvider,
    )

    resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={"extractor_key": "Youtube", "title": "Fallback"},
        ytmusic_oauth_client_file=oauth_client_file,
        ytmusic_oauth_token_file=oauth_token_file,
    )

    assert calls[0]["oauth_client_file"] == oauth_client_file
    assert calls[0]["oauth_token_file"] == oauth_token_file


def test_youtube_resolver_prefers_cover_art_archive_over_platform_thumbnail() -> None:
    calls: list[str] = []

    class FakeCoverArtProvider:
        def lookup(self, release_id: str) -> str:
            calls.append(release_id)
            return "https://coverartarchive.org/release/rel-1/front-500.jpg"

    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FakeYTMusicWithCoverProvider(),
        musicbrainz_provider_factory=FakeReleaseMusicBrainzProvider,
        cover_art_provider_factory=FakeCoverArtProvider,
    )

    resolution = resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={
            "extractor_key": "Youtube",
            "title": "Fallback",
            "uploader": "Uploader",
            "thumbnail": "https://img.youtube.com/fallback.jpg",
        },
    )

    assert resolution.metadata.cover_url == "https://coverartarchive.org/release/rel-1/front-500.jpg"
    assert calls == ["rel-1"]


def test_youtube_resolver_falls_back_to_platform_thumbnail_when_cover_art_missing() -> None:
    class MissingCoverArtProvider:
        def lookup(self, release_id: str) -> str:
            return ""

    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FakeYTMusicWithCoverProvider(),
        musicbrainz_provider_factory=FakeReleaseMusicBrainzProvider,
        cover_art_provider_factory=MissingCoverArtProvider,
    )

    resolution = resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={
            "extractor_key": "Youtube",
            "title": "Fallback",
            "uploader": "Uploader",
            "thumbnail": "https://img.youtube.com/fallback.jpg",
        },
    )

    assert resolution.metadata.cover_url == "https://img.youtube.com/yt-thumb.jpg"


def test_soundcloud_resolver_keeps_native_artwork_even_with_release_match() -> None:
    class FailingCoverArtProvider:
        def lookup(self, release_id: str) -> str:
            raise AssertionError("SoundCloud native artwork should be kept")

    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FailingYTMusicProvider(),
        musicbrainz_provider_factory=FakeReleaseMusicBrainzProvider,
        cover_art_provider_factory=FailingCoverArtProvider,
    )

    resolution = resolver.resolve(
        url="https://soundcloud.com/dj/anime-song-bootleg",
        info={
            "extractor_key": "Soundcloud",
            "title": "DJ Name - Anime Song (Bootleg Remix) [Free DL]",
            "uploader": "DJ Name",
            "thumbnail": "https://i1.sndcdn.com/artworks-native.jpg",
            "webpage_url": "https://soundcloud.com/dj/anime-song-bootleg",
        },
    )

    assert resolution.metadata.cover_url == "https://i1.sndcdn.com/artworks-native.jpg"


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


def test_youtube_auto_generated_description_does_not_create_theme_hint() -> None:
    class FeaturedArtistYTMusicProvider:
        def lookup(self, url: str) -> TrackMetadata:
            return TrackMetadata(
                title='Dear Mother Father (feat. DD"Nakata"Metal)',
                artist="RoughSketch",
                album="Dear Mother Father",
            )

    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: FeaturedArtistYTMusicProvider(),
        musicbrainz_provider_factory=EmptyMusicBrainzProvider,
    )

    resolution = resolver.resolve(
        url="https://youtu.be/stx3Nve3a-0",
        info={
            "extractor_key": "Youtube",
            "title": 'Dear Mother Father (feat. DD"ナカタ"Metal)',
            "artist": 'RoughSketch, DD"Nakata"Metal',
            "description": 'Provided to YouTube by HARDCORE TANO*C\n\n'
            'Dear Mother Father (feat. DD"ナカタ"Metal) · RoughSketch · DD"Nakata"Metal\n\n'
            'Dear Mother Father (feat. DD"Nakata"Metal)\n\n'
            "Auto-generated by YouTube.",
            "webpage_url": "https://www.youtube.com/watch?v=stx3Nve3a-0",
        },
    )

    assert resolution.metadata.title == 'Dear Mother Father (feat. DD"Nakata"Metal)'
    assert resolution.metadata.artist == "RoughSketch"
    assert resolution.candidates == []


def test_youtube_resolver_keeps_compact_title_pattern_as_review_candidate() -> None:
    class EmptyYTMusicProvider:
        def lookup(self, url: str) -> TrackMetadata:
            return TrackMetadata()

    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: EmptyYTMusicProvider(),
        musicbrainz_provider_factory=EmptyMusicBrainzProvider,
    )

    resolution = resolver.resolve(
        url="https://youtu.be/Jty1MDOAKvQ",
        info={
            "extractor_key": "Youtube",
            "title": "린-개미관찰",
            "uploader": "윤다희",
            "upload_date": "20140427",
            "webpage_url": "https://www.youtube.com/watch?v=Jty1MDOAKvQ",
        },
    )

    assert resolution.metadata.title == "린-개미관찰"
    assert resolution.metadata.artist == "윤다희"
    assert resolution.metadata.release_date == "2014-04-27"
    assert resolution.state == ReviewState.REVIEW_REQUIRED
    assert resolution.candidates[0].provider == "title_artist_title"
    assert resolution.candidates[0].metadata.title == "개미관찰"
    assert resolution.candidates[0].metadata.artist == "린"


def test_youtube_resolver_adds_gemma_fallback_as_review_candidate() -> None:
    class EmptyYTMusicProvider:
        def lookup(self, url: str) -> TrackMetadata:
            return TrackMetadata()

    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda auth_path: EmptyYTMusicProvider(),
        musicbrainz_provider_factory=EmptyMusicBrainzProvider,
        generative_suggester_factory=FakeGemmaSuggester,
    )

    resolution = resolver.resolve(
        url="https://youtu.be/abc",
        info={
            "extractor_key": "Youtube",
            "title": "Noisy Video Title",
            "uploader": "Uploader",
        },
    )

    assert any(candidate.provider == "gemma_e2b" for candidate in resolution.candidates)
    assert resolution.metadata.title == "Noisy Video Title"
    assert resolution.metadata.artist == "Uploader"
