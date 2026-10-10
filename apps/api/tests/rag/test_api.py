"""API-boundary tests for the QA route: request validation and response
serialization. The underlying service function is monkeypatched — no
database needed, same convention as tests/impact/test_api.py.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.qa as qa_route
from app.db.session import get_db
from app.main import app
from app.rag.citations import Evidence, EvidenceSet
from app.rag.qa import QAAnswer, QAResult, RevisionNotFoundError
from app.rag.query import MAX_QUESTION_CHARS


@pytest.fixture(autouse=True)
def _override_db() -> Generator[None]:
    app.dependency_overrides[get_db] = lambda: iter([None])
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


client = TestClient(app)

REPOSITORY_ID = str(uuid.uuid4())
REVISION_ID = str(uuid.uuid4())


def _evidence_set() -> EvidenceSet:
    return EvidenceSet(
        items=(
            Evidence(
                evidence_id="Q1",
                repository_id=uuid.UUID(REPOSITORY_ID),
                revision_id=uuid.UUID(REVISION_ID),
                file_path="a.py",
                symbol_id=None,
                qualified_name="a.f",
                line_start=1,
                line_end=2,
                chunk_id=uuid.uuid4(),
                retrieval_reason="matched",
                retrieval_source="lexical",
                relationship_type=None,
                content="def f(): pass",
            ),
        )
    )


def test_question_is_required() -> None:
    response = client.post(f"/repositories/{REPOSITORY_ID}/qa", json={"revision_id": REVISION_ID})
    assert response.status_code == 422


def test_top_k_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/qa",
        json={"revision_id": REVISION_ID, "question": "q", "top_k": 1000},
    )
    assert response.status_code == 422


def test_revision_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionNotFoundError("no such revision")

    monkeypatch.setattr(qa_route, "run_qa", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/qa", json={"revision_id": REVISION_ID, "question": "q"}
    )
    assert response.status_code == 404


def test_retrieval_only_result_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    result = QAResult(analysis_status="retrieval_only", evidence_set=_evidence_set())
    monkeypatch.setattr(qa_route, "run_qa", lambda *a, **k: result)
    monkeypatch.setattr(qa_route, "get_provider", lambda settings: None)
    monkeypatch.setattr(qa_route, "get_embedding_provider", lambda settings: None)

    response = client.post(
        f"/repositories/{REPOSITORY_ID}/qa", json={"revision_id": REVISION_ID, "question": "q"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "retrieval_only"
    assert body["answer"] is None
    assert len(body["evidence_items"]) == 1
    assert body["evidence_items"][0]["evidence_id"] == "Q1"


def test_answered_result_is_serialized_not_an_orm_object(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = QAAnswer(
        answer="x [Q1]",
        evidence=["Q1"],
        limitations="",
        related_symbols=[],
        suggested_next_questions=[],
    )
    from app.rag.qa import ResolvedEvidence

    result = QAResult(
        analysis_status="answered",
        evidence_set=_evidence_set(),
        answer=answer,
        evidence_map={"Q1": ResolvedEvidence(file_path="a.py", line_start=1, line_end=2)},
    )
    monkeypatch.setattr(qa_route, "run_qa", lambda *a, **k: result)
    monkeypatch.setattr(qa_route, "get_provider", lambda settings: object())
    monkeypatch.setattr(qa_route, "get_embedding_provider", lambda settings: None)

    response = client.post(
        f"/repositories/{REPOSITORY_ID}/qa", json={"revision_id": REVISION_ID, "question": "q"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "answered"
    assert body["answer"]["answer"] == "x [Q1]"
    assert body["evidence"]["Q1"]["file_path"] == "a.py"
    assert isinstance(
        body["evidence_items"][0]["symbol_id"], (str, type(None))
    )  # never a raw UUID/ORM obj


@pytest.mark.parametrize("question", ["", "   ", "\n\t", "x" * (MAX_QUESTION_CHARS + 1)])
def test_blank_or_oversized_questions_are_rejected_before_retrieval(
    monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    def _must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("retrieval must not run for an invalid question")

    monkeypatch.setattr(qa_route, "run_qa", _must_not_run)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/qa",
        json={"revision_id": REVISION_ID, "question": question},
    )
    assert response.status_code == 422


def test_question_at_the_length_limit_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _record(*args: object, **kwargs: object) -> QAResult:
        seen.append(str(args[8]))
        return QAResult(analysis_status="retrieval_only", evidence_set=EvidenceSet(items=()))

    monkeypatch.setattr(qa_route, "run_qa", _record)
    monkeypatch.setattr(qa_route, "get_provider", lambda settings: None)
    monkeypatch.setattr(qa_route, "get_embedding_provider", lambda settings: None)
    question = "a" * MAX_QUESTION_CHARS
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/qa",
        json={"revision_id": REVISION_ID, "question": question},
    )
    assert response.status_code == 200
    assert seen == [question]
    assert response.json()["evidence_items"] == []
