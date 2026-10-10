"""Evidence objects and citation validation — pure, no DB, no AI provider.
See docs/architecture/impact-intelligence.md sections 2 and 7.

Evidence IDs are assigned once, deterministically, by
app/impact/context.py as evidence is assembled. This module never invents
one — it only ever checks a model's claimed citations against a closed set
that was built before the model was ever called.
"""

import re
import uuid
from dataclasses import dataclass

EVIDENCE_TYPES = (
    "target_source",
    "direct_dependent",
    "transitive_dependent",
    "impact_path",
    "file_context",
)

# The PATTERN match is case-insensitive, but the captured value is NEVER
# case-normalized before validation (Task 7 of the adversarial-hardening
# pass). These are two different decisions:
#   1. Recognizing "[e2]" (lowercase) as *an attempted citation at all* —
#      so it gets flagged and sanitized to "[unverified]" rather than
#      silently left as ambiguous literal text (an unverified-looking
#      citation is more honest than a silently-ignored one).
#   2. NEVER treating "[e2]" as equivalent to a real "E2" — validation
#      compares the captured string exactly, case-sensitive, against the
#      real (always-uppercase, by construction in context.py) ID set. This
#      is deliberate: Task 7 prefers "unknown citation -> explicit removal"
#      over "silently mapping it to another evidence item" — treating
#      "e2" as "really meant E2" would be exactly that kind of silent
#      remapping. A case mismatch is always invalid, never auto-corrected.
# No other leniency is added — "[ E1 ]" (internal whitespace), "[E-1]"
# (non-digit characters), and "[E1 ]" never match at all, by design; they
# are not citation-shaped enough to be worth flagging as unverified rather
# than simply left as literal text.
_CITATION_PATTERN = re.compile(r"\[(E\d+)\]", re.IGNORECASE)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str  # "E1", "E2", ... — assigned by context.py, stable within one package
    type: str
    symbol_id: uuid.UUID | None
    file_path: str
    line_start: int | None
    line_end: int | None
    relationship_type: str | None
    depth: int | None
    content: str | None  # None when source genuinely isn't available — never fabricated


@dataclass(frozen=True)
class EvidenceSet:
    items: tuple[Evidence, ...]

    def by_id(self) -> dict[str, Evidence]:
        return {e.evidence_id: e for e in self.items}

    def valid_ids(self) -> frozenset[str]:
        return frozenset(e.evidence_id for e in self.items)


def extract_cited_ids(text: str) -> list[str]:
    """Every `[E<n>]`-shaped marker appearing in free text, in order of
    appearance, duplicates included (sanitization — not this function —
    decides what to do with an unknown one)."""
    return _CITATION_PATTERN.findall(text)


def validate_citations(cited_ids: list[str], evidence: EvidenceSet) -> tuple[list[str], list[str]]:
    """Splits `cited_ids` into (valid, invalid) against the real evidence
    set. Never raises — an invalid id is data to sanitize, not a reason to
    fail the whole response (impact-intelligence.md section 7)."""
    valid_ids = evidence.valid_ids()
    valid = [cid for cid in cited_ids if cid in valid_ids]
    invalid = [cid for cid in cited_ids if cid not in valid_ids]
    return valid, invalid


def sanitize_citations(text: str, evidence: EvidenceSet) -> str:
    """Replaces every citation marker not present in `evidence` with
    "[unverified]", leaving valid citations untouched. Never exposes a
    fabricated id to a caller as if it resolved to something real."""
    valid_ids = evidence.valid_ids()

    def _replace(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in valid_ids else "[unverified]"

    return _CITATION_PATTERN.sub(_replace, text)
