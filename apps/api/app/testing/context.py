"""Pure test-generation context assembly — no SQLAlchemy, no DB, no AI
provider. Combines a bounded TestTarget (already deterministically
discovered) and already-loaded source text into one deterministic
TestContextPackage, under the identical "degrade to metadata-only, never
omit, never truncate mid-excerpt" budget discipline app/impact/context.py
and app/rag/context.py both already established.

Priority order (never reordered by content, length, or any LLM input):
target source, then callers, then callees, then related existing tests,
then configuration — matching the T1/T2/T3/T4/T5 example in the testing
phase's own spec.
"""

import uuid
from dataclasses import dataclass

from app.testing.citations import Evidence, EvidenceSet
from app.testing.models import TestTarget

MAX_EVIDENCE_ITEMS = 12
MAX_TOTAL_CONTENT_CHARS = 20_000


@dataclass(frozen=True)
class ConfigurationFile:
    file_path: str
    content: str | None


@dataclass(frozen=True)
class TestContextPackage:
    __test__ = False  # not a test class — see app/testing/models.py::TestTarget

    evidence: EvidenceSet


@dataclass
class _Candidate:
    file_path: str
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    line_start: int | None
    line_end: int | None
    kind: str
    content: str | None = None


def assemble_test_context(
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    target: TestTarget,
    target_source: str | None,
    caller_sources: dict[uuid.UUID, str | None],
    callee_sources: dict[uuid.UUID, str | None],
    existing_test_sources: dict[uuid.UUID, str | None],
    configuration: tuple[ConfigurationFile, ...] = (),
) -> TestContextPackage:
    candidates: list[_Candidate] = [
        _Candidate(
            file_path=target.file_path,
            symbol_id=target.target_symbol_id,
            qualified_name=target.qualified_name,
            line_start=target.start_line,
            line_end=target.end_line,
            kind="target_source",
            content=target_source,
        )
    ]

    for caller in target.callers:
        candidates.append(
            _Candidate(
                file_path=caller.file_path,
                symbol_id=caller.symbol_id,
                qualified_name=caller.qualified_name,
                line_start=caller.start_line,
                line_end=caller.end_line,
                kind="caller",
                content=caller_sources.get(caller.symbol_id),
            )
        )

    for callee in target.callees:
        candidates.append(
            _Candidate(
                file_path=callee.file_path,
                symbol_id=callee.symbol_id,
                qualified_name=callee.qualified_name,
                line_start=callee.start_line,
                line_end=callee.end_line,
                kind="callee",
                content=callee_sources.get(callee.symbol_id),
            )
        )

    for existing_test in target.existing_tests:
        candidates.append(
            _Candidate(
                file_path=existing_test.file_path,
                symbol_id=existing_test.symbol_id,
                qualified_name=existing_test.test_identifier,
                line_start=None,
                line_end=None,
                kind="existing_test",
                content=existing_test_sources.get(existing_test.symbol_id),
            )
        )

    for config_file in configuration:
        candidates.append(
            _Candidate(
                file_path=config_file.file_path,
                symbol_id=None,
                qualified_name=None,
                line_start=None,
                line_end=None,
                kind="configuration",
                content=config_file.content,
            )
        )

    accepted: list[Evidence] = []
    total_chars = 0
    for candidate in candidates:
        if len(accepted) >= MAX_EVIDENCE_ITEMS:
            break
        content = candidate.content
        content_len = len(content) if content else 0
        if content is not None and total_chars + content_len > MAX_TOTAL_CONTENT_CHARS:
            content = None  # degrade to metadata-only; never omit, never truncate mid-excerpt
        else:
            total_chars += content_len
        accepted.append(
            Evidence(
                evidence_id=f"T{len(accepted) + 1}",
                repository_id=repository_id,
                revision_id=revision_id,
                file_path=candidate.file_path,
                symbol_id=candidate.symbol_id,
                qualified_name=candidate.qualified_name,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                kind=candidate.kind,
                content=content,
            )
        )

    return TestContextPackage(evidence=EvidenceSet(items=tuple(accepted)))
