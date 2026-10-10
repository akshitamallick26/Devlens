"""Semgrep integration. Invocation (`run_semgrep`) and output normalization
(`parse_semgrep_output`) are deliberately separate so parsing is fully
unit-testable without the real binary — `semgrep` is not installed in this
development sandbox (confirmed via `shutil.which`), so this parser is built
against Semgrep's documented `--json` output schema
(`results[].{check_id,path,start,end,extra.{message,severity,metadata,
fingerprint}}`, top-level `version`), not verified against live tool
output. Stated limitation, not hidden — see the final report.

Rule configuration is explicit and versioned, never a runtime download of
arbitrary rules: this module only ever invokes Semgrep with a fixed,
repository-local rule pack path (`app/security/rules/python-security.yml`),
never `--config=auto` or a remote registry reference.

Safety properties mirror app/security/osv.py exactly: explicit argv list,
never `shell=True`, minimal environment, bounded timeout, bounded stdout.
Semgrep only *reads* source files to match patterns against their syntax
tree — it never executes the target application.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.security.identity import semgrep_fingerprint
from app.security.models import NormalizedFinding, NormalizedObservation, ScannerRunResult

SCANNER_NAME = "semgrep"
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_OUTPUT_BYTES = 5_000_000

RULE_PACK_PATH = Path(__file__).parent / "rules" / "python-security.yml"

# Semgrep's own three-level severity -> this project's existing SEVERITIES
# vocabulary, the same lossless, documented normalization app/security/
# osv.py applies to GHSA severity strings — not an invented judgment.
_SEVERITY_MAP = {"ERROR": "high", "WARNING": "medium", "INFO": "info"}


def is_available() -> bool:
    return shutil.which("semgrep") is not None


def _scanner_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}


def get_version(timeout: float = 10.0) -> str | None:
    if not is_available():
        return None
    try:
        result = subprocess.run(
            ["semgrep", "--version"],
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


def run_semgrep(
    scan_dir: Path,
    *,
    rule_pack_path: Path = RULE_PACK_PATH,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ScannerRunResult:
    if not is_available():
        return ScannerRunResult(
            scanner=SCANNER_NAME, status="unavailable", detail="semgrep binary not found on PATH"
        )
    if not rule_pack_path.is_file():
        return ScannerRunResult(
            scanner=SCANNER_NAME,
            status="error",
            detail=f"local rule pack not found: {rule_pack_path}",
        )

    command = [
        "semgrep",
        "scan",
        "--config",
        str(rule_pack_path),
        "--json",
        "--quiet",
        "--no-git-ignore",
        "--disable-version-check",
        "--metrics",
        "off",
        str(scan_dir),
    ]
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

    # Semgrep's documented exit codes: 0 = clean, 1 = findings present (not
    # a failure), 2 = findings present with some rule/parse errors (still
    # not a hard failure — partial results are real results). Anything
    # else is a genuine tool failure.
    if result.returncode not in (0, 1, 2):
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

    return parse_semgrep_output(payload)


def _references(metadata: dict[str, Any]) -> tuple[str, ...]:
    refs = metadata.get("references")
    if not isinstance(refs, list):
        return ()
    return tuple(r for r in refs if isinstance(r, str))


def parse_semgrep_output(payload: Any) -> ScannerRunResult:
    """Pure parsing — defensive against any unexpected shape, same
    discipline as app/security/osv.py::parse_osv_output. One finding per
    distinct `check_id` (Semgrep's rule identity is global, not per-file);
    one observation per `results[]` entry (each is a specific file/line
    occurrence)."""
    if not isinstance(payload, dict):
        return ScannerRunResult(
            scanner=SCANNER_NAME, status="malformed_output", detail="payload is not a JSON object"
        )
    results = payload.get("results", [])
    if not isinstance(results, list):
        return ScannerRunResult(
            scanner=SCANNER_NAME, status="malformed_output", detail="'results' is not a list"
        )

    errors = payload.get("errors")
    error_count = len(errors) if isinstance(errors, list) else 0

    findings: dict[str, NormalizedFinding] = {}
    observations: list[NormalizedObservation] = []
    seen_observation_keys: set[tuple[str, str | None, int | None]] = set()

    for entry in results:
        if not isinstance(entry, dict):
            continue
        check_id = entry.get("check_id")
        path = entry.get("path")
        start = entry.get("start")
        end = entry.get("end")
        extra = entry.get("extra")
        if not isinstance(check_id, str) or not isinstance(path, str):
            continue
        start_line = start.get("line") if isinstance(start, dict) else None
        end_line = end.get("line") if isinstance(end, dict) else None
        if not isinstance(start_line, int):
            continue
        extra = extra if isinstance(extra, dict) else {}
        message = extra.get("message")
        severity_raw = extra.get("severity")
        metadata = extra.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        scanner_fingerprint = extra.get("fingerprint")

        fingerprint = semgrep_fingerprint(
            check_id,
            path,
            start_line,
            scanner_fingerprint if isinstance(scanner_fingerprint, str) else None,
        )
        if fingerprint not in findings:
            message_text = message if isinstance(message, str) else ""
            title = message_text.splitlines()[0] if message_text.strip() else check_id
            severity = (
                _SEVERITY_MAP.get(severity_raw.upper()) if isinstance(severity_raw, str) else None
            )
            confidence = metadata.get("confidence")
            findings[fingerprint] = NormalizedFinding(
                analyzer=SCANNER_NAME,
                rule_id=check_id,
                fingerprint=fingerprint,
                severity=severity,
                title=title,
                message=message_text,
                cvss_score=None,  # Semgrep does not supply CVSS data
                cvss_vector=None,
                scanner_confidence=confidence if isinstance(confidence, str) else None,
                references=_references(metadata),
            )

        obs_key = (fingerprint, path, start_line)
        if obs_key in seen_observation_keys:
            continue
        seen_observation_keys.add(obs_key)
        observations.append(
            NormalizedObservation(
                fingerprint=fingerprint,
                file_path=path,
                start_line=start_line,
                end_line=end_line if isinstance(end_line, int) else None,
                affected_package=None,
                affected_ecosystem=None,
                installed_version=None,
                affected_range=None,
                fixed_version=None,
                raw_finding=entry,
            )
        )

    detail = f"{error_count} scanner-reported parse/rule error(s)" if error_count else None
    return ScannerRunResult(
        scanner=SCANNER_NAME,
        status="completed",
        findings=tuple(findings.values()),
        observations=tuple(observations),
        detail=detail,
    )
