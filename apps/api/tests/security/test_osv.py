"""OSV-Scanner integration tests. `osv-scanner` is not installed in this
sandbox (confirmed via `shutil.which`), so invocation is tested via
monkeypatched `subprocess.run`/`shutil.which`, and normalization is tested
directly against hand-built realistic fixture JSON. No real network/binary
call is ever made.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.security import osv


def _clean_payload() -> dict[str, Any]:
    return {"results": []}


def _vulnerable_payload(advisory_id: str = "GHSA-h5c8-rqwp-cp95") -> dict[str, Any]:
    return {
        "results": [
            {
                "source": {"path": "requirements.txt", "type": "lockfile"},
                "packages": [
                    {
                        "package": {"name": "jinja2", "version": "2.11.2", "ecosystem": "PyPI"},
                        "vulnerabilities": [
                            {
                                "id": advisory_id,
                                "summary": "Jinja2 vulnerable to XSS",
                                "details": "Full details.",
                                "severity": [
                                    {
                                        "type": "CVSS_V3",
                                        "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N",
                                    }
                                ],
                                "database_specific": {"severity": "MODERATE"},
                                "references": [
                                    {"type": "ADVISORY", "url": "https://example.com/advisory"}
                                ],
                                "affected": [
                                    {
                                        "ranges": [
                                            {
                                                "type": "ECOSYSTEM",
                                                "events": [
                                                    {"introduced": "0"},
                                                    {"fixed": "2.11.3"},
                                                ],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ],
                        "groups": [{"ids": [advisory_id], "fixedVersion": "2.11.3"}],
                    }
                ],
            }
        ]
    }


# ---- normalization (pure) ----------------------------------------------------


def test_clean_dependency_produces_no_findings() -> None:
    result = osv.parse_osv_output(_clean_payload())
    assert result.status == "completed"
    assert result.findings == ()
    assert result.observations == ()


def test_vulnerable_dependency_is_normalized() -> None:
    result = osv.parse_osv_output(_vulnerable_payload())
    assert result.status == "completed"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.analyzer == "osv-scanner"
    assert finding.rule_id == "GHSA-h5c8-rqwp-cp95"
    assert finding.severity == "medium"  # MODERATE -> medium, documented normalization
    assert finding.cvss_vector == "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:L/A:N"
    assert finding.cvss_score is None  # never computed from the vector
    assert finding.references == ("https://example.com/advisory",)

    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.affected_package == "jinja2"
    assert observation.installed_version == "2.11.2"
    assert observation.fixed_version == "2.11.3"
    assert observation.file_path == "requirements.txt"


def test_same_package_multiple_advisories_produces_multiple_findings() -> None:
    payload = _vulnerable_payload("GHSA-first")
    payload["results"][0]["packages"][0]["vulnerabilities"].append(
        {"id": "GHSA-second", "summary": "Another issue", "details": "x"}
    )
    result = osv.parse_osv_output(payload)
    assert len(result.findings) == 2
    assert {f.rule_id for f in result.findings} == {"GHSA-first", "GHSA-second"}
    assert len(result.observations) == 2


def test_deterministic_normalization_is_order_independent_in_output() -> None:
    first = osv.parse_osv_output(_vulnerable_payload())
    second = osv.parse_osv_output(_vulnerable_payload())
    assert first.findings == second.findings
    assert first.observations == second.observations


def test_duplicate_occurrence_in_one_run_is_deduplicated() -> None:
    """OSV can report the same package/advisory/source combination twice
    (e.g. via different resolution paths) — this must not become two
    identical observation rows from one parse."""
    payload = _vulnerable_payload()
    payload["results"].append(payload["results"][0])  # exact duplicate block
    result = osv.parse_osv_output(payload)
    assert len(result.findings) == 1
    assert len(result.observations) == 1


def test_unsupported_manifest_path_is_not_recognized() -> None:
    from app.security.manifests import is_supported_manifest_path

    assert not is_supported_manifest_path("package.json")
    assert not is_supported_manifest_path("Gemfile.lock")
    assert is_supported_manifest_path("requirements.txt")
    assert is_supported_manifest_path("requirements/dev.txt")
    assert is_supported_manifest_path("nested/dir/pyproject.toml")


def test_malformed_output_not_a_dict_is_reported_honestly() -> None:
    result = osv.parse_osv_output(["not", "a", "dict"])
    assert result.status == "malformed_output"
    assert result.findings == ()


def test_malformed_output_results_not_a_list() -> None:
    result = osv.parse_osv_output({"results": "oops"})
    assert result.status == "malformed_output"


def test_malformed_entries_are_skipped_not_crashed_on() -> None:
    payload = {
        "results": [
            "not-a-dict",
            {"source": {"path": "x"}, "packages": "not-a-list"},
            {"packages": [{"package": "not-a-dict"}]},
            {
                "packages": [
                    {"package": {"name": "ok", "ecosystem": "PyPI"}, "vulnerabilities": "bad"}
                ]
            },
        ]
    }
    result = osv.parse_osv_output(payload)
    assert result.status == "completed"
    assert result.findings == ()


# ---- invocation (subprocess, monkeypatched) ------------------------------------


def test_unavailable_when_binary_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = osv.run_osv_scanner(Path("/tmp/does-not-matter"))
    assert result.status == "unavailable"
    assert result.findings == ()


def test_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")

    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="osv-scanner", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _raise)
    result = osv.run_osv_scanner(tmp_path, timeout_seconds=1.0)
    assert result.status == "timeout"


def test_nonzero_nonone_exit_code_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["osv-scanner"], returncode=127, stdout="", stderr="not found"
        ),
    )
    result = osv.run_osv_scanner(tmp_path)
    assert result.status == "error"


def test_exit_code_one_with_findings_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """osv-scanner's documented convention: exit 1 means vulnerabilities
    were found, not that the tool failed."""
    import json

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["osv-scanner"], returncode=1, stdout=json.dumps(_clean_payload()), stderr=""
        ),
    )
    result = osv.run_osv_scanner(tmp_path)
    assert result.status == "completed"


def test_malformed_json_stdout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["osv-scanner"], returncode=0, stdout="{not valid json", stderr=""
        ),
    )
    result = osv.run_osv_scanner(tmp_path)
    assert result.status == "malformed_output"


def test_invocation_never_uses_shell_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=["osv-scanner"], returncode=0, stdout="{}", stderr=""
        )

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")
    monkeypatch.setattr(subprocess, "run", _spy)
    osv.run_osv_scanner(tmp_path)
    assert captured.get("shell") is False
    assert isinstance(captured.get("timeout"), float)
