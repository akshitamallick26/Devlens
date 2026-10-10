"""Deterministic documentation fallback (Task 7). Pure — no DB, no AI
provider, operates only on an already-assembled `DocumentationContextPackage`.
Used whenever no AI provider is configured, or the provider fails for any
reason (see app/documentation/service.py). Produces structured, factual
text built directly from `facts` fields — never invented prose, and never
anything not traceable to a real count/path/name/signature already present
in `facts` or `evidence`.
"""

from app.documentation.models import (
    ArchitectureOverviewFacts,
    DocumentationArtifactDraft,
    DocumentationContextPackage,
    DocumentationSection,
    ModuleFacts,
    RepositoryOverviewFacts,
    SymbolFact,
    SymbolFacts,
)


def _symbol_line(fact: SymbolFact) -> str:
    sig = f" — `{fact.signature}`" if fact.signature else ""
    rel = f" ({fact.relationship_type})" if fact.relationship_type else ""
    location = f"{fact.file_path}:{fact.start_line}"
    return f"- `{fact.qualified_name}` ({fact.kind}) at {location}{sig}{rel}"


def _evidence_ids_for_kind(package: DocumentationContextPackage, *kinds: str) -> tuple[str, ...]:
    return tuple(item.evidence_id for item in package.evidence.items if item.kind in kinds)


def _repository_overview_fallback(
    package: DocumentationContextPackage,
) -> DocumentationArtifactDraft:
    facts = package.facts
    assert isinstance(facts, RepositoryOverviewFacts)

    lines = [
        f"- Default branch: `{facts.default_branch}`",
        f"- Revision: `{facts.revision_commit_sha}`",
        f"- Files: {facts.file_count}",
        f"- Symbols: {facts.symbol_count}",
        f"- Graph relationships: {facts.graph_edge_count}",
    ]
    if facts.files_by_language:
        lines.append("- Languages:")
        lines.extend(f"  - {lang}: {count} file(s)" for lang, count in facts.files_by_language)
    if facts.top_level_modules:
        lines.append("- Top-level modules/packages:")
        lines.extend(f"  - `{m}`" for m in facts.top_level_modules)

    readme_evidence_ids = _evidence_ids_for_kind(package, "existing_documentation")
    sections = [
        DocumentationSection(
            heading="Repository Statistics", content="\n".join(lines), evidence_ids=()
        )
    ]
    if facts.existing_readme_path:
        note = (
            f"An existing `{facts.existing_readme_path}` was found in this revision. "
            "It is treated as evidence, not authority, and may be stale or inaccurate."
        )
        sections.append(
            DocumentationSection(
                heading="Existing Documentation",
                content=note,
                evidence_ids=readme_evidence_ids,
            )
        )

    return DocumentationArtifactDraft(
        title=f"Repository Overview — {facts.repository_name}",
        summary=(
            f"`{facts.repository_name}` at revision `{facts.revision_commit_sha}`: "
            f"{facts.file_count} file(s), {facts.symbol_count} symbol(s), "
            f"{facts.graph_edge_count} graph relationship(s)."
        ),
        sections=tuple(sections),
        evidence_ids=readme_evidence_ids,
    )


def _architecture_overview_fallback(
    package: DocumentationContextPackage,
) -> DocumentationArtifactDraft:
    facts = package.facts
    assert isinstance(facts, ArchitectureOverviewFacts)

    key_symbol_ids = _evidence_ids_for_kind(package, "symbol_source")
    sections = [
        DocumentationSection(
            heading="Module Count",
            content=f"{facts.module_count} module(s) with symbols.",
            evidence_ids=(),
        )
    ]
    if facts.relationship_counts:
        rel_lines = [f"- `{rel}`: {count}" for rel, count in facts.relationship_counts]
        sections.append(
            DocumentationSection(
                heading="Relationship Counts", content="\n".join(rel_lines), evidence_ids=()
            )
        )
    if facts.key_symbols:
        symbol_lines = [_symbol_line(s) for s in facts.key_symbols]
        sections.append(
            DocumentationSection(
                heading="Most-Connected Symbols",
                content="\n".join(symbol_lines),
                evidence_ids=key_symbol_ids,
            )
        )
    readme_evidence_ids = _evidence_ids_for_kind(package, "existing_documentation")
    if facts.existing_readme_path:
        sections.append(
            DocumentationSection(
                heading="Existing Documentation",
                content=(
                    f"An existing `{facts.existing_readme_path}` was found. Treated as "
                    "evidence, not authority — may be stale or inaccurate."
                ),
                evidence_ids=readme_evidence_ids,
            )
        )

    return DocumentationArtifactDraft(
        title="Architecture Overview",
        summary=(
            f"{facts.module_count} module(s), {len(facts.key_symbols)} most-connected "
            f"symbol(s) identified by graph degree."
        ),
        sections=tuple(sections),
        evidence_ids=key_symbol_ids + readme_evidence_ids,
    )


