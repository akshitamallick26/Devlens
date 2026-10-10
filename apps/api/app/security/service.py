"""Security analysis orchestration: `run_security_analysis`. Pipeline —

    manifest/source discovery -> scanner invocation (OSV-Scanner, Semgrep)
    -> normalization -> finding/observation persistence
    -> graph resolution -> blast radius -> optional AI explanation

Task 17's core discipline: each scanner's failure is isolated. One scanner
being unavailable, timing out, or returning malformed output never erases
another scanner's successful results, and never gets silently reported as
"no findings" — see `ScannerStatusSummary`, always present per attempted
scanner with its own honest status.
"""

import logging
import platform
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import AIProvider
from app.models import (
    AnalysisRun,
    RepositoryRevision,
    SecurityFinding,
    SecurityFindingObservation,
    SourceBlob,
    SourceFile,
    Symbol,
)
from app.rag.persistence import load_chunk_texts
from app.security import graph_resolution, manifests, osv, persistence, semgrep
from app.security.blast_radius import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_EVIDENCE_PATHS,
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_SYMBOLS,
    compute_security_blast_radius,
)
from app.security.context import assemble_security_context
from app.security.explanation import SecurityExplanation, generate_security_explanation
from app.security.materialize import MaterializedFile, materialize_files
from app.security.models import (
    BlastRadiusResult,
    FindingView,
    GraphResolution,
    NormalizedObservation,
    ObservationView,
    ScannerRunResult,
)

logger = logging.getLogger("devlens.security")

ANALYZER_NAME = "devlens-security-analysis"
ALL_SCANNERS = ("osv-scanner", "semgrep")
MAX_FINDINGS_PER_RESPONSE = 100


class SecurityAnalysisError(Exception):
    pass


class RevisionNotFoundError(SecurityAnalysisError):
    pass


@dataclass(frozen=True)
class ScannerStatusSummary:
    scanner: str
    status: str
    detail: str | None
    finding_count: int
    observation_count: int
    scanner_version: str | None


@dataclass(frozen=True)
class FindingResult:
    finding: FindingView
    observations: tuple[ObservationView, ...]
    blast_radius: BlastRadiusResult | None
    explanation: SecurityExplanation | None


@dataclass(frozen=True)
class SecurityAnalysisResult:
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    analysis_run_id: uuid.UUID | None
    analysis_status: str  # "completed", or "failed" when no scanner completed
    scanner_results: tuple[ScannerStatusSummary, ...]
    findings: tuple[FindingResult, ...]
    limitations: tuple[str, ...]


def _analyzer_version() -> str:
    return f"security-analysis@{platform.python_version()}"


def _load_repository_files(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, *, python_only: bool
) -> list[MaterializedFile]:
    query = (
        select(SourceFile.id, SourceFile.path, SourceBlob.inline_content)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
    )
    if python_only:
        query = query.where(SourceFile.language == "python")
    rows = db.execute(query).all()
    return [
        MaterializedFile(file_id=file_id, path=path, content=content)
        for file_id, path, content in rows
        if content is not None
    ]


def _run_dependency_scan(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, timeout_seconds: float
) -> ScannerRunResult:
    all_files = _load_repository_files(db, repository_id, revision_id, python_only=False)
    manifest_files = [f for f in all_files if manifests.is_supported_manifest_path(f.path)]
    if not manifest_files:
        return ScannerRunResult(
            scanner=osv.SCANNER_NAME,
            status="completed",
            detail="no supported dependency manifest found in this revision",
        )
    with materialize_files(manifest_files) as scan_dir:
        return osv.run_osv_scanner(scan_dir, timeout_seconds=timeout_seconds)


def _run_static_scan(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, timeout_seconds: float
) -> ScannerRunResult:
    source_files = _load_repository_files(db, repository_id, revision_id, python_only=True)
    if not source_files:
        return ScannerRunResult(
            scanner=semgrep.SCANNER_NAME,
            status="completed",
            detail="no Python source files found in this revision",
        )
    with materialize_files(source_files) as scan_dir:
        return semgrep.run_semgrep(scan_dir, timeout_seconds=timeout_seconds)


def _resolve_observation(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    observation: NormalizedObservation,
) -> GraphResolution:
    if observation.affected_package is not None:
        candidates = graph_resolution.resolve_dependency_finding(
            db,
            repository_id,
            revision_id,
            observation.affected_package,
            observation.affected_ecosystem or "",
        )
        return graph_resolution.fold_dependency_resolutions(candidates)
    if observation.file_path is not None and observation.start_line is not None:
        return graph_resolution.resolve_source_finding(
            db, repository_id, revision_id, observation.file_path, observation.start_line
        )
    return GraphResolution(status="unresolved")


