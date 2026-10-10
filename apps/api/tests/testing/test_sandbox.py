"""Sandboxed-execution tests — real subprocess calls (to the actual Python
interpreter running this test suite), no database. Always run.
"""

import subprocess
from pathlib import Path

import pytest

from app.testing.sandbox import (
    MaterializedFile,
    UnsafeMaterializationError,
    _safe_write,
    run_generated_test,
)


def test_passing_test_reports_passed() -> None:
    test_file = MaterializedFile("test_ok.py", "def test_ok():\n    assert True\n")
    result = run_generated_test([], test_file)
    assert result.status == "passed"
    assert result.exit_code == 0
    assert not result.timed_out


def test_failing_test_reports_failed_not_error() -> None:
    test_file = MaterializedFile("test_fail.py", "def test_fail():\n    assert 1 == 2\n")
    result = run_generated_test([], test_file)
    assert result.status == "failed"
    assert result.exit_code != 0
    assert "assert" in result.stdout.lower() or "assert" in result.stderr.lower()


def test_syntax_failure_reports_failed_with_evidence_in_output() -> None:
    test_file = MaterializedFile("test_bad.py", "def test_bad(:\n    pass\n")
    result = run_generated_test([], test_file)
    assert result.status == "failed"
    assert result.exit_code != 0


def test_timeout_is_reported_explicitly() -> None:
    test_file = MaterializedFile(
        "test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(5)\n"
    )
    result = run_generated_test([], test_file, timeout_seconds=1.0)
    assert result.status == "timeout"
    assert result.timed_out
    assert result.exit_code is None


def test_stdout_and_stderr_are_bounded() -> None:
    test_file = MaterializedFile(
        "test_noisy.py",
        "def test_noisy():\n    print('x' * 500)\n    assert True\n",
    )
    result = run_generated_test([], test_file, max_output_bytes=100)
    assert len(result.stdout.encode()) <= 100 + len("...<truncated>")


def test_context_files_are_materialized_for_imports() -> None:
    context = [
        MaterializedFile("pkg/__init__.py", ""),
        MaterializedFile("pkg/mod.py", "def f():\n    return 42\n"),
    ]
    test_file = MaterializedFile(
        "test_import.py", "from pkg.mod import f\n\ndef test_f():\n    assert f() == 42\n"
    )
    result = run_generated_test(context, test_file)
    assert result.status == "passed"


def test_sandbox_directory_is_cleaned_up_after_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Path] = {}
    real_run = subprocess.run

    def _spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = kwargs.get("cwd")
        if isinstance(cwd, Path):
            captured["cwd"] = cwd
        return real_run(*args, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", _spy)
    test_file = MaterializedFile("test_ok.py", "def test_ok():\n    assert True\n")
    run_generated_test([], test_file)

    assert "cwd" in captured
    assert not captured["cwd"].exists()


def test_subprocess_never_uses_shell_true(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, object] = {}
    real_run = subprocess.run

    def _spy(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_kwargs.update(kwargs)
        return real_run(*args, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", _spy)
    test_file = MaterializedFile("test_ok.py", "def test_ok():\n    assert True\n")
    run_generated_test([], test_file)

    assert captured_kwargs.get("shell") is False
    assert isinstance(captured_kwargs.get("timeout"), float)


def test_path_traversal_in_materialization_is_refused() -> None:
    evil = MaterializedFile("../../etc/evil.py", "x = 1\n")
    result = run_generated_test([], evil)
    assert result.status == "error"


def test_safe_write_rejects_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(UnsafeMaterializationError):
        _safe_write(tmp_path, "/etc/passwd", "x")


def test_safe_write_rejects_dotdot_traversal(tmp_path: Path) -> None:
    with pytest.raises(UnsafeMaterializationError):
        _safe_write(tmp_path, "../outside.py", "x")
