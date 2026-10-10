"""Pure tests for app/documentation/fallback.py — no DB, no AI provider.
Every fallback draft must be built only from `facts`/`evidence` already
present in the package — never invented prose."""

import uuid

from app.documentation.fallback import generate_fallback_draft
from app.documentation.models import (
    ArchitectureOverviewFacts,
    DocumentationContextPackage,
    EvidenceItem,
    EvidenceSet,
    ModuleFacts,
    RepositoryOverviewFacts,
    SymbolFact,
    SymbolFacts,
)

REPO_ID = uuid.uuid4()
REV_ID = uuid.uuid4()


def test_repository_overview_fallback_cites_only_real_facts() -> None:
    facts = RepositoryOverviewFacts(
        repository_name="octocat/hello-world",
        default_branch="main",
        revision_commit_sha="a" * 40,
        file_count=12,
        files_by_language=(("python", 10), ("markdown", 2)),
        top_level_modules=("app", "tests"),
        symbol_count=42,
        graph_edge_count=7,
        existing_readme_path=None,
    )
    package = DocumentationContextPackage(
        repository_id=REPO_ID,
        revision_id=REV_ID,
        artifact_type="repository_overview",
        target_file_id=None,
        target_symbol_id=None,
        facts=facts,
        evidence=EvidenceSet(items=()),
        truncated=False,
    )
    draft = generate_fallback_draft(package)
    assert "octocat/hello-world" in draft.title
    assert "12" in draft.summary  # file_count
    assert "42" in draft.summary  # symbol_count
    full_text = draft.summary + "".join(s.content for s in draft.sections)
    assert "python" in full_text
    assert "app" in full_text
    assert draft.evidence_ids == ()  # no README evidence supplied


def test_repository_overview_fallback_notes_existing_readme_as_evidence() -> None:
    facts = RepositoryOverviewFacts(
        repository_name="o/r",
        default_branch="main",
        revision_commit_sha="b" * 40,
        file_count=1,
        files_by_language=(),
        top_level_modules=(),
        symbol_count=0,
        graph_edge_count=0,
        existing_readme_path="README.md",
    )
    readme_evidence = EvidenceItem(
        evidence_id="D1",
        repository_id=REPO_ID,
        revision_id=REV_ID,
        file_path="README.md",
        symbol_id=None,
        qualified_name=None,
        graph_edge_relationship=None,
        line_start=None,
        line_end=None,
        kind="existing_documentation",
        content="# Hello",
    )
    package = DocumentationContextPackage(
        repository_id=REPO_ID,
        revision_id=REV_ID,
        artifact_type="repository_overview",
        target_file_id=None,
        target_symbol_id=None,
        facts=facts,
        evidence=EvidenceSet(items=(readme_evidence,)),
        truncated=False,
    )
    draft = generate_fallback_draft(package)
    assert "D1" in draft.evidence_ids
    assert any("README.md" in s.content for s in draft.sections)
    assert any("may be stale" in s.content for s in draft.sections)


def test_architecture_overview_fallback_reports_relationship_counts() -> None:
    facts = ArchitectureOverviewFacts(
        module_count=3,
        key_symbols=(
            SymbolFact(
                symbol_id=uuid.uuid4(),
                qualified_name="app.foo.bar",
                kind="function",
                file_path="app/foo.py",
                start_line=1,
                end_line=5,
                signature="def bar()",
            ),
        ),
        relationship_counts=(("calls", 10), ("imports", 4)),
        existing_readme_path=None,
    )
    package = DocumentationContextPackage(
        repository_id=REPO_ID,
        revision_id=REV_ID,
        artifact_type="architecture_overview",
        target_file_id=None,
        target_symbol_id=None,
        facts=facts,
        evidence=EvidenceSet(items=()),
        truncated=False,
    )
    draft = generate_fallback_draft(package)
    full_text = draft.summary + "".join(s.content for s in draft.sections)
    assert "calls" in full_text and "10" in full_text
    assert "app.foo.bar" in full_text


def test_module_documentation_fallback_lists_symbols_and_dependencies() -> None:
    facts = ModuleFacts(
        file_path="app/math_utils.py",
        language="python",
        imports=("os",),
        symbols=(
            SymbolFact(
                symbol_id=uuid.uuid4(),
                qualified_name="app.math_utils.add",
                kind="function",
                file_path="app/math_utils.py",
                start_line=1,
                end_line=2,
                signature="def add(a, b)",
            ),
        ),
        outbound_dependencies=("app/other.py",),
        inbound_dependents=("app/caller.py",),
    )
    package = DocumentationContextPackage(
        repository_id=REPO_ID,
        revision_id=REV_ID,
        artifact_type="module_documentation",
        target_file_id=uuid.uuid4(),
        target_symbol_id=None,
        facts=facts,
        evidence=EvidenceSet(items=()),
        truncated=False,
    )
    draft = generate_fallback_draft(package)
    full_text = draft.summary + "".join(s.content for s in draft.sections)
    assert "app/math_utils.py" in full_text
    assert "app.math_utils.add" in full_text
    assert "app/other.py" in full_text
    assert "app/caller.py" in full_text


def test_symbol_documentation_fallback_lists_callers_and_callees() -> None:
    caller = SymbolFact(
        symbol_id=uuid.uuid4(),
        qualified_name="app.caller.compute",
        kind="function",
        file_path="app/caller.py",
        start_line=1,
        end_line=3,
        signature="def compute()",
        relationship_type="calls",
    )
    facts = SymbolFacts(
        symbol_id=uuid.uuid4(),
        qualified_name="app.math_utils.add",
        kind="function",
        file_path="app/math_utils.py",
        start_line=1,
        end_line=2,
        signature="def add(a, b)",
        callers=(caller,),
        callees=(),
        inherits_from=(),
    )
    package = DocumentationContextPackage(
        repository_id=REPO_ID,
        revision_id=REV_ID,
        artifact_type="symbol_documentation",
        target_file_id=None,
        target_symbol_id=uuid.uuid4(),
        facts=facts,
        evidence=EvidenceSet(items=()),
        truncated=False,
    )
    draft = generate_fallback_draft(package)
    full_text = draft.summary + "".join(s.content for s in draft.sections)
    assert "app.math_utils.add" in full_text
    assert "app.caller.compute" in full_text
    assert "def add(a, b)" in full_text


def test_fallback_never_invents_text_beyond_facts_and_evidence() -> None:
    """A minimal, empty-everything package must still produce a draft with
    no fabricated claims — every section either reflects a real (even if
    zero) count or is omitted."""
    facts = RepositoryOverviewFacts(
        repository_name="o/r",
        default_branch="main",
        revision_commit_sha="c" * 40,
        file_count=0,
        files_by_language=(),
        top_level_modules=(),
        symbol_count=0,
        graph_edge_count=0,
        existing_readme_path=None,
    )
    package = DocumentationContextPackage(
        repository_id=REPO_ID,
        revision_id=REV_ID,
        artifact_type="repository_overview",
        target_file_id=None,
        target_symbol_id=None,
        facts=facts,
        evidence=EvidenceSet(items=()),
        truncated=False,
    )
    draft = generate_fallback_draft(package)
    assert len(draft.sections) == 1  # only "Repository Statistics" — no README section
    assert "0" in draft.summary
