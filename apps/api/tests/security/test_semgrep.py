"""Semgrep integration tests. `semgrep` is not installed in this sandbox
(confirmed via `shutil.which`), so invocation is tested via monkeypatched
`subprocess.run`/`shutil.which`, and normalization is tested directly
against hand-built realistic fixture JSON. No real network/binary call is
ever made.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.security import semgrep


def _finding_entry(
    check_id: str = "devlens-python-eval-exec",
    path: str = "app/foo.py",
    line: int = 10,
    severity: str = "ERROR",
    fingerprint: str | None = "abc123",
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "path": path,
        "start": {"line": line, "col": 5},
        "end": {"line": line, "col": 20},
        "extra": {
            "message": "Use of eval() can lead to arbitrary code execution.",
            "severity": severity,
            "metadata": {
                "cwe": ["CWE-95"],
                "confidence": "HIGH",
                "references": ["https://example.com"],
            },
            "fingerprint": fingerprint,
        },
    }


# ---- normalization (pure) ----------------------------------------------------


def test_no_findings_produces_empty_result() -> None:
    result = semgrep.parse_semgrep_output({"results": [], "errors": []})
    assert result.status == "completed"
    assert result.findings == ()
    assert result.observations == ()


def test_single_finding_is_normalized() -> None:
    result = semgrep.parse_semgrep_output({"results": [_finding_entry()], "errors": []})
    assert result.status == "completed"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_id == "devlens-python-eval-exec"
    assert finding.severity == "high"  # ERROR -> high
    assert finding.scanner_confidence == "HIGH"
    assert finding.references == ("https://example.com",)
    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.file_path == "app/foo.py"
    assert observation.start_line == 10
    assert observation.end_line == 10


def test_multiple_findings_from_different_rules() -> None:
    result = semgrep.parse_semgrep_output(
        {
            "results": [
                _finding_entry(check_id="rule-a", fingerprint="fp-a"),
                _finding_entry(check_id="rule-b", fingerprint="fp-b", path="other.py", line=5),
            ],
            "errors": [],
        }
    )
    assert len(result.findings) == 2
    assert {f.rule_id for f in result.findings} == {"rule-a", "rule-b"}
    assert len(result.observations) == 2


def test_same_rule_multiple_locations_is_one_finding_two_observations() -> None:
    result = semgrep.parse_semgrep_output(
        {
            "results": [
                _finding_entry(fingerprint="fp-1", line=10),
                _finding_entry(fingerprint="fp-2", line=50),
            ],
            "errors": [],
        }
    )
    assert len(result.findings) == 2  # different fingerprints -> genuinely distinct findings
    assert len(result.observations) == 2


def test_severity_without_mapping_is_null_not_guessed() -> None:
    result = semgrep.parse_semgrep_output(
        {"results": [_finding_entry(severity="UNKNOWN_LEVEL")], "errors": []}
    )
    assert result.findings[0].severity is None


def test_source_location_is_preserved() -> None:
    result = semgrep.parse_semgrep_output({"results": [_finding_entry(line=42)], "errors": []})
    assert result.observations[0].start_line == 42
    assert result.observations[0].file_path == "app/foo.py"


def test_scanner_metadata_confidence_and_references_preserved() -> None:
    result = semgrep.parse_semgrep_output({"results": [_finding_entry()], "errors": []})
    finding = result.findings[0]
    assert finding.scanner_confidence == "HIGH"
    assert finding.references == ("https://example.com",)
    assert finding.cvss_score is None  # Semgrep never supplies CVSS
    assert finding.cvss_vector is None


def test_deterministic_normalization() -> None:
    payload = {"results": [_finding_entry()], "errors": []}
    first = semgrep.parse_semgrep_output(payload)
    second = semgrep.parse_semgrep_output(payload)
    assert first.findings == second.findings
    assert first.observations == second.observations


def test_duplicate_result_entry_in_one_run_is_deduplicated() -> None:
    entry = _finding_entry()
    result = semgrep.parse_semgrep_output({"results": [entry, entry], "errors": []})
    assert len(result.findings) == 1
    assert len(result.observations) == 1


def test_malformed_output_not_a_dict() -> None:
    result = semgrep.parse_semgrep_output(["not", "a", "dict"])
    assert result.status == "malformed_output"


def test_malformed_output_results_not_a_list() -> None:
    result = semgrep.parse_semgrep_output({"results": "oops"})
    assert result.status == "malformed_output"


def test_malformed_entries_are_skipped_not_crashed_on() -> None:
    payload = {
        "results": [
            "not-a-dict",
            {"check_id": "x"},  # missing path
            {"check_id": "x", "path": "a.py", "start": "not-a-dict"},
            {"check_id": "x", "path": "a.py", "start": {"line": "not-an-int"}},
        ],
        "errors": [],
    }
    result = semgrep.parse_semgrep_output(payload)
    assert result.status == "completed"
    assert result.findings == ()


def test_scanner_errors_are_surfaced_as_detail_not_a_failure() -> None:
    result = semgrep.parse_semgrep_output({"results": [], "errors": [{"message": "parse error"}]})
    assert result.status == "completed"
    assert result.detail is not None


# ---- invocation (subprocess, monkeypatched) ------------------------------------


def test_unavailable_when_binary_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = semgrep.run_semgrep(Path("/tmp/does-not-matter"))
    assert result.status == "unavailable"


def test_missing_rule_pack_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")
    result = semgrep.run_semgrep(tmp_path, rule_pack_path=tmp_path / "does-not-exist.yml")
    assert result.status == "error"


def test_uses_the_local_fixed_rule_pack_never_auto_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_command: list[str] = []

    def _spy(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_command.extend(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")
    monkeypatch.setattr(subprocess, "run", _spy)
    semgrep.run_semgrep(tmp_path)
    assert "--config" in captured_command
    config_index = captured_command.index("--config")
    assert captured_command[config_index + 1] == str(semgrep.RULE_PACK_PATH)
    assert "auto" not in captured_command


def test_timeout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")

    def _raise(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="semgrep", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _raise)
    result = semgrep.run_semgrep(tmp_path, timeout_seconds=1.0)
    assert result.status == "timeout"


def test_exit_code_two_with_partial_errors_is_not_a_hard_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["semgrep"], returncode=2, stdout='{"results": [], "errors": []}', stderr=""
        ),
    )
    result = semgrep.run_semgrep(tmp_path)
    assert result.status == "completed"


def test_real_failure_exit_code_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["semgrep"], returncode=127, stdout="", stderr="not found"
        ),
    )
    result = semgrep.run_semgrep(tmp_path)
    assert result.status == "error"


def test_malformed_json_stdout_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=["semgrep"], returncode=0, stdout="{not valid json", stderr=""
        ),
    )
    result = semgrep.run_semgrep(tmp_path)
    assert result.status == "malformed_output"


def test_invocation_never_uses_shell_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=["semgrep"], returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/semgrep")
    monkeypatch.setattr(subprocess, "run", _spy)
    semgrep.run_semgrep(tmp_path)
    assert captured.get("shell") is False
    assert isinstance(captured.get("timeout"), float)
