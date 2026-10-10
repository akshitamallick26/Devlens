"""API-boundary tests for the pipeline and code intelligence routes. Service
functions are monkeypatched: no database. Covers request bounds (rejected
before any query), error mapping, and response serialization."""

import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

import app.api.routes.pipeline as pipeline_route
from app.codeintel import service as codeintel
from app.codeintel.models import GraphEdgeView, GraphNeighborhood, GraphNode, SourceRange
from app.db.session import get_db
from app.main import app
from app.pipeline import service as pipeline
from app.pipeline.models import PipelineResult, StageResult


@pytest.fixture(autouse=True)
def _override_db() -> Generator[None]:
    app.dependency_overrides[get_db] = lambda: iter([None])
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


client = TestClient(app)
REPO = str(uuid.uuid4())
REV = str(uuid.uuid4())
BASE = f"/repositories/{REPO}/revisions/{REV}"


def _fail(*args: object, **kwargs: object) -> None:
    raise AssertionError("service must not be called for an invalid request")


# ---- Pipeline ----


def _pipeline_result() -> PipelineResult:
    return PipelineResult(
        repository_id=uuid.UUID(REPO),
        revision_id=uuid.UUID(REV),
        overall_status="partial",
        stages=(
            StageResult(
                stage="parsing",
                requested=True,
                action="ran",
                status="completed",
                up_to_date=True,
                analysis_run_id=uuid.uuid4(),
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
                detail=None,
            ),
            StageResult(
                stage="rag_embedding",
                requested=True,
                action="skipped",
                status="unavailable",
                up_to_date=False,
                analysis_run_id=None,
                started_at=None,
                finished_at=None,
                detail="no embedding provider is configured",
            ),
        ),
    )


def test_analyze_defaults_to_full_pipeline_without_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake(
        db: object, repository_id: uuid.UUID, revision_id: uuid.UUID, **kwargs: object
    ) -> PipelineResult:
        calls.append(kwargs)
        return _pipeline_result()

    monkeypatch.setattr(pipeline_route, "analyze_revision", fake)
    response = client.post(f"{BASE}/analyze")
    assert response.status_code == 200
    assert calls == [
        {"run_parsing_stage": True, "run_graph_stage": True, "run_rag_stage": True, "force": False}
    ]
    body = response.json()
    assert body["execution"] == "synchronous"
    assert body["overall_status"] == "partial"
    assert body["stages"][1]["status"] == "unavailable"
    assert "progress" not in body


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (pipeline.RepositoryNotFoundError("no repo"), 404),
        (pipeline.RevisionNotFoundError("no rev"), 404),
        (pipeline.RevisionConflictError("other repo"), 409),
        (pipeline.AnalysisInProgressError("already running"), 409),
    ],
)
def test_analyze_error_mapping(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int
) -> None:
    def fake(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(pipeline_route, "analyze_revision", fake)
    assert client.post(f"{BASE}/analyze", json={}).status_code == status


def test_analyze_rejects_malformed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_route, "analyze_revision", _fail)
    assert client.post(f"/repositories/x/revisions/{REV}/analyze").status_code == 422


# ---- Bounds are enforced before the service runs ----


@pytest.mark.parametrize(
    "url",
    [
        f"{BASE}/graph/neighborhood?symbol_id={uuid.uuid4()}&depth=5",
        f"{BASE}/graph/neighborhood?symbol_id={uuid.uuid4()}&depth=0",
        f"{BASE}/graph/neighborhood?symbol_id={uuid.uuid4()}&limit=201",
        f"{BASE}/graph/neighborhood?symbol_id={uuid.uuid4()}&direction=sideways",
        f"{BASE}/graph/neighborhood?symbol_id=not-a-uuid",
        f"{BASE}/files?limit=201",
        f"{BASE}/files?offset=-1",
        f"{BASE}/files/{uuid.uuid4()}/source?start_line=0",
        f"{BASE}/symbols?q=",
        f"{BASE}/symbols?q=x&limit=51",
        f"{BASE}/security/findings?limit=1000",
        f"/repositories/{REPO}/revisions?limit=0",
    ],
)
def test_out_of_bounds_requests_are_rejected(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    for name in (
        "graph_neighborhood",
        "browse_directory",
        "get_source_range",
        "search_symbols",
        "list_findings",
        "list_revisions",
    ):
        monkeypatch.setattr(codeintel, name, _fail)
    assert client.get(url).status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (codeintel.RepositoryNotFoundError("x"), 404),
        (codeintel.RevisionNotFoundError("x"), 404),
        (codeintel.NotFoundError("x"), 404),
        (codeintel.RevisionConflictError("x"), 409),
        (codeintel.InvalidRequestError("x"), 422),
    ],
)
def test_code_error_mapping(monkeypatch: pytest.MonkeyPatch, error: Exception, status: int) -> None:
    def fake(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(codeintel, "get_symbol_detail", fake)
    assert client.get(f"{BASE}/symbols/{uuid.uuid4()}").status_code == status


def test_neighborhood_serializes_nodes_and_directed_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    caller, callee = uuid.uuid4(), uuid.uuid4()
    file_id = uuid.uuid4()

    def fake(*args: object, **kwargs: object) -> GraphNeighborhood:
        return GraphNeighborhood(
            revision_id=uuid.UUID(REV),
            focus_symbol_id=callee,
            direction="dependents",
            depth=1,
            node_limit=100,
            nodes=(
                GraphNode(callee, file_id, "app/m.py", "app.m.add", "function", 0, "focus"),
                GraphNode(
                    caller, file_id, "app/c.py", "app.c.compute", "function", 1, "dependents"
                ),
            ),
            edges=(
                GraphEdgeView(
                    source=caller, target=callee, relationship_type="calls", occurrences=1
                ),
            ),
            truncated=False,
            max_depth_reached=1,
            focus_external_references=(),
        )

    monkeypatch.setattr(codeintel, "graph_neighborhood", fake)
    body = client.get(
        f"{BASE}/graph/neighborhood", params={"symbol_id": str(callee), "direction": "dependents"}
    ).json()
    assert body["edges"] == [
        {
            "source": str(caller),
            "target": str(callee),
            "relationship_type": "calls",
            "occurrences": 1,
        }
    ]
    assert [n["depth"] for n in body["nodes"]] == [0, 1]


def test_source_passes_range_and_serializes_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []

    def fake(
        db: object, repo: uuid.UUID, rev: uuid.UUID, file_id: uuid.UUID, start: int, end: int | None
    ) -> SourceRange:
        seen.append((start, end))
        return SourceRange(file_id, "a.py", start, start + 1, 10, ("x = 1", "y = 2"), False)

    monkeypatch.setattr(codeintel, "get_source_range", fake)
    body = client.get(
        f"{BASE}/files/{uuid.uuid4()}/source", params={"start_line": 3, "end_line": 4}
    ).json()
    assert seen == [(3, 4)]
    assert body["lines"] == ["x = 1", "y = 2"]
    assert body["total_lines"] == 10


# ---- Pure helpers ----


@pytest.mark.parametrize("bad", ["../etc", "a/../b", "a//b", "./a", "a\x00b"])
def test_directory_normalization_rejects_traversal_like_input(bad: str) -> None:
    with pytest.raises(codeintel.InvalidRequestError):
        codeintel.normalize_directory(bad)


def test_directory_normalization_strips_slashes() -> None:
    assert codeintel.normalize_directory("/app/api/") == "app/api"
    assert codeintel.normalize_directory("") == ""
