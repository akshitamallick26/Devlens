from pathlib import Path

from app.ingestion.inventory import FileCategory, classify_file


def test_text_file_is_classified_as_text(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')\n")
    entry = classify_file(tmp_path, "main.py", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.TEXT
    assert entry.language == "python"
    assert not entry.is_generated


def test_binary_file_is_detected_via_nul_byte(tmp_path: Path) -> None:
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")
    entry = classify_file(tmp_path, "logo.png", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.BINARY


def test_oversized_file_is_unsupported_not_truncated(tmp_path: Path) -> None:
    (tmp_path / "huge.txt").write_text("x" * 100)
    entry = classify_file(tmp_path, "huge.txt", max_file_bytes=10)
    assert entry.category == FileCategory.UNSUPPORTED
    assert entry.skip_reason is not None
    assert "exceeds max file size" in entry.skip_reason


def test_symlink_is_unsupported(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_text("content\n")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    entry = classify_file(tmp_path, "link.txt", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.UNSUPPORTED
    assert entry.skip_reason == "symlink"


def test_known_lockfile_is_flagged_generated(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text("{}\n")
    entry = classify_file(tmp_path, "package-lock.json", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.TEXT
    assert entry.is_generated


def test_minified_file_is_flagged_generated(tmp_path: Path) -> None:
    (tmp_path / "bundle.min.js").write_text("!function(){}();\n")
    entry = classify_file(tmp_path, "bundle.min.js", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.TEXT
    assert entry.is_generated


def test_ordinary_source_file_is_not_flagged_generated(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    entry = classify_file(tmp_path, "app.py", max_file_bytes=1_000_000)
    assert not entry.is_generated


def test_unknown_extension_has_no_language(tmp_path: Path) -> None:
    (tmp_path / "data.xyz").write_text("whatever\n")
    entry = classify_file(tmp_path, "data.xyz", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.TEXT
    assert entry.language is None


def test_empty_file_is_classified_as_text(tmp_path: Path) -> None:
    (tmp_path / "empty.py").write_text("")
    entry = classify_file(tmp_path, "empty.py", max_file_bytes=1_000_000)
    assert entry.category == FileCategory.TEXT
    assert entry.size_bytes == 0
