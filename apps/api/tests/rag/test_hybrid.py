"""Pure RRF combination tests — no DB, always run."""

import uuid

from app.rag.hybrid import RRF_K, combine_hybrid
from app.rag.models import RetrievedChunk


def _chunk(chunk_id: uuid.UUID, rank: int, name: str = "f") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        file_id=uuid.uuid4(),
        file_path=f"{name}.py",
        symbol_id=None,
        qualified_name=name,
        start_line=1,
        end_line=2,
        rank=rank,
    )


def test_chunk_found_by_both_retrievers_outranks_one_found_by_only_one() -> None:
    both = uuid.uuid4()
    lexical_only = uuid.uuid4()
    lexical = [_chunk(both, 1), _chunk(lexical_only, 2)]
    vector = [_chunk(both, 1)]
    result = combine_hybrid(lexical, vector)
    assert result[0].chunk_id == both
    assert result[0].sources == ("lexical", "vector")
    assert result[1].chunk_id == lexical_only
    assert result[1].sources == ("lexical",)


def test_rrf_score_matches_the_documented_formula() -> None:
    chunk_id = uuid.uuid4()
    lexical = [_chunk(chunk_id, 3)]
    result = combine_hybrid(lexical, [])
    expected = 1.0 / (RRF_K + 3)
    assert result[0].rrf_score == expected


def test_combined_score_is_the_sum_of_both_retrievers_terms() -> None:
    chunk_id = uuid.uuid4()
    result = combine_hybrid([_chunk(chunk_id, 2)], [_chunk(chunk_id, 5)])
    expected = 1.0 / (RRF_K + 2) + 1.0 / (RRF_K + 5)
    assert result[0].rrf_score == expected


def test_tie_breaking_is_by_chunk_id_not_insertion_order() -> None:
    a = uuid.UUID(int=1)
    b = uuid.UUID(int=2)
    # Both get the identical score (same rank, each found by one retriever).
    lexical = [_chunk(b, 1, "b"), _chunk(a, 1, "a")]
    result = combine_hybrid(lexical, [])
    assert result[0].rrf_score == result[1].rrf_score
    assert [r.chunk_id for r in result] == sorted([a, b])


def test_vector_only_result_still_scores_and_appears() -> None:
    chunk_id = uuid.uuid4()
    result = combine_hybrid([], [_chunk(chunk_id, 1)])
    assert len(result) == 1
    assert result[0].sources == ("vector",)


def test_no_results_from_either_retriever_produces_empty_result() -> None:
    assert combine_hybrid([], []) == []


def test_ordering_is_deterministic_across_repeated_calls() -> None:
    lexical = [_chunk(uuid.uuid4(), i) for i in range(1, 6)]
    vector = [_chunk(uuid.uuid4(), i) for i in range(1, 6)]
    first = combine_hybrid(lexical, vector)
    second = combine_hybrid(lexical, vector)
    assert first == second


def test_provenance_fields_are_preserved_from_the_original_retrieved_chunk() -> None:
    chunk_id = uuid.uuid4()
    chunk = RetrievedChunk(
        chunk_id=chunk_id,
        file_id=uuid.uuid4(),
        file_path="payments/checkout.py",
        symbol_id=uuid.uuid4(),
        qualified_name="checkout.run",
        start_line=10,
        end_line=20,
        rank=1,
    )
    result = combine_hybrid([chunk], [])
    assert result[0].file_path == "payments/checkout.py"
    assert result[0].qualified_name == "checkout.run"
    assert result[0].start_line == 10
    assert result[0].end_line == 20