def _module_documentation_fallback(
    package: DocumentationContextPackage,
) -> DocumentationArtifactDraft:
    facts = package.facts
    assert isinstance(facts, ModuleFacts)

    module_evidence_ids = _evidence_ids_for_kind(package, "module_source")
    sections = [
        DocumentationSection(
            heading="Module", content=f"`{facts.file_path}`", evidence_ids=module_evidence_ids
        )
    ]
    if facts.imports:
        sections.append(
            DocumentationSection(
                heading="Imports",
                content="\n".join(f"- `{i}`" for i in facts.imports),
                evidence_ids=(),
            )
        )
    if facts.symbols:
        sections.append(
            DocumentationSection(
                heading="Symbols",
                content="\n".join(_symbol_line(s) for s in facts.symbols),
                evidence_ids=(),
            )
        )
    if facts.outbound_dependencies:
        sections.append(
            DocumentationSection(
                heading="Outbound Dependencies",
                content="\n".join(f"- `{p}`" for p in facts.outbound_dependencies),
                evidence_ids=(),
            )
        )
    if facts.inbound_dependents:
        sections.append(
            DocumentationSection(
                heading="Inbound Dependents",
                content="\n".join(f"- `{p}`" for p in facts.inbound_dependents),
                evidence_ids=(),
            )
        )

    return DocumentationArtifactDraft(
        title=f"Module: {facts.file_path}",
        summary=(
            f"`{facts.file_path}` ({facts.language or 'unknown language'}): "
            f"{len(facts.symbols)} symbol(s), {len(facts.imports)} import(s)."
        ),
        sections=tuple(sections),
        evidence_ids=module_evidence_ids,
    )


def _symbol_documentation_fallback(
    package: DocumentationContextPackage,
) -> DocumentationArtifactDraft:
    facts = package.facts
    assert isinstance(facts, SymbolFacts)

    target_evidence_ids = _evidence_ids_for_kind(package, "symbol_source")
    caller_evidence_ids = _evidence_ids_for_kind(package, "caller")
    callee_evidence_ids = _evidence_ids_for_kind(package, "callee")

    sig = f"`{facts.signature}`" if facts.signature else "(signature unavailable)"
    sections = [
        DocumentationSection(
            heading="Signature",
            content=f"{sig}\n\nLocation: {facts.file_path}:{facts.start_line}-{facts.end_line}",
            evidence_ids=target_evidence_ids,
        )
    ]
    if facts.inherits_from:
        sections.append(
            DocumentationSection(
                heading="Inherits From",
                content="\n".join(f"- `{name}`" for name in facts.inherits_from),
                evidence_ids=(),
            )
        )
    if facts.callers:
        sections.append(
            DocumentationSection(
                heading="Callers",
                content="\n".join(_symbol_line(c) for c in facts.callers),
                evidence_ids=caller_evidence_ids,
            )
        )
    if facts.callees:
        sections.append(
            DocumentationSection(
                heading="Callees",
                content="\n".join(_symbol_line(c) for c in facts.callees),
                evidence_ids=callee_evidence_ids,
            )
        )

    return DocumentationArtifactDraft(
        title=f"Symbol: {facts.qualified_name}",
        summary=(
            f"`{facts.qualified_name}` ({facts.kind}) in {facts.file_path}: "
            f"{len(facts.callers)} caller(s), {len(facts.callees)} callee(s)."
        ),
        sections=tuple(sections),
        evidence_ids=target_evidence_ids + caller_evidence_ids + callee_evidence_ids,
    )


_FALLBACKS = {
    "repository_overview": _repository_overview_fallback,
    "architecture_overview": _architecture_overview_fallback,
    "module_documentation": _module_documentation_fallback,
    "symbol_documentation": _symbol_documentation_fallback,
}


def generate_fallback_draft(package: DocumentationContextPackage) -> DocumentationArtifactDraft:
    return _FALLBACKS[package.artifact_type](package)