def _finding_view(row: SecurityFinding) -> FindingView:
    return FindingView(
        id=row.id,
        analyzer=row.analyzer,
        rule_id=row.rule_id,
        severity=row.severity,
        title=row.title,
        message=row.message,
        status=row.status,
        cvss_score=row.cvss_score,
        cvss_vector=row.cvss_vector,
        scanner_confidence=row.scanner_confidence,
        references=tuple(row.references) if row.references else (),
    )


def _observation_view(row: SecurityFindingObservation) -> ObservationView:
    return ObservationView(
        id=row.id,
        finding_id=row.finding_id,
        file_path=None,  # filled in by the caller, which has the SourceFile join
        start_line=row.start_line,
        end_line=row.end_line,
        affected_package=row.affected_package,
        installed_version=row.installed_version,
        affected_range=row.affected_range,
        fixed_version=row.fixed_version,
        resolution_status=row.resolution_status,
        symbol_id=row.symbol_id,
    )


def _load_findings_for_run(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> list[tuple[FindingView, tuple[ObservationView, ...]]]:
    """Loads exactly the findings observed by *this* analysis run — not
    every finding ever recorded for the repository (which would include
    findings from unrelated past scans / other revisions)."""
    observation_rows = db.execute(
        select(SecurityFindingObservation, SourceFile.path)
        .outerjoin(SourceFile, SecurityFindingObservation.file_id == SourceFile.id)
        .where(SecurityFindingObservation.repository_id == repository_id)
        .where(SecurityFindingObservation.revision_id == revision_id)
        .where(SecurityFindingObservation.analysis_run_id == analysis_run_id)
    ).all()
    observations_by_finding: dict[uuid.UUID, list[ObservationView]] = {}
    for obs_row, path in observation_rows:
        view = _observation_view(obs_row)
        view = ObservationView(
            id=view.id,
            finding_id=view.finding_id,
            file_path=path,
            start_line=view.start_line,
            end_line=view.end_line,
            affected_package=view.affected_package,
            installed_version=view.installed_version,
            affected_range=view.affected_range,
            fixed_version=view.fixed_version,
            resolution_status=view.resolution_status,
            symbol_id=view.symbol_id,
        )
        observations_by_finding.setdefault(obs_row.finding_id, []).append(view)

    if not observations_by_finding:
        return []

    finding_rows = (
        db.execute(
            select(SecurityFinding)
            .where(SecurityFinding.repository_id == repository_id)
            .where(SecurityFinding.id.in_(observations_by_finding))
        )
        .scalars()
        .all()
    )
    return [(_finding_view(row), tuple(observations_by_finding[row.id])) for row in finding_rows]


def _affected_source(
    db: Session, revision_id: uuid.UUID, observations: tuple[ObservationView, ...]
) -> dict[uuid.UUID, str | None]:
    requests = []
    index_to_obs_id = []
    for obs in observations:
        if obs.file_path is not None and obs.start_line is not None:
            file_row = db.execute(
                select(SourceFile.id)
                .where(SourceFile.revision_id == revision_id)
                .where(SourceFile.path == obs.file_path)
            ).first()
            if file_row is None:
                continue
            requests.append((file_row[0], obs.start_line, obs.end_line or obs.start_line))
            index_to_obs_id.append(obs.id)
    if not requests:
        return {}
    texts = load_chunk_texts(db, revision_id, requests)
    return dict(zip(index_to_obs_id, texts, strict=True))


def _symbol_names(
    db: Session, revision_id: uuid.UUID, symbol_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not symbol_ids:
        return {}
    rows = db.execute(
        select(Symbol.id, Symbol.qualified_name)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.id.in_(symbol_ids))
    ).all()
    return dict(rows)


def _summarize(result: ScannerRunResult) -> ScannerStatusSummary:
    return ScannerStatusSummary(
        scanner=result.scanner,
        status=result.status,
        detail=result.detail,
        finding_count=len(result.findings),
        observation_count=len(result.observations),
        scanner_version=result.scanner_version,
    )


def run_security_analysis(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    scanners: tuple[str, ...] = ALL_SCANNERS,
    llm_provider: AIProvider | None = None,
    include_explanation: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    max_files: int = DEFAULT_MAX_FILES,
    max_evidence_paths: int = DEFAULT_MAX_EVIDENCE_PATHS,
    osv_timeout_seconds: float = osv.DEFAULT_TIMEOUT_SECONDS,
    semgrep_timeout_seconds: float = semgrep.DEFAULT_TIMEOUT_SECONDS,
) -> SecurityAnalysisResult:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="security_scan",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
        config={"scanners": list(scanners)},
    )
    db.add(run)
    db.commit()

    try:
        scanner_results: list[ScannerRunResult] = []
        if "osv-scanner" in scanners:
            scanner_results.append(
                _run_dependency_scan(db, repository_id, revision_id, osv_timeout_seconds)
            )
        if "semgrep" in scanners:
            scanner_results.append(
                _run_static_scan(db, repository_id, revision_id, semgrep_timeout_seconds)
            )

        for scanner_result in scanner_results:
            if scanner_result.status != "completed":
                continue  # Task 17: one scanner's failure never erases another's results
            finding_ids_by_fingerprint = {
                nf.fingerprint: persistence.upsert_finding(db, repository_id, nf, run.id)
                for nf in scanner_result.findings
            }
            for observation in scanner_result.observations:
                finding_id = finding_ids_by_fingerprint.get(observation.fingerprint)
                if finding_id is None:
                    continue  # defensive — every observation's fingerprint has a matching finding
                resolution = _resolve_observation(db, repository_id, revision_id, observation)
                persistence.persist_observation(
                    db, repository_id, revision_id, finding_id, run.id, observation, resolution
                )
        db.flush()

        loaded = _load_findings_for_run(db, repository_id, revision_id, run.id)[
            :MAX_FINDINGS_PER_RESPONSE
        ]
        finding_results = []
        for finding_view, observation_views in loaded:
            seed_symbol_id = next((o.symbol_id for o in observation_views if o.symbol_id), None)
            blast_radius = (
                compute_security_blast_radius(
                    db,
                    repository_id,
                    revision_id,
                    seed_symbol_id,
                    max_depth=max_depth,
                    max_symbols=max_symbols,
                    max_files=max_files,
                    max_evidence_paths=max_evidence_paths,
                )
                if seed_symbol_id is not None
                else None
            )

            explanation = None
            if include_explanation and llm_provider is not None:
                affected_source = _affected_source(db, revision_id, observation_views)
                symbol_ids = {o.symbol_id for o in observation_views if o.symbol_id is not None}
                if blast_radius is not None:
                    symbol_ids |= {blast_radius.seed_symbol_id}
                    symbol_ids |= {s.symbol_id for s in blast_radius.direct_dependents}
                    symbol_ids |= {s.symbol_id for s in blast_radius.transitive_dependents}
                symbol_names = _symbol_names(db, revision_id, symbol_ids)
                context = assemble_security_context(
                    repository_id,
                    revision_id,
                    finding_view,
                    observation_views,
                    affected_source,
                    blast_radius,
                    symbol_names,
                )
                try:
                    explanation = generate_security_explanation(llm_provider, context)
                except Exception as exc:  # noqa: BLE001 - provider call, same seam as every other phase
                    logger.warning("security explanation unavailable: %s", exc)
                    explanation = None

            finding_results.append(
                FindingResult(
                    finding=finding_view,
                    observations=observation_views,
                    blast_radius=blast_radius,
                    explanation=explanation,
                )
            )

        # The run row is the only persisted record of this scan (per-scanner
        # status is not stored), and readers show "latest completed scan" as
        # the revision's findings. A run in which no scanner completed must
        # not read as a clean scan later, so it is recorded as failed.
        if any(r.status == "completed" for r in scanner_results):
            run.status = "completed"
        else:
            run.status = "failed"
            run.error_message = "no scanner completed: " + (
                "; ".join(f"{r.scanner} {r.status}" for r in scanner_results) or "none requested"
            )
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        db.commit()
        raise SecurityAnalysisError(str(exc)) from exc

    scanner_summaries = tuple(_summarize(r) for r in scanner_results)
    limitations = _build_limitations(scanner_results, llm_provider, include_explanation)

    return SecurityAnalysisResult(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_run_id=run.id,
        analysis_status=run.status,
        scanner_results=scanner_summaries,
        findings=tuple(finding_results),
        limitations=limitations,
    )


def _build_limitations(
    scanner_results: list[ScannerRunResult],
    llm_provider: AIProvider | None,
    include_explanation: bool,
) -> tuple[str, ...]:
    limitations = [
        "A finding means a scanner's rule/advisory criteria matched — it does not by itself mean "
        "the issue is exploitable in this application's actual usage.",
        '"No findings" from a scanner means that scanner ran and found nothing, which is a '
        "different state from that scanner being unavailable — see each scanner's own status.",
        "Blast radius reflects the existing deterministic code graph's import/call edges only; "
        "it is a bounded, static approximation of reachability, not a runtime guarantee.",
    ]
    for result in scanner_results:
        if result.status != "completed":
            limitations.append(
                f"{result.scanner}: {result.status} — {result.detail or 'no detail'}"
            )
    if include_explanation and llm_provider is None:
        limitations.append("AI explanation was requested but no provider is configured.")
    return tuple(limitations)
