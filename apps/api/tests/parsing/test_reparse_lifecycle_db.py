"""Re-parse lifecycle vs historical breadcrumbs, on live PostgreSQL with the
real ingestion -> parsing -> graph pipeline.

Contract: a revision's source is immutable, so re-parsing it reproduces the
same symbols and each keeps its Symbol id (identity = file, kind,
qualified_name, then source order among equal keys). Everything holding a
Symbol id (session breadcrumbs, generated tests, documentation, chunks,
security observations) stays valid. Only symbols the new parse no longer
produces are deleted, and if a breadcrumb still references one, the
re-parse is refused and nothing changes: a breadcrumb is never nulled,
deleted, or re-pointed at another symbol."""

import dataclasses
import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

import app.parsing.service as parsing_service
from app.graph.service import run_graph_construction
from app.investigation import service as sessions
from app.models import (
    AnalysisRun,
    GeneratedTest,
    GraphEdge,
    RepositoryRevision,
    SessionContextRef,
    SourceBlob,
    SourceFile,
    Symbol,
)
from app.parsing.parser import parse_source_file
from app.parsing.result import ParseResult
from app.parsing.service import ParsingError, run_parsing
from tests.automation.conftest import ingest_revision, modify_and_commit
from tests.ingestion.conftest import run_git

SOURCE = (
    "class Greeter:\n"
    "    @property\n"
    "    def name(self):\n"
    "        return 'x'\n"
    "\n"
    "    @name.setter\n"
    "    def name(self, value):\n"
    "        pass\n"
    "\n"
    "    def greet(self):\n"
    "        return helper()\n"
    "\n"
    "\n"
    "def helper():\n"
    "    return 1\n"
)


@pytest.fixture
def lifecycle_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "lifecycle-repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "mod.py").write_text(SOURCE)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "r1")
    return repo


def _analyze(db: Session, repo: Path, owner: str = "lifecycle") -> tuple[uuid.UUID, uuid.UUID]:
    result = ingest_revision(db, repo, owner=owner)
    repository_id, revision_id = uuid.UUID(result.repository_id), uuid.UUID(result.revision_id)
    run_parsing(db, repository_id, revision_id)
    run_graph_construction(db, repository_id, revision_id)
    return repository_id, revision_id


