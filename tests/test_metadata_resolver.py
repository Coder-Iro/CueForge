from pathlib import Path

from cueforge.metadata.resolver import MetadataResolver
from cueforge.models import MetadataCandidate, ReviewState, TrackMetadata
from cueforge.sources import SourcePlatform


class FailingYTMusicProvider:
    def lookup(self, url: str) -> TrackMetadata:
        raise AssertionError("YTMusic should not be called for SoundCloud")


class FakeYTMusicProvider:
    def lookup(self, url: str) -> TrackMetadata:
        return TrackMetadata(title="YT Title", artist="YT Artist")


class EmptyYTMusicProvider:
    def lookup(self, url: str) -> TrackMetadata:
        return TrackMetadata()


class FakeYTMusicWithCoverProvider:
    def lookup(self, url: str) -> TrackMetadata:
        return TrackMetadata(
            title="YT Title",
            artist="YT Artist",
            cover_url="https://img.youtube.com/yt-thumb.jpg",
            cover_source="YouTube Music thumbnail",
        )


class OfficialProjectYTMusicProvider:
    def lookup(self, url: str) -> TrackMetadata:
        return TrackMetadata(title="Ex-Otogibanashi", artist="『超かぐや姫 ! 』公式")


class FakeChatGPTSuggester:
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
                provider="chatgpt",
                score=0.72,
                matched_fields=("llm", "title", "artist"),
                metadata=TrackMetadata(title="ChatGPT Song", artist="ChatGPT Artist"),
                raw={"review_only": True},
            )
        ]


class FakeChatGPTArtworkSuggester:
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
                provider="chatgpt",
                score=0.82,
                matched_fields=("title", "artist", "cover_url"),
                metadata=TrackMetadata(
                    title="YT Title",
                    artist="YT Artist",
                    cover_url="https://is1-ssl.mzstatic.com/image/thumb/Music/source/1200x1200bb.jpg",
                    cover_source="official release artwork",
                ),
                raw={"prefer_initial_metadata": True},
            )
        ]


class CloseScoreChatGPTSuggester:
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
                provider="chatgpt",
                score=0.77,
                matched_fields=("title", "artist"),
                metadata=TrackMetadata(title="ray", artist="ゆう。"),
                raw={"prefer_initial_metadata": True},
            )
        ]


def test_soundcloud_resolver_trusts_native_metadata() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FailingYTMusicProvider())

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
    assert resolution.metadata.artist == "DJ Name"
    assert [candidate.provider for candidate in resolution.candidates] == ["soundcloud"]


def test_youtube_resolver_uses_ytmusic_without_external_enrichment() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FakeYTMusicProvider())

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
    assert resolution.metadata.title == "YT Title"
    assert resolution.metadata.artist == "YT Artist"
    assert resolution.state == ReviewState.REVIEW_REQUIRED
    assert resolution.candidates == []


def test_youtube_resolver_passes_oauth_auth_options(tmp_path: Path) -> None:
    calls: list[dict] = []
    oauth_client_file = tmp_path / "google_oauth_client.json"
    oauth_token_file = tmp_path / "ytmusic_oauth_token.json"

    def factory(**kwargs: object) -> FakeYTMusicProvider:
        calls.append(kwargs)
        return FakeYTMusicProvider()

    resolver = MetadataResolver(ytmusic_provider_factory=factory)

    resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={"extractor_key": "Youtube", "title": "Fallback"},
        ytmusic_oauth_client_file=oauth_client_file,
        ytmusic_oauth_token_file=oauth_token_file,
    )

    assert calls[0]["oauth_client_file"] == oauth_client_file
    assert calls[0]["oauth_token_file"] == oauth_token_file


def test_youtube_resolver_keeps_ytmusic_cover_before_platform_thumbnail() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FakeYTMusicWithCoverProvider())

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
    assert resolution.metadata.cover_source == "YouTube Music thumbnail"


def test_youtube_resolver_falls_back_to_platform_thumbnail_when_ytmusic_has_no_cover() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FakeYTMusicProvider())

    resolution = resolver.resolve(
        url="https://music.youtube.com/watch?v=abc",
        info={
            "extractor_key": "Youtube",
            "title": "Fallback",
            "uploader": "Uploader",
            "thumbnail": "https://img.youtube.com/fallback.jpg",
        },
    )

    assert resolution.metadata.cover_url == "https://img.youtube.com/fallback.jpg"
    assert resolution.metadata.cover_source == "platform thumbnail"


def test_youtube_resolver_prefers_release_artwork_over_platform_thumbnail() -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda **_: FakeYTMusicProvider(),
        generative_suggester_factory=FakeChatGPTArtworkSuggester,
    )

    resolution = resolver.resolve(
        url="https://www.youtube.com/watch?v=abc",
        info={
            "extractor_key": "Youtube",
            "title": "YT Title",
            "uploader": "YT Artist",
            "thumbnail": "https://img.youtube.com/fallback.jpg",
        },
    )

    assert resolution.metadata.cover_url == "https://is1-ssl.mzstatic.com/image/thumb/Music/source/1200x1200bb.jpg"
    assert resolution.metadata.cover_source == "official release artwork"


