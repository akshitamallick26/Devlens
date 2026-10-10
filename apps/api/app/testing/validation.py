"""Pre-execution static validation of generated test source — pure, no DB,
no subprocess, no execution. Task 8's explicit framing: this proves nothing
about runtime safety. It is a fail-closed filter that keeps obviously
malformed or obviously dangerous generated output from ever reaching
app/testing/sandbox.py, nothing more. The sandbox boundary (Task 9/10) is
what's actually responsible for what a test can do once it runs; this
module's forbidden-pattern checks are best-effort static detection of the
most common ways a careless or adversarial generation could try to escape
that boundary, not an exhaustive guarantee.
"""

import ast

from app.testing.citations import EvidenceSet
from app.testing.existing_tests import is_test_file_path
from app.testing.models import GeneratedTestOutput, TestTarget, ValidationIssue, ValidationResult

MAX_SOURCE_BYTES = 200_000

_FORBIDDEN_IMPORT_MODULES = frozenset(
    {
        "subprocess",
        "socket",
        "multiprocessing",
        "ctypes",
        "urllib.request",
        "urllib2",
        "requests",
        "httpx",
        "http.client",
        "ftplib",
        "telnetlib",
        "smtplib",
        "pty",
        "shlex",
    }
)
_FORBIDDEN_CALL_NAMES = frozenset({"eval", "exec", "compile", "__import__"})
_FORBIDDEN_OS_ATTRS = frozenset(
    {
        "system",
        "popen",
        "popen2",
        "popen3",
        "popen4",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnv",
        "spawnve",
        "fork",
        "forkpty",
        "kill",
        "killpg",
    }
)


def _dotted_name(node: ast.expr) -> str | None:
    """Reconstructs a dotted attribute chain (`os.path.join` ->
    "os.path.join") where possible; returns None for anything more dynamic
    than a plain chain of names/attributes — which is exactly the case this
    best-effort check cannot reason about, so it's left unflagged rather
    than guessed at."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return None
    return ".".join(reversed(parts))


def _find_forbidden_operations(tree: ast.Module) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_IMPORT_MODULES:
                    issues.append(ValidationIssue("forbidden_import", f"import of {alias.name!r}"))
        elif isinstance(node, ast.ImportFrom):
            if node.module in _FORBIDDEN_IMPORT_MODULES:
                issues.append(ValidationIssue("forbidden_import", f"import from {node.module!r}"))
        elif isinstance(node, ast.Call):
            name = (
                _dotted_name(node.func)
                if isinstance(node.func, (ast.Name, ast.Attribute))
                else None
            )
            if name is None:
                continue
            last = name.rsplit(".", 1)[-1]
            if name in _FORBIDDEN_CALL_NAMES or last in _FORBIDDEN_CALL_NAMES:
                issues.append(ValidationIssue("forbidden_call", f"call to {name}()"))
            elif name.startswith("os.") and last in _FORBIDDEN_OS_ATTRS:
                issues.append(ValidationIssue("forbidden_call", f"call to {name}()"))
    return issues


def _has_discoverable_test(tree: ast.Module) -> bool:
    class_methods: dict[str, list[str]] = {}
    has_top_level_test_function = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            has_top_level_test_function = True
        elif isinstance(node, ast.ClassDef):
            methods = [
                n.name
                for n in node.body
                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
            ]
            if methods:
                class_methods[node.name] = methods
    if has_top_level_test_function:
        return True
    return any(name.startswith("Test") for name in class_methods)


def validate_generated_test(
    output: GeneratedTestOutput,
    target: TestTarget,
    evidence: EvidenceSet,
    existing_file_paths: frozenset[str],
) -> ValidationResult:
    issues: list[ValidationIssue] = []

    source_bytes = output.source.encode("utf-8", errors="surrogateescape")
    if len(source_bytes) > MAX_SOURCE_BYTES:
        issues.append(
            ValidationIssue("source_too_large", f"{len(source_bytes)} bytes > {MAX_SOURCE_BYTES}")
        )
        # Too large to safely parse/walk further in this pass — everything
        # else below assumes a bounded tree.
        return ValidationResult(passed=False, issues=tuple(issues))

    try:
        tree = ast.parse(output.source)
    except SyntaxError as exc:
        issues.append(ValidationIssue("syntax_error", str(exc)))
        return ValidationResult(passed=False, issues=tuple(issues))

    if not _has_discoverable_test(tree):
        issues.append(ValidationIssue("no_test_found", "no test_* function or Test* class found"))

    issues.extend(_find_forbidden_operations(tree))

    if output.target_symbol_id != target.target_symbol_id:
        issues.append(
            ValidationIssue(
                "target_mismatch",
                f"output targets {output.target_symbol_id}, requested {target.target_symbol_id}",
            )
        )

    valid_ids = evidence.valid_ids()
    unknown_citations = [cid for cid in output.cited_evidence_ids if cid not in valid_ids]
    if unknown_citations:
        issues.append(
            ValidationIssue("unknown_evidence_id", f"cites unknown evidence: {unknown_citations}")
        )

    path = output.file_path
    if path.startswith("/") or ".." in path.split("/"):
        issues.append(ValidationIssue("unsafe_path", f"path escapes sandbox: {path!r}"))
    elif not path.endswith(".py"):
        issues.append(ValidationIssue("unsafe_path", f"not a .py file: {path!r}"))
    elif not is_test_file_path(path):
        issues.append(ValidationIssue("unsafe_path", f"not a recognizable test filename: {path!r}"))
    elif path in existing_file_paths:
        issues.append(
            ValidationIssue("overwrites_source", f"path collides with existing source: {path!r}")
        )

    return ValidationResult(passed=not issues, issues=tuple(issues))
