"""Strict artifact traversal regressions; real permissions, temporary data only."""
import os
from pathlib import Path
import zipfile

import pytest

from operator_app import engine, storage
from test_engine import isolated_engine, source_file
from test_yandex import source as yandex_source, yandex_config


@pytest.mark.skipif(os.geteuid() == 0, reason="Root bypasses real directory permission checks")
@pytest.mark.parametrize("kind", ["glopro", "yandex"])
@pytest.mark.parametrize("stage", ["zip", "inventory"])
@pytest.mark.parametrize("location", ["root", "nested"])
def test_unreadable_artifact_directory_blocks_note_publication(
        isolated_engine, tmp_path, monkeypatch, kind, stage, location):
    _, operators = isolated_engine
    notes = tmp_path / "notes"
    notes.mkdir()
    if kind == "glopro":
        config = operators / "glopro.md"
        config.write_text(config.read_text().replace("rules: {}", f"rules: {{}}\nobsidian_output: '{notes}'"))
        day, title = "2026-09-18", "Активация — 18.09.2026.md"
        upload = source_file(tmp_path / "source.xlsx")
        nested = "Файлы по фирмам"
    else:
        yandex_config(operators, notes)
        monkeypatch.setitem(engine.HANDLERS, "yandex", lambda *args: pytest.fail("External handler"))
        day, title = "2026-09-22", "Яндекс Заправки — 22.09.2026.md"
        upload = yandex_source(tmp_path / "source.xlsx")
        nested = "Исходные файлы"
    note = notes / title
    original = "# Предыдущий результат\nСохранить ручной текст.  \n"
    note.write_text(original)
    protected = []

    def revoke(archive):
        root = Path(archive.filename).parent
        path = root if location == "root" else root / nested
        protected.append((path, path.stat().st_mode & 0o777))
        path.chmod(0)
        # Establish that the test exercises actual denial, not a mocked scanner.
        with pytest.raises(PermissionError):
            list(path.iterdir())

    if stage == "zip":
        enter = zipfile.ZipFile.__enter__

        def revoke_before_archive_scan(archive):
            result = enter(archive)
            if str(archive.filename).endswith(".zip"):
                revoke(archive)
            return result

        monkeypatch.setattr(zipfile.ZipFile, "__enter__", revoke_before_archive_scan)
    else:
        close = zipfile.ZipFile.close

        def revoke_before_inventory(archive):
            target = archive.fp is not None and str(archive.filename).endswith(".zip")
            result = close(archive)
            if target:
                revoke(archive)
            return result

        monkeypatch.setattr(zipfile.ZipFile, "close", revoke_before_inventory)
    try:
        run = engine.submit(kind, day, "import", [{"path": str(upload), "name": upload.name}])
        assert len(protected) == 1
        assert run["status"] == "failed"
        assert run["finished_at"] and run["files"] == []
        assert storage.get_run(run["id"]) == run
        assert note.read_text() == original
        assert not upload.exists()
    finally:
        for path, mode in protected:
            path.chmod(mode)


def test_strict_traversal_preserves_existing_file_inclusion_rules(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()
    (root / "empty").mkdir()
    (root / ".hidden").write_text("hidden artifact")
    (nested / "result.txt").write_text("nested artifact")
    (root / "prior.zip").write_bytes(b"prior archive")
    # Like the previous rglob/is_file combination: file symlinks are included,
    # directory symlinks are not traversed, and dangling symlinks are ignored.
    (root / "file-link").symlink_to(nested / "result.txt")
    (root / "directory-link").symlink_to(nested, target_is_directory=True)
    (root / "dangling").symlink_to(root / "absent")
    assert {p.relative_to(root).as_posix() for p in engine._artifact_files(root)} == {
        ".hidden", "nested/result.txt", "prior.zip", "file-link"}


@pytest.mark.parametrize("operation", ["list", "stat"])
@pytest.mark.parametrize("location", ["root", "nested"])
def test_traversal_propagates_io_errors(tmp_path, monkeypatch, operation, location):
    root = tmp_path / "run"
    nested = root / "nested"
    nested.mkdir(parents=True)
    artifact = nested / "result.txt"
    artifact.write_text("synthetic")
    if operation == "list":
        original = Path.iterdir
        blocked = root if location == "root" else nested

        def failed(path):
            if path == blocked:
                raise OSError("synthetic directory I/O failure")
            return original(path)

        monkeypatch.setattr(Path, "iterdir", failed)
    else:
        original = Path.lstat
        blocked = nested if location == "root" else artifact

        def failed(path):
            if path == blocked:
                raise OSError("synthetic metadata I/O failure")
            return original(path)

        monkeypatch.setattr(Path, "lstat", failed)
    with pytest.raises(OSError, match="synthetic"):
        list(engine._artifact_files(root))
