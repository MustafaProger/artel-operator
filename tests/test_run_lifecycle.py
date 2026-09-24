from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier

import pytest

from operator_app import engine, storage
from test_engine import DeferredPool, isolated_engine, source_file


def create_record(**kwargs):
    return storage.create_run("glopro", date(2026, 9, 18), date(2026, 9, 15),
                              date(2026, 9, 17), "manual", **kwargs)


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_run_beyond_history_page_still_blocks_submit(isolated_engine, monkeypatch, status):
    active = create_record()
    active["status"] = status
    storage.save_run(active)
    for _ in range(101):
        completed = create_record()
        completed["status"] = "completed"
        storage.save_run(completed)
    assert active["id"] not in {r["id"] for r in storage.runs()}
    pool = DeferredPool()
    monkeypatch.setattr(engine, "POOL", pool)
    with pytest.raises(ValueError, match="Дождитесь завершения"):
        engine.submit("glopro", "2026-09-18")
    assert pool.calls == []


def test_reservation_is_atomic_across_database_connections(isolated_engine):
    barrier = Barrier(2)

    def reserve():
        barrier.wait(timeout=5)
        try:
            return create_record(require_idle=True)
        except ValueError as exc:
            assert "Дождитесь завершения" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(storage.runs()) == 1


def test_pool_rejection_finishes_record_and_allows_manual_retry(isolated_engine, monkeypatch):
    class RejectingPool:
        def submit(self, *args):
            raise RuntimeError("private executor internals")

    monkeypatch.setattr(engine, "POOL", RejectingPool())
    with pytest.raises(RuntimeError):
        engine.submit("glopro", "2026-09-18")
    failed = storage.runs()[0]
    assert failed["status"] == "failed"
    assert failed["finished_at"]
    assert "private executor internals" not in failed["error"]
    pool = DeferredPool()
    monkeypatch.setattr(engine, "POOL", pool)
    retry = engine.submit("glopro", "2026-09-18")
    assert retry["id"] != failed["id"]
    assert len(pool.calls) == 1


@pytest.mark.parametrize("failure", ["directory", "inventory"])
def test_artifact_io_failure_finishes_run_and_cleans_import(isolated_engine, tmp_path, monkeypatch, failure):
    source = source_file(tmp_path / "upload.xlsx")
    if failure == "directory":
        mkdir = Path.mkdir

        def fail_directory(path, *args, **kwargs):
            if path.name == "Файлы по фирмам":
                raise OSError("private filesystem details")
            return mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_directory)
    else:
        def fail_inventory(*args):
            raise OSError("private filesystem details")

        monkeypatch.setattr(engine, "_file", fail_inventory)
    run = engine.submit("glopro", "2026-09-18", "import", [{"path": str(source), "name": "source.xlsx"}])
    saved = storage.get_run(run["id"])
    assert saved["status"] == "failed"
    assert saved["finished_at"]
    assert "private filesystem details" not in saved["error"]
    assert not source.exists()


def test_edition_identity_is_saved_with_reservation(isolated_engine):
    record = create_record(require_idle=True, edition_key="recovery-test")
    assert storage.get_run(record["id"]) == record
    assert record["edition_slot"] == "manual:recovery-test"
    assert record["off_schedule"] is True