def test_youtube_resolver_prefers_mixed_creator_artist_over_official_project_artist() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: OfficialProjectYTMusicProvider())

    resolution = resolver.resolve(
        url="https://youtu.be/qbT7bBYz5YA",
        info={
            "extractor_key": "Youtube",
            "title": "【Official MV】Ex-Otogibanashi (Anime ver.) - ryo (supercell)",
            "channel": "『超かぐや姫 ! 』公式",
            "uploader": "『超かぐや姫 ! 』公式",
            "creator": "『超かぐや姫 ! 』公式, ryo (supercell)",
        },
    )

    assert resolution.metadata.title == "Ex-Otogibanashi"
    assert resolution.metadata.artist == "ryo (supercell)"


def test_soundcloud_resolver_keeps_native_artwork() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FailingYTMusicProvider())

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
    assert resolution.metadata.cover_source == "SoundCloud native"


def test_youtube_resolver_uses_description_theme_hints_as_review_metadata() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: EmptyYTMusicProvider())

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

    assert resolution.metadata.title == "明日の私に幸あれ"
    assert resolution.metadata.artist == "ナナヲアカリ"
    assert resolution.metadata.genre == "Anison"
    assert resolution.state == ReviewState.REVIEW_REQUIRED
    assert resolution.candidates[0].provider == "description_エンディングテーマ"


def test_youtube_auto_generated_description_does_not_create_theme_hint() -> None:
    class FeaturedArtistYTMusicProvider:
        def lookup(self, url: str) -> TrackMetadata:
            return TrackMetadata(
                title='Dear Mother Father (feat. DD"Nakata"Metal)',
                artist="RoughSketch",
                album="Dear Mother Father",
            )

    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: FeaturedArtistYTMusicProvider())

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
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: EmptyYTMusicProvider())

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


def test_youtube_resolver_applies_badged_title_candidate_when_artist_matches_uploader() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: EmptyYTMusicProvider())

    resolution = resolver.resolve(
        url="https://www.youtube.com/watch?v=wu-xvFB6aRg",
        info={
            "extractor_key": "Youtube",
            "title": "【Rotaeno】Raimukun - OLDSCHOOL TENTACLES",
            "uploader": "Raimukun",
            "channel": "Raimukun",
            "upload_date": "20260601",
            "webpage_url": "https://www.youtube.com/watch?v=wu-xvFB6aRg",
        },
    )

    assert resolution.metadata.title == "OLDSCHOOL TENTACLES"
    assert resolution.metadata.artist == "Raimukun"
    assert resolution.metadata.release_date == "2026-06-01"
    assert resolution.state == ReviewState.REVIEW_REQUIRED
    assert resolution.candidates[0].provider == "title_artist_title"


def test_youtube_resolver_uses_cover_hint_as_initial_review_metadata() -> None:
    resolver = MetadataResolver(ytmusic_provider_factory=lambda **_: EmptyYTMusicProvider())

    resolution = resolver.resolve(
        url="https://youtu.be/MvbPY6mrcy4",
        info={
            "extractor_key": "Youtube",
            "title": "체인소맨 레제편 그 노래 / IRIS OUT - 요네즈 켄시 (COVER)",
            "channel": "계화",
            "uploader": "계화",
            "description": "Original: IRIS OUT - 米津玄師 (yonezu kenshi)",
        },
    )

    assert resolution.metadata.title == "IRIS OUT"
    assert resolution.metadata.artist == "계화"
    assert resolution.state == ReviewState.REVIEW_REQUIRED
    assert resolution.candidates[0].provider == "title_cover"


def test_youtube_resolver_adds_chatgpt_fallback_as_review_candidate() -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda **_: EmptyYTMusicProvider(),
        generative_suggester_factory=FakeChatGPTSuggester,
    )

    resolution = resolver.resolve(
        url="https://youtu.be/abc",
        info={
            "extractor_key": "Youtube",
            "title": "Noisy Video Title",
            "uploader": "Uploader",
        },
    )

    assert any(candidate.provider == "chatgpt" for candidate in resolution.candidates)
    assert resolution.metadata.title == "Noisy Video Title"
    assert resolution.metadata.artist == "Uploader"
    assert resolution.state == ReviewState.REVIEW_REQUIRED


def test_youtube_resolver_orders_close_chatgpt_candidate_before_description_credits() -> None:
    resolver = MetadataResolver(
        ytmusic_provider_factory=lambda **_: EmptyYTMusicProvider(),
        generative_suggester_factory=CloseScoreChatGPTSuggester,
    )

    resolution = resolver.resolve(
        url="https://www.youtube.com/watch?v=A9W86K7i3mQ",
        info={
            "extractor_key": "Youtube",
            "title": "ゆう。 - ray / ゆう。 - cover[オリジナルMV]",
            "uploader": "ゆう。",
            "channel": "ゆう。",
            "description": "Song by ray\nArtist: BUMP OF CHICKEN\nCover: ゆう。",
            "webpage_url": "https://www.youtube.com/watch?v=A9W86K7i3mQ",
        },
    )

    assert resolution.candidates[0].provider == "chatgpt"
    assert resolution.metadata.title == "ray"
    assert resolution.metadata.artist == "ゆう。"
    assert resolution.state == ReviewState.REVIEW_REQUIRED
