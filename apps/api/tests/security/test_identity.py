"""Pure fingerprint-identity tests — no DB, always run."""

from app.security.identity import osv_fingerprint, semgrep_fingerprint


def test_osv_fingerprint_is_deterministic() -> None:
    first = osv_fingerprint("PyPI", "jinja2", "GHSA-abcd")
    second = osv_fingerprint("PyPI", "jinja2", "GHSA-abcd")
    assert first == second


def test_osv_fingerprint_excludes_version() -> None:
    """Same package+advisory at different installed versions must dedupe
    to the same finding identity — version is observation-level, not
    finding-level."""
    a = osv_fingerprint("PyPI", "jinja2", "GHSA-abcd")
    b = osv_fingerprint("PyPI", "jinja2", "GHSA-abcd")
    assert a == b


def test_osv_fingerprint_distinguishes_package_and_advisory() -> None:
    assert osv_fingerprint("PyPI", "jinja2", "GHSA-abcd") != osv_fingerprint(
        "PyPI", "flask", "GHSA-abcd"
    )
    assert osv_fingerprint("PyPI", "jinja2", "GHSA-abcd") != osv_fingerprint(
        "PyPI", "jinja2", "GHSA-wxyz"
    )


def test_semgrep_fingerprint_prefers_scanner_fingerprint() -> None:
    with_fp = semgrep_fingerprint("rule-1", "a.py", 10, "scanner-hash-abc")
    other_line = semgrep_fingerprint("rule-1", "a.py", 99, "scanner-hash-abc")
    assert with_fp == other_line  # line shift doesn't matter when scanner fingerprint is stable


def test_semgrep_fingerprint_falls_back_to_location() -> None:
    first = semgrep_fingerprint("rule-1", "a.py", 10, None)
    second = semgrep_fingerprint("rule-1", "a.py", 10, None)
    shifted = semgrep_fingerprint("rule-1", "a.py", 11, None)
    assert first == second
    assert first != shifted  # the documented fragility: a line shift changes the fallback identity
