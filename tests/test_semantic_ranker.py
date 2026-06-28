from cueforge.metadata.semantic import SemanticCandidateRanker, SemanticRankerConfig
from cueforge.models import MetadataCandidate, TrackMetadata


class FakeEmbeddingModel:
    def similarity(self, left: str, right: str) -> float:
        if "Correct Song" in right:
            return 0.95
        return 0.20


class LowEmbeddingModel:
    def similarity(self, left: str, right: str) -> float:
        return 0.20


def test_semantic_ranker_reorders_candidates_and_records_score() -> None:
    ranker = SemanticCandidateRanker(model=FakeEmbeddingModel())
    candidates = [
        MetadataCandidate(
            provider="title_artist_title",
            score=0.70,
            matched_fields=("title", "artist"),
            metadata=TrackMetadata(title="Wrong Song", artist="Wrong Artist"),
        ),
        MetadataCandidate(
            provider="description_credits",
            score=0.70,
            matched_fields=("description",),
            metadata=TrackMetadata(title="Correct Song", artist="Correct Artist"),
        ),
    ]

    ranked = ranker.rerank(
        info={"title": "Correct Artist - Correct Song", "uploader": "Correct Artist"},
        reference=TrackMetadata(title="Correct Artist - Correct Song", artist="Correct Artist"),
        candidates=candidates,
    )

    assert ranked[0].metadata.title == "Correct Song"
    assert ranked[0].raw["semantic_score"] == 1.0
    assert ranked[0].raw["semantic_embedding_score"] == 0.95
    assert ranked[0].raw["semantic_evidence_score"] == 1.0
    assert "semantic" in ranked[0].matched_fields


def test_semantic_ranker_caps_unverified_title_hints_below_auto_approval() -> None:
    ranker = SemanticCandidateRanker(
        config=SemanticRankerConfig(title_hint_cap=0.84),
        model=FakeEmbeddingModel(),
    )
    candidate = MetadataCandidate(
        provider="title_artist_title",
        score=0.84,
        matched_fields=("title", "artist"),
        metadata=TrackMetadata(title="Correct Song", artist="Correct Artist"),
    )

    ranked = ranker.rerank(
        info={"title": "Correct Artist - Correct Song"},
        reference=TrackMetadata(title="Correct Artist - Correct Song", artist="Uploader"),
        candidates=[candidate],
    )

    assert ranked[0].score == 0.84


def test_semantic_ranker_uses_onnx_score_for_gemma_candidates() -> None:
    ranker = SemanticCandidateRanker(
        config=SemanticRankerConfig(title_hint_cap=0.84),
        model=FakeEmbeddingModel(),
    )
    candidate = MetadataCandidate(
        provider="gemma_e2b",
        score=0.0,
        matched_fields=("gemma_e2b", "title", "artist"),
        metadata=TrackMetadata(title="Correct Song", artist="Correct Artist"),
        raw={"review_only": True, "requires_semantic_score": True},
    )

    ranked = ranker.rerank(
        info={"title": "Correct Artist - Correct Song"},
        reference=TrackMetadata(title="Correct Artist - Correct Song", artist="Uploader"),
        candidates=[candidate],
    )

    assert ranked[0].score == 0.84
    assert ranked[0].raw["semantic_score"] == 1.0
    assert ranked[0].raw["semantic_embedding_score"] == 0.95
    assert ranked[0].raw["semantic_evidence_score"] == 1.0


def test_semantic_ranker_uses_source_evidence_when_minilm_underrates_short_gemma_candidate() -> None:
    ranker = SemanticCandidateRanker(
        config=SemanticRankerConfig(title_hint_cap=0.84),
        model=LowEmbeddingModel(),
    )
    candidate = MetadataCandidate(
        provider="gemma_e2b",
        score=0.0,
        matched_fields=("gemma_e2b", "title", "artist"),
        metadata=TrackMetadata(title="출항", artist="아카네 리제"),
        raw={"review_only": True, "requires_semantic_score": True},
    )

    ranked = ranker.rerank(
        info={
            "title": "출항 [抜錨(발묘) / 나나호시 관현악단] ㅣ아카네 리제(Akane Lize) 【COVER】",
            "channel": "아카네 리제 AKANE LIZE",
            "uploader": "아카네 리제 AKANE LIZE",
            "description": "Vocal 아카네 리제(Akane Lize)\nOriginal 나나호시 관현악단",
        },
        reference=TrackMetadata(title="출항", artist="아카네 리제"),
        candidates=[candidate],
    )

    assert ranked[0].score == 0.84
    assert ranked[0].raw["semantic_score"] == 1.0
    assert ranked[0].raw["semantic_embedding_score"] == 0.2
    assert ranked[0].raw["semantic_evidence_score"] == 1.0
