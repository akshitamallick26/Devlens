"""OSV-Scanner integration. Invocation (`run_osv_scanner`) and output
normalization (`parse_osv_output`) are deliberately separate functions so
parsing/normalization is fully unit-testable without the real binary —
neither `osv-scanner` nor `semgrep` is installed in this development
sandbox (confirmed via `shutil.which` before writing this module), so this
parser is built against OSV-Scanner's documented JSON output schema
(`results[].source.path`, `.packages[].package.{name,version,ecosystem}`,
`.vulnerabilities[].{id,summary,details,severity[],references,
database_specific.severity}`, `.groups[].fixedVersion`), not verified
against live tool output. This is a stated limitation, not hidden — see
the final report.

Safety properties, mirroring app/ingestion/git_adapter.py and
app/testing/sandbox.py exactly:
- `osv-scanner` is invoked as an explicit argv list, never `shell=True`.
- A minimal, credential-free environment.
- An explicit, bounded timeout.
- stdout is bounded before being parsed.
- Only the materialized manifest directory (app/security/manifests.py) is
  ever passed as a scan target — never an arbitrary user-supplied path.
- No lockfile resolution/installation ever happens: OSV-Scanner reads
  lockfiles/manifests as data; it does not run `pip install` or any
  package manager.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.security.identity import osv_fingerprint
from app.security.models import NormalizedFinding, NormalizedObservation, ScannerRunResult

SCANNER_NAME = "osv-scanner"
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_BYTES = 5_000_000

# OSV/GHSA-style severity strings -> this project's existing SEVERITIES
# vocabulary (app/models/security.py, unchanged since Phase 0). A plain
# vocabulary/casing normalization against an already-established enum,
# not an invented judgment — an unrecognized string maps to None (unknown)
# rather than being guessed into one of these buckets.
_SEVERITY_MAP = {
    "CRITICAL": "critical",
    "HIGH": "high",
    "MODERATE": "medium",
    "MEDIUM": "medium",
    "LOW": "low",
}


def is_available() -> bool:
    return shutil.which("osv-scanner") is not None


def _scanner_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}


def get_version(timeout: float = 10.0) -> str | None:
    if not is_available():
        return None
    try:
        result = subprocess.run(
            ["osv-scanner", "--version"],
            env=_scanner_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout.strip() or result.stderr.strip()) or None


def run_osv_scanner(
    scan_dir: Path, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> ScannerRunResult:
    if not is_available():
        return ScannerRunResult(
            scanner=SCANNER_NAME,
            status="unavailable",
            detail="osv-scanner binary not found on PATH",
        )

    command = ["osv-scanner", "--format", "json", "--recursive", str(scan_dir)]
    try:
        result = subprocess.run(  # noqa: S603 - argv list, shell=False, see module docstring
            command,
            cwd=scan_dir,
            env=_scanner_env(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ScannerRunResult(
            scanner=SCANNER_NAME, status="timeout", detail=f"exceeded {timeout_seconds}s"
        )
    except OSError as exc:
        return ScannerRunResult(scanner=SCANNER_NAME, status="error", detail=str(exc))

    # osv-scanner's documented exit codes: 0 = clean, 1 = vulnerabilities
    # found (not a failure!), anything else = a real tool failure.
    if result.returncode not in (0, 1):
        return ScannerRunResult(
            scanner=SCANNER_NAME,
            status="error",
            detail=f"exit code {result.returncode}: {result.stderr[:2000]}",
        )

    stdout = result.stdout[:MAX_OUTPUT_BYTES]
    if not stdout.strip():
        return ScannerRunResult(scanner=SCANNER_NAME, status="completed")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return ScannerRunResult(scanner=SCANNER_NAME, status="malformed_output", detail=str(exc))

    return parse_osv_output(payload)


def _severity_from_vuln(vuln: dict[str, Any]) -> str | None:
    database_specific = vuln.get("database_specific")
    if isinstance(database_specific, dict):
        raw = database_specific.get("severity")
        if isinstance(raw, str) and raw.upper() in _SEVERITY_MAP:
            return _SEVERITY_MAP[raw.upper()]
    return None


def _cvss(vuln: dict[str, Any]) -> tuple[float | None, str | None]:
    """Returns (score, vector). OSV's `severity[]` entries carry a CVSS
    *vector* string (e.g. "CVSS:3.1/AV:N/..."), not a precomputed numeric
    base score — this function never computes a CVSS base score from a
    vector itself (that would be a derived/invented number this scanner
    did not directly supply); `score` is only populated on the rare
    occasion the field is already a bare number."""
    entries = vuln.get("severity")
    if not isinstance(entries, list):
        return None, None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_score = entry.get("score")
        if not isinstance(raw_score, str):
            continue
        if raw_score.upper().startswith("CVSS:"):
            return None, raw_score
        try:
            return float(raw_score), None
        except ValueError:
            continue
    return None, None


def _references(vuln: dict[str, Any]) -> tuple[str, ...]:
    refs = vuln.get("references")
    if not isinstance(refs, list):
        return ()
    urls = [r["url"] for r in refs if isinstance(r, dict) and isinstance(r.get("url"), str)]
    return tuple(urls)


def _fixed_versions_by_vuln_id(groups: Any) -> dict[str, str]:
    if not isinstance(groups, list):
        return {}
    fixed_by_id: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        fixed_version = group.get("fixedVersion")
        ids = group.get("ids")
        if not fixed_version or not isinstance(ids, list):
            continue
        for vuln_id in ids:
            if isinstance(vuln_id, str):
                fixed_by_id[vuln_id] = fixed_version
    return fixed_by_id


def _affected_range(vuln: dict[str, Any]) -> str | None:
    affected = vuln.get("affected")
    if not isinstance(affected, list):
        return None
    for entry in affected:
        if not isinstance(entry, dict):
            continue
        ranges = entry.get("ranges")
        if not isinstance(ranges, list):
            continue
        for range_entry in ranges:
            if not isinstance(range_entry, dict):
                continue
            events = range_entry.get("events")
            if not isinstance(events, list):
                continue
            parts = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                for key in ("introduced", "fixed", "last_affected"):
                    if key in event:
                        parts.append(f"{key}:{event[key]}")
            if parts:
                return ", ".join(parts)
    return None


def parse_osv_output(payload: Any) -> ScannerRunResult:
    """Pure parsing — defensive against any unexpected shape. An
    unrecognized top-level shape yields `malformed_output`; an individual
    malformed entry inside an otherwise-valid payload is skipped rather
    than aborting the whole parse (OSV-Scanner's own schema has had minor
    version-to-version drift, and this must degrade gracefully, never
    crash and never fabricate)."""
    if not isinstance(payload, dict):
        return ScannerRunResult(
            scanner=SCANNER_NAME, status="malformed_output", detail="payload is not a JSON object"
        )
    results = payload.get("results", [])
    if not isinstance(results, list):
        return ScannerRunResult(
            scanner=SCANNER_NAME, status="malformed_output", detail="'results' is not a list"
        )

    findings: dict[str, NormalizedFinding] = {}
    observations: list[NormalizedObservation] = []
    seen_observation_keys: set[tuple[str, str | None, str | None, str | None]] = set()

    for result in results:
        if not isinstance(result, dict):
            continue
        source = result.get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        packages = result.get("packages", [])
        if not isinstance(packages, list):
            continue
        for pkg_entry in packages:
            if not isinstance(pkg_entry, dict):
                continue
            package = pkg_entry.get("package")
            if not isinstance(package, dict):
                continue
            package_name = package.get("name")
            ecosystem = package.get("ecosystem")
            installed_version = package.get("version")
            if not isinstance(package_name, str) or not isinstance(ecosystem, str):
                continue
            vulns = pkg_entry.get("vulnerabilities", [])
            if not isinstance(vulns, list):
                continue
            fixed_by_id = _fixed_versions_by_vuln_id(pkg_entry.get("groups"))

            for vuln in vulns:
                if not isinstance(vuln, dict):
                    continue
                advisory_id = vuln.get("id")
                if not isinstance(advisory_id, str):
                    continue
                fingerprint = osv_fingerprint(ecosystem, package_name, advisory_id)
                if fingerprint not in findings:
                    score, vector = _cvss(vuln)
                    summary = vuln.get("summary")
                    details = vuln.get("details")
                    findings[fingerprint] = NormalizedFinding(
                        analyzer=SCANNER_NAME,
                        rule_id=advisory_id,
                        fingerprint=fingerprint,
                        severity=_severity_from_vuln(vuln),
                        title=summary if isinstance(summary, str) else advisory_id,
                        message=(
                            details
                            if isinstance(details, str)
                            else (summary if isinstance(summary, str) else "")
                        ),
                        cvss_score=score,
                        cvss_vector=vector,
                        scanner_confidence=None,
                        references=_references(vuln),
                    )

                obs_key = (fingerprint, source_path, package_name, installed_version)
                if obs_key in seen_observation_keys:
                    continue  # same scanner run reporting the identical occurrence twice
                seen_observation_keys.add(obs_key)
                observations.append(
                    NormalizedObservation(
                        fingerprint=fingerprint,
                        file_path=source_path if isinstance(source_path, str) else None,
                        start_line=None,
                        end_line=None,
                        affected_package=package_name,
                        affected_ecosystem=ecosystem,
                        installed_version=installed_version
                        if isinstance(installed_version, str)
                        else None,
                        affected_range=_affected_range(vuln),
                        fixed_version=fixed_by_id.get(advisory_id),
                        raw_finding=vuln,
                    )
                )

    return ScannerRunResult(
        scanner=SCANNER_NAME,
        status="completed",
        findings=tuple(findings.values()),
        observations=tuple(observations),
    )
