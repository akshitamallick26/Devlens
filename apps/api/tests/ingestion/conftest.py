"""A tiny, deterministic local Git repository fixture — used so Git-adapter
and ingestion-service tests never depend on real network access or a real
GitHub repository, per docs/architecture/repository-ingestion.md's own
instruction to treat ingestion inputs as untrusted and testable in isolation.
"""

import os
import subprocess
from pathlib import Path

import pytest

_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "DevLens Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "DevLens Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "LC_ALL": "C",
}


def run_git(repo_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo_dir, env=_GIT_ENV, check=True, capture_output=True, text=True
    )


@pytest.fixture
def local_git_repo(tmp_path: Path) -> Path:
    """A local repo with: a Python source file, a Markdown file, a nested
    directory, a binary file, and two files with identical content (for
    dedup testing) — all committed once."""
    repo_dir = tmp_path / "source-repo"
    repo_dir.mkdir()
    run_git(repo_dir, "init", "-q", "-b", "main")

    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "main.py").write_text("def main():\n    print('hello')\n")
    (repo_dir / "README.md").write_text("# Test repo\n")

    nested = repo_dir / "nested" / "deep"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("deep content\n")

    (repo_dir / "duplicate_a.txt").write_text("identical content\n")
    (repo_dir / "duplicate_b.txt").write_text("identical content\n")

    (repo_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))

    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "initial commit")
    return repo_dir


@pytest.fixture
def local_git_repo_head_sha(local_git_repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=local_git_repo,
        env=_GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