def _symbols(db: Session, revision_id: uuid.UUID) -> dict[uuid.UUID, tuple[Any, ...]]:
    db.expire_all()
    rows = db.execute(
        select(
            Symbol.id,
            SourceFile.path,
            Symbol.kind,
            Symbol.qualified_name,
            Symbol.start_line,
            Symbol.end_line,
            Symbol.parent_symbol_id,
        )
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(Symbol.revision_id == revision_id)
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _symbol_id(
    db: Session, revision_id: uuid.UUID, qualified_name: str, line: int = 0
) -> uuid.UUID:
    stmt = select(Symbol.id).where(
        Symbol.revision_id == revision_id, Symbol.qualified_name == qualified_name
    )
    if line:
        stmt = stmt.where(Symbol.start_line == line)
    return db.execute(stmt).scalar_one()


def _edges(db: Session, revision_id: uuid.UUID) -> set[tuple[uuid.UUID, uuid.UUID, str]]:
    return {
        (source, target, relationship)
        for source, target, relationship in db.execute(
            select(
                GraphEdge.source_symbol_id, GraphEdge.target_symbol_id, GraphEdge.relationship_type
            ).where(GraphEdge.revision_id == revision_id, GraphEdge.target_symbol_id.is_not(None))
        ).all()
    }


def _parse_runs(db: Session, revision_id: uuid.UUID) -> list[AnalysisRun]:
    db.expire_all()
    return list(
        db.execute(
            select(AnalysisRun)
            .where(AnalysisRun.revision_id == revision_id, AnalysisRun.analysis_type == "parsing")
            .order_by(AnalysisRun.started_at, AnalysisRun.id)
        ).scalars()
    )


def _session_with_breadcrumb(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, symbol_id: uuid.UUID
) -> uuid.UUID:
    session_id = sessions.create_session(db, repository_id, revision_id, title="trace").id
    sessions.append_message(db, repository_id, session_id, "user", "why does greet call helper?")
    sessions.add_context_ref(db, repository_id, session_id, "selected_symbol", symbol_id=symbol_id)
    return session_id


def _snapshot(db: Session, repository_id: uuid.UUID, session_id: uuid.UUID) -> Any:
    return sessions.get_session(db, repository_id, session_id)


# ---- 1/2/7. A breadcrumb no longer blocks re-parse, and stays valid ----


def test_selected_symbol_breadcrumb_survives_reparse_and_still_resolves(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    helper = _symbol_id(db_session, revision_id, "app.mod.helper")
    identity_before = _symbols(db_session, revision_id)[helper]
    session_id = _session_with_breadcrumb(db_session, repository_id, revision_id, helper)
    before = _snapshot(db_session, repository_id, session_id)

    result = run_parsing(db_session, repository_id, revision_id)

    assert result.status == "completed"
    after = _snapshot(db_session, repository_id, session_id)
    assert after == before  # messages, refs, recency: untouched
    (ref,) = after.context_refs
    assert ref.symbol_id == helper
    resolved = db_session.get(Symbol, ref.symbol_id, populate_existing=True)
    assert resolved is not None and resolved.revision_id == revision_id
    assert _symbols(db_session, revision_id)[helper] == identity_before


# ---- 3/9. Idempotent, no duplicates, provenance moves to the latest run ----


def test_reparse_is_idempotent_and_keeps_every_id(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    first = _symbols(db_session, revision_id)
    assert len(first) == 7  # 2 modules, class, getter, setter, greet, helper

    for _ in range(3):
        run_parsing(db_session, repository_id, revision_id)
        assert _symbols(db_session, revision_id) == first

    latest = _parse_runs(db_session, revision_id)[-1]
    assert latest.status == "completed"
    produced_by = set(
        db_session.execute(
            select(Symbol.analysis_run_id).where(Symbol.revision_id == revision_id)
        ).scalars()
    )
    assert produced_by == {latest.id}
    assert len(_parse_runs(db_session, revision_id)) == 4  # history is append-only


def test_equal_qualified_names_keep_their_own_ids_in_source_order(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    getter = _symbol_id(db_session, revision_id, "app.mod.Greeter.name", line=3)
    setter = _symbol_id(db_session, revision_id, "app.mod.Greeter.name", line=7)
    run_parsing(db_session, repository_id, revision_id)
    assert _symbol_id(db_session, revision_id, "app.mod.Greeter.name", line=3) == getter
    assert _symbol_id(db_session, revision_id, "app.mod.Greeter.name", line=7) == setter


def test_records_holding_a_symbol_id_survive_reparse(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    helper = _symbol_id(db_session, revision_id, "app.mod.helper")
    content = "def test_helper():\n    assert True\n"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    db_session.add(
        SourceBlob(content_hash=content_hash, inline_content=content, size_bytes=len(content))
    )
    db_session.flush()
    generated = GeneratedTest(
        repository_id=repository_id,
        revision_id=revision_id,
        target_symbol_id=helper,
        proposed_path="tests/test_helper.py",
        content_hash=content_hash,
        language="python",
    )
    db_session.add(generated)
    db_session.commit()

    run_parsing(db_session, repository_id, revision_id)

    reloaded = db_session.get(GeneratedTest, generated.id, populate_existing=True)
    assert reloaded is not None and reloaded.target_symbol_id == helper


# ---- 4. Graph rebuild ----


def test_graph_rebuild_after_reparse_matches_the_original_graph(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    original = _edges(db_session, revision_id)
    greet = _symbol_id(db_session, revision_id, "app.mod.Greeter.greet")
    helper = _symbol_id(db_session, revision_id, "app.mod.helper")
    assert (greet, helper, "calls") in original

    run_parsing(db_session, repository_id, revision_id)
    run_graph_construction(db_session, repository_id, revision_id)

    assert _edges(db_session, revision_id) == original
    source, target = aliased(Symbol), aliased(Symbol)
    dangling = db_session.execute(
        select(func.count())
        .select_from(GraphEdge)
        .join(source, source.id == GraphEdge.source_symbol_id)
        .outerjoin(target, target.id == GraphEdge.target_symbol_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(
            (source.revision_id != revision_id)
            | (GraphEdge.target_symbol_id.is_not(None) & (target.revision_id != revision_id))
        )
    ).scalar_one()
    assert dangling == 0


# ---- 5/6. Revision isolation ----


def test_reparsing_r1_never_touches_r2_and_breadcrumbs_never_drift(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repository_id, r1 = _analyze(db_session, lifecycle_repo)
    modify_and_commit(
        lifecycle_repo, "app/mod.py", SOURCE + "\n\ndef added():\n    return 2\n", "r2"
    )
    _, r2 = _analyze(db_session, lifecycle_repo)
    r1_helper = _symbol_id(db_session, r1, "app.mod.helper")
    r2_helper = _symbol_id(db_session, r2, "app.mod.helper")
    assert r1_helper != r2_helper  # same name, separate revision-scoped rows

    session_id = sessions.create_session(db_session, repository_id, r2).id
    sessions.add_context_ref(
        db_session, repository_id, session_id, "selected_symbol", symbol_id=r1_helper
    )
    r1_before, r2_before = _symbols(db_session, r1), _symbols(db_session, r2)
    r2_edges = _edges(db_session, r2)

    run_parsing(db_session, repository_id, r1)
    assert _symbols(db_session, r1) == r1_before
    assert _symbols(db_session, r2) == r2_before and _edges(db_session, r2) == r2_edges

    run_parsing(db_session, repository_id, r2)
    assert _symbols(db_session, r1) == r1_before
    (ref,) = _snapshot(db_session, repository_id, session_id).context_refs
    assert ref.symbol_id == r1_helper  # the historical R1 symbol, never R2's helper
    resolved = db_session.get(Symbol, ref.symbol_id, populate_existing=True)
    assert resolved is not None and resolved.revision_id == r1


# ---- 10. Cross-repository ----


def test_identical_repositories_never_share_or_borrow_symbol_ids(
    db_session: Session, lifecycle_repo: Path
) -> None:
    repo_a, rev_a = _analyze(db_session, lifecycle_repo, owner="lifecycle-a")
    repo_b, rev_b = _analyze(db_session, lifecycle_repo, owner="lifecycle-b")
    a_before, b_before = _symbols(db_session, rev_a), _symbols(db_session, rev_b)
    assert a_before.keys().isdisjoint(b_before.keys())

    run_parsing(db_session, repo_a, rev_a)
    assert _symbols(db_session, rev_a) == a_before
    assert _symbols(db_session, rev_b) == b_before
    with pytest.raises(parsing_service.RevisionNotFoundError):
        run_parsing(db_session, repo_b, rev_a)
    assert _symbols(db_session, rev_a) == a_before


# ---- Symbols the new parse no longer produces (e.g. an analyzer change) ----


def _parser_without(qualified_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def parse(content: str, path: str, language: str | None) -> ParseResult:
        result = parse_source_file(content, path, language)
        kept = [s for s in result.symbols if s.qualified_name != qualified_name]
        return dataclasses.replace(result, symbols=kept)

    monkeypatch.setattr(parsing_service, "parse_source_file", parse)


def test_unreferenced_vanished_symbol_is_deleted_and_the_rest_keep_their_ids(
    db_session: Session, lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    before = _symbols(db_session, revision_id)
    helper = _symbol_id(db_session, revision_id, "app.mod.helper")

    _parser_without("app.mod.helper", monkeypatch)
    run_parsing(db_session, repository_id, revision_id)

    after = _symbols(db_session, revision_id)
    assert after == {k: v for k, v in before.items() if k != helper}
    assert _parse_runs(db_session, revision_id)[-1].status == "completed"


def test_vanished_symbol_with_a_breadcrumb_refuses_reparse_and_changes_nothing(
    db_session: Session, lifecycle_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _analyze(db_session, lifecycle_repo)
    helper = _symbol_id(db_session, revision_id, "app.mod.helper")
    session_id = _session_with_breadcrumb(db_session, repository_id, revision_id, helper)
    symbols_before, edges_before = (
        _symbols(db_session, revision_id),
        _edges(db_session, revision_id),
    )
    session_before = _snapshot(db_session, repository_id, session_id)

    _parser_without("app.mod.helper", monkeypatch)
    with pytest.raises(ParsingError, match="referenced by 1 investigation session breadcrumb"):
        run_parsing(db_session, repository_id, revision_id)

    assert _symbols(db_session, revision_id) == symbols_before
    assert _edges(db_session, revision_id) == edges_before
    assert _snapshot(db_session, repository_id, session_id) == session_before
    failed = _parse_runs(db_session, revision_id)[-1]
    assert failed.status == "failed" and "breadcrumb" in (failed.error_message or "")
    assert (
        db_session.execute(
            select(func.count()).where(SessionContextRef.symbol_id == helper)
        ).scalar_one()
        == 1
    )

    monkeypatch.undo()  # the real parser again: the same revision parses cleanly
    run_parsing(db_session, repository_id, revision_id)
    assert _symbols(db_session, revision_id) == symbols_before
    assert _parse_runs(db_session, revision_id)[-1].status == "completed"
    revision = db_session.get(RepositoryRevision, revision_id, populate_existing=True)
    assert revision is not None and revision.latest_analysis_status == "completed"
