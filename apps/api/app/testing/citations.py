"""Test-generation evidence objects and citation validation — pure, no DB,
no AI provider. "T<n>" prefix, deliberately distinct from Impact
Intelligence's "E<n>" (app/impact/evidence.py) and RAG's "Q<n>"
(app/rag/citations.py). Re-implemented here rather than generalizing either
already-approved module for a cosmetic prefix difference — the validation
logic and policy are identical by design, verified by an equivalent test
suite.
"""

import re
import uuid
from dataclasses import dataclass

EVIDENCE_KINDS = ("target_source", "caller", "callee", "existing_test", "configuration")

# Case-insensitive match, case-sensitive validation — identical two-step
# distinction to app/rag/citations.py and app/impact/evidence.py: flag a
# lowercase "[t1]" as an attempted-but-unknown citation rather than
# silently ignoring it as plain text, while never treating it as
# equivalent to a real "T1".
_CITATION_PATTERN = re.compile(r"\[(T\d+)\]", re.IGNORECASE)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str  # "T1", "T2", ... — assigned once, deterministically, by context.py
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    file_path: str
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    line_start: int | None
    line_end: int | None
    kind: str  # one of EVIDENCE_KINDS
    content: str | None  # None when source genuinely isn't available — never fabricated


@dataclass(frozen=True)
class EvidenceSet:
    items: tuple[Evidence, ...]

    def by_id(self) -> dict[str, Evidence]:
        return {e.evidence_id: e for e in self.items}

    def valid_ids(self) -> frozenset[str]:
        return frozenset(e.evidence_id for e in self.items)


def extract_cited_ids(text: str) -> list[str]:
    return _CITATION_PATTERN.findall(text)


def validate_citations(cited_ids: list[str], evidence: EvidenceSet) -> tuple[list[str], list[str]]:
    """Splits into (valid, invalid) against the real evidence set. Never
    raises — an invalid id is data to sanitize, never a reason to fail the
    whole response."""
    valid_ids = evidence.valid_ids()
    valid = [cid for cid in cited_ids if cid in valid_ids]
    invalid = [cid for cid in cited_ids if cid not in valid_ids]
    return valid, invalid


def sanitize_citations(text: str, evidence: EvidenceSet) -> str:
    """Replaces every citation marker not present in `evidence` with
    "[unverified]", leaving valid citations untouched."""
    valid_ids = evidence.valid_ids()

    def _replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in valid_ids else "[unverified]"

    return _CITATION_PATTERN.sub(_replace, text)
