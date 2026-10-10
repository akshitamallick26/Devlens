from pathlib import Path

import pytest

from app.ingestion.checkout import checkout_repository
from app.ingestion.git_adapter import (
    FileTooLargeError,
    GitCommandError,
    GitTimeoutError,
    PathEscapeError,
    list_tracked_files,
    read_file_at_revision,
    resolve_current_branch,
    resolve_head_commit,
)

TIMEOUT = 10.0


def test_clone_and_resolve_head_commit_matches_local_repo(
    local_git_repo: Path, local_git_repo_head_sha: str
) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        assert checkout.commit_sha == local_git_repo_head_sha
        assert len(checkout.commit_sha) == 40
        assert checkout.branch == "main"


def test_checkout_directory_is_removed_after_the_with_block(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        checkout_path = checkout.path
        assert checkout_path.exists()
    assert not checkout_path.exists()


def test_checkout_directory_is_removed_even_if_the_caller_raises(local_git_repo: Path) -> None:
    captured_path: Path | None = None
    with pytest.raises(RuntimeError):
        with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
            captured_path = checkout.path
            raise RuntimeError("simulated failure after checkout")
    assert captured_path is not None
    assert not captured_path.exists()


def test_clone_of_nonexistent_repository_raises_git_command_error(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    with pytest.raises(GitCommandError):
        with checkout_repository(str(nonexistent), timeout=TIMEOUT):
            pass


def test_clone_timeout_raises_git_timeout_error(local_git_repo: Path) -> None:
    with pytest.raises(GitTimeoutError):
        with checkout_repository(str(local_git_repo), timeout=0.0001):
            pass


def test_list_tracked_files_is_sorted_and_complete(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        files = list_tracked_files(checkout.path, timeout=TIMEOUT)
    assert files == sorted(files)
    assert set(files) == {
        "README.md",
        "duplicate_a.txt",
        "duplicate_b.txt",
        "logo.png",
        "nested/deep/file.txt",
        "src/main.py",
    }


def test_list_tracked_files_is_deterministic_across_calls(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        first = list_tracked_files(checkout.path, timeout=TIMEOUT)
        second = list_tracked_files(checkout.path, timeout=TIMEOUT)
    assert first == second


def test_read_file_at_revision_returns_exact_content(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        content = read_file_at_revision(checkout.path, "src/main.py", max_bytes=1_000_000)
    assert content == b"def main():\n    print('hello')\n"


def test_read_file_at_revision_rejects_oversized_file(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        with pytest.raises(FileTooLargeError):
            read_file_at_revision(checkout.path, "src/main.py", max_bytes=5)


def test_read_file_at_revision_rejects_path_traversal(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        with pytest.raises(PathEscapeError):
            read_file_at_revision(checkout.path, "../../../../etc/passwd", max_bytes=1000)


def test_read_file_at_revision_rejects_absolute_path(local_git_repo: Path) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        with pytest.raises(PathEscapeError):
            read_file_at_revision(checkout.path, "/etc/passwd", max_bytes=1000)


def test_read_file_at_revision_refuses_a_symlink_escaping_the_checkout(
    tmp_path: Path, local_git_repo: Path
) -> None:
    """The actual attack this defends against: a tracked path that is a
    symlink pointing outside the checkout directory entirely."""
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the checkout\n")

    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        escape_link = checkout.path / "escape.txt"
        escape_link.symlink_to(secret)
        with pytest.raises(PathEscapeError):
            read_file_at_revision(checkout.path, "escape.txt", max_bytes=1000)


def test_resolve_current_branch_matches_the_local_repos_default_branch(
    local_git_repo: Path,
) -> None:
    with checkout_repository(str(local_git_repo), timeout=TIMEOUT) as checkout:
        assert resolve_current_branch(checkout.path, timeout=TIMEOUT) == "main"


def test_resolve_head_commit_on_a_non_git_directory_raises(tmp_path: Path) -> None:
    empty_dir = tmp_path / "not-a-repo"
    empty_dir.mkdir()
    with pytest.raises(GitCommandError):
        resolve_head_commit(empty_dir, timeout=TIMEOUT)
