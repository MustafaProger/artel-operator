"""Deterministic runtime failure/race tests. No real scheduler, portals or notes.

Counts: parameterized races are distinct configurations; seeded state machines
exercise transitions rather than counting repeated assertions as test cases.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from random import Random
from threading import Barrier
from zoneinfo import ZoneInfo
import hashlib
import json
import sqlite3

import pytest

from operator_app import engine, storage
from test_engine import DeferredPool, isolated_engine


DAY = date(2026, 9, 18)
TERMINAL = ("completed", "failed", "needs_review", "no_data")


def reserve(trigger="manual", ident="glopro", day=DAY):
    return storage.create_run(ident, day, day - timedelta(days=3),
                              day - timedelta(days=1), trigger, require_idle=True)


def rows():
    with storage.db() as db:
        return {row["id"]: (row["status"], json.loads(row["payload"]))
                for row in db.execute("SELECT * FROM runs")}


@pytest.mark.parametrize("workers", [2, 8, 16])
@pytest.mark.parametrize("mix", ["manual", "schedule", "mixed"])
def test_sql_reservation_race_across_connections(isolated_engine, workers, mix):
    barrier = Barrier(workers)

    def contender(index):
        barrier.wait(timeout=10)
        trigger = mix if mix != "mixed" else ("manual" if index % 2 else "schedule")
        try:
            return reserve(trigger, ident=f"operator-{index % 3}")
        except ValueError as exc:
            assert "Дождитесь завершения" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        result = list(pool.map(contender, range(workers)))
    winners = [r for r in result if r is not None]
    assert len(winners) == 1
    assert rows() == {winners[0]["id"]: ("queued", winners[0])}


@pytest.mark.parametrize("workers", [2, 12])
def test_engine_manual_and_schedule_race_dispatches_once(isolated_engine, monkeypatch, workers):
    deferred = DeferredPool()
    monkeypatch.setattr(engine, "POOL", deferred)
    barrier = Barrier(workers)

    def submit(index):
        barrier.wait(timeout=10)
        try:
            return engine.submit("glopro", str(DAY), "manual" if index % 2 else "schedule")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(submit, range(workers)))
    assert len([r for r in results if r]) == len(deferred.calls) == 1
    assert len(rows()) == 1


@pytest.mark.parametrize("status", ["queued", "running", *TERMINAL])
def test_restart_only_interrupts_active_and_preserves_all_other_payloads(isolated_engine, status):
    record = reserve()
    record.update(status=status, report="previous report", events=[{"stage": "audit"}],
                  files=[{"name": "previous.zip"}], finished_at=None if status in {"queued", "running"} else "earlier")
    storage.save_run(record)
    storage.set_setting("enabled_since:glopro", "2026-09-14T00:00:00+03:00")
    storage.init()
    after = storage.get_run(record["id"])
    if status in {"queued", "running"}:
        assert after["status"] == "failed" and after["finished_at"]
        assert "Процесс был остановлен" in after["error"]
        for key in ("id", "run_date", "report", "events", "files", "created_at"):
            assert after[key] == record[key]
    else:
        assert after == record
    storage.init()
    assert storage.get_run(record["id"]) == after
    assert storage.setting("enabled_since:glopro") == "2026-09-14T00:00:00+03:00"


@pytest.mark.parametrize("status", TERMINAL)
def test_scheduled_once_survives_terminal_state_and_allows_explicit_manual_retry(isolated_engine, status):
    scheduled = reserve("schedule")
    scheduled.update(status=status, finished_at="finished")
    storage.save_run(scheduled)
    storage.init()
    with pytest.raises(sqlite3.IntegrityError):
        reserve("schedule")
    manual = reserve("manual")
    assert manual["id"] != scheduled["id"]
    assert storage.get_run(scheduled["id"]) == scheduled
    assert len(rows()) == 2


@pytest.mark.parametrize("seed", [29, 1701, 65537, 20260929])
def test_seeded_reservation_restart_state_machine(isolated_engine, seed):
    """4 seeds × 160 actions: independent persisted-state oracle each step."""
    rng = Random(seed)
    expected = {}
    scheduled_keys = set()
    active = None
    for _ in range(160):
        action = rng.choice(("reserve", "reserve", "running", "finish", "restart"))
        if action == "reserve":
            day = DAY - timedelta(days=rng.randrange(4))
            ident = f"operator-{rng.randrange(3)}"
            trigger = rng.choice(("manual", "schedule"))
            key = (ident, str(day))
            if active is not None:
                with pytest.raises(ValueError):
                    reserve(trigger, ident, day)
            elif trigger == "schedule" and key in scheduled_keys:
                with pytest.raises(sqlite3.IntegrityError):
                    reserve(trigger, ident, day)
            else:
                record = reserve(trigger, ident, day)
                active = record["id"]
                expected[active] = "queued"
                if trigger == "schedule":
                    scheduled_keys.add(key)
        elif action in {"running", "finish"} and active is not None:
            record = storage.get_run(active)
            status = "running" if action == "running" else rng.choice(TERMINAL)
            record["status"] = status
            storage.save_run(record)
            expected[active] = status
            if action == "finish":
                active = None
        elif action == "restart":
            storage.init()
            if active is not None:
                expected[active] = "failed"
                active = None
        actual = rows()
        assert {key: value[0] for key, value in actual.items()} == expected
        assert all(sql_status == payload["status"] for sql_status, payload in actual.values())
        assert sum(s in {"queued", "running"} for s in expected.values()) <= 1


def test_failed_transaction_rolls_back_reservation_and_settings(isolated_engine):
    record = reserve()
    before = rows()
    with pytest.raises(OSError, match="fault injection"):
        with storage.db() as db:
            db.execute("UPDATE runs SET status='running'")
            db.execute("INSERT INTO settings VALUES ('partial', 'true')")
            raise OSError("fault injection")
    assert rows() == before
    assert storage.setting("partial") is None
    assert storage.get_run(record["id"])["status"] == "queued"


@pytest.mark.parametrize("hour,expected_days", [(8, {"2026-09-22", "2026-09-25"}),
                                              (10, {"2026-09-22", "2026-09-25", "2026-09-29"})])
def test_wakeup_window_and_repeated_tick_do_not_replay_terminal_schedule(isolated_engine, monkeypatch, hour, expected_days):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 29, hour, tzinfo=ZoneInfo("Europe/Moscow")).astimezone(tz)

    monkeypatch.setattr(engine, "datetime", Frozen)
    monkeypatch.setattr(storage, "credentials", lambda: ("synthetic", "synthetic"))
    attempts = []

    class FailOncePool:
        def submit(self, fn, conf, record, uploads):
            attempts.append(record["run_date"])
            record.update(status="failed", finished_at="synthetic fault")
            storage.save_run(record)

    monkeypatch.setattr(engine, "POOL", FailOncePool())
    storage.set_setting("enabled_since:glopro", "2026-01-01T00:00:00+03:00")
    for index in range(24):
        engine.tick(Frozen.now(ZoneInfo("Europe/Moscow")))
        if index in (7, 15):
            storage.init()
    assert set(attempts) == expected_days
    assert len(attempts) == len(expected_days)
    assert {r[1]["run_date"] for r in rows().values()} == expected_days


@pytest.fixture
def synthetic_pipeline(isolated_engine, monkeypatch):
    calls = []

    def handler(conf, record, directory, progress):
        calls.append(record["id"])
        path = directory / "synthetic.txt"
        path.write_text("independent synthetic source")
        return [{"path": str(path)}]

    def processor(conf, record, sources, root, progress, *, imported):
        (root / "result.txt").write_text("synthetic result")
        return engine.ProcessResult("completed", report="synthetic", audit={"valid": True})

    monkeypatch.setattr(engine, "HANDLERS", {"glopro": handler})
    monkeypatch.setattr(engine, "PROCESSORS", {"glopro": processor})
    return isolated_engine, calls


@pytest.mark.parametrize("fault", ["instruction", "audit", "source_hash", "zip_open", "zip_write", "metadata"])
def test_local_artifact_fault_never_replays_handler_or_damages_previous_success(synthetic_pipeline, monkeypatch, fault):
    (data, _), calls = synthetic_pipeline
    previous = engine.submit("glopro", str(DAY))
    old_root = data / "runs" / previous["id"]
    old_hashes = {p.relative_to(old_root): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in old_root.rglob("*") if p.is_file()}
    original_write, original_read = Path.write_text, Path.read_bytes

    if fault in {"instruction", "audit"}:
        target = "Инструкция.md" if fault == "instruction" else "Проверка расчётов.json"
        def fail_write(path, *args, **kwargs):
            if path.name == target and old_root not in path.parents:
                raise OSError("private fault details")
            return original_write(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", fail_write)
    elif fault == "source_hash":
        def fail_read(path):
            if path.name == "synthetic.txt" and old_root not in path.parents:
                raise OSError("private fault details")
            return original_read(path)
        monkeypatch.setattr(Path, "read_bytes", fail_read)
    elif fault.startswith("zip_"):
        def fail_zip(*args, **kwargs):
            raise OSError("private fault details")
        monkeypatch.setattr(engine.zipfile.ZipFile, "__init__" if fault == "zip_open" else "write", fail_zip)
    else:
        monkeypatch.setattr(engine, "_file", lambda *args: (_ for _ in ()).throw(OSError("private fault details")))
    current = engine.submit("glopro", str(DAY))
    assert current["status"] == "failed" and current["finished_at"]
    assert "private" not in current["error"]
    assert calls.count(current["id"]) == (0 if fault == "instruction" else 1)
    assert storage.get_run(previous["id"]) == previous
    assert {p.relative_to(old_root): hashlib.sha256(original_read(p)).hexdigest()
            for p in old_root.rglob("*") if p.is_file()} == old_hashes


def test_permanent_database_failure_cleans_import_and_restart_recovers_without_replay(synthetic_pipeline, monkeypatch, tmp_path):
    (data, _), calls = synthetic_pipeline
    deferred = DeferredPool()
    monkeypatch.setattr(engine, "POOL", deferred)
    upload = tmp_path / "input.txt"
    upload.write_text("synthetic upload")
    record = engine.submit("glopro", str(DAY), "import", [{"path": str(upload), "name": "input.txt"}])
    conf = deferred.calls[0][1][0]
    with monkeypatch.context() as broken:
        broken.setattr(storage, "save_run", lambda record: (_ for _ in ()).throw(sqlite3.OperationalError("disk unavailable")))
        with pytest.raises(sqlite3.OperationalError):
            engine.execute(conf, record, [{"path": str(upload), "name": "input.txt"}])
    assert not upload.exists()
    assert calls == []
    assert storage.get_run(record["id"])["status"] == "queued"
    storage.init()
    assert storage.get_run(record["id"])["status"] == "failed"
    assert deferred.calls and calls == []


@pytest.mark.parametrize("failure_stage", ["initial", "progress", "checkpoint", "final"])
def test_transient_database_failure_is_terminal_without_replaying_side_effect(synthetic_pipeline, monkeypatch, failure_stage):
    (data, _), calls = synthetic_pipeline
    published = []
    base_handler = engine.HANDLERS["glopro"]
    base_processor = engine.PROCESSORS["glopro"]

    def handler(conf, record, directory, progress):
        sources = base_handler(conf, record, directory, progress)
        progress({"stage": "source_ready", "message": "synthetic"})
        return sources

    def processor(*args, **kwargs):
        result = base_processor(*args, **kwargs)
        def publish():
            persisted = storage.get_run(args[1]["id"])
            assert persisted["status"] == "running" and persisted["finished_at"] is None
            assert persisted["files"]
            published.append(persisted["id"])
        result.publish = publish
        return result

    monkeypatch.setitem(engine.HANDLERS, "glopro", handler)
    monkeypatch.setitem(engine.PROCESSORS, "glopro", processor)
    save = storage.save_run
    injected = []

    def fail_once(record):
        stage = ("final" if record["finished_at"] else "checkpoint" if record["files"]
                 else "progress" if record["events"] else "initial")
        if stage == failure_stage and not injected:
            injected.append(stage)
            raise sqlite3.OperationalError("private database fault")
        save(record)

    monkeypatch.setattr(storage, "save_run", fail_once)
    run = engine.submit("glopro", str(DAY))
    saved = storage.get_run(run["id"])
    assert injected == [failure_stage]
    assert saved["status"] == "failed" and saved["finished_at"]
    assert "private database fault" not in saved["error"]
    assert len(calls) == (0 if failure_stage == "initial" else 1)
    assert len(published) == (1 if failure_stage == "final" else 0)
    if failure_stage == "final":
        assert "Внешний результат мог быть сохранён" in saved["error"]
    assert sum(status in {"queued", "running"} for status, _ in rows().values()) == 0


def test_permanent_final_database_failure_leaves_active_checkpoint_for_recovery(synthetic_pipeline, monkeypatch):
    (data, _), calls = synthetic_pipeline
    deferred = DeferredPool()
    monkeypatch.setattr(engine, "POOL", deferred)
    record = engine.submit("glopro", str(DAY))
    conf = deferred.calls[0][1][0]
    save = storage.save_run
    failed_writes = []

    def save_until_finish(record):
        if record["finished_at"]:
            failed_writes.append(record["status"])
            raise sqlite3.OperationalError("storage unavailable")
        save(record)

    with monkeypatch.context() as outage:
        outage.setattr(storage, "save_run", save_until_finish)
        with pytest.raises(sqlite3.OperationalError):
            engine.execute(conf, record)
    assert failed_writes == ["completed", "failed"]
    assert len(calls) == 1
    assert storage.get_run(record["id"])["status"] == "running"
    assert list((data / "runs" / record["id"]).glob("*.zip"))
    storage.init()
    assert storage.get_run(record["id"])["status"] == "failed"
    assert len(calls) == 1


@pytest.mark.parametrize("failure_stage", ["handler", "processor", "publisher"])
def test_stage_exception_runs_at_most_once_and_does_not_poison_next_run(synthetic_pipeline, monkeypatch, failure_stage):
    _, calls = synthetic_pipeline
    publisher_calls = []
    handler = engine.HANDLERS["glopro"]
    processor = engine.PROCESSORS["glopro"]
    def fail():
        raise RuntimeError("private stage failure")
    def broken_handler(*args):
        handler(*args)
        fail()
    def broken_processor(*args, **kwargs):
        result = processor(*args, **kwargs)
        if failure_stage == "processor":
            fail()
        def broken_publish():
            publisher_calls.append(args[1]["id"])
            fail()
        result.publish = broken_publish
        return result
    with monkeypatch.context() as broken:
        if failure_stage == "handler":
            broken.setitem(engine.HANDLERS, "glopro", broken_handler)
        else:
            broken.setitem(engine.PROCESSORS, "glopro", broken_processor)
        run = engine.submit("glopro", str(DAY))
    assert run["status"] == "failed"
    assert "private" not in run["error"]
    assert calls == [run["id"]]
    assert len(publisher_calls) == (1 if failure_stage == "publisher" else 0)
    next_run = engine.submit("glopro", str(DAY))
    assert next_run["status"] == "completed" and next_run["id"] != run["id"]
    assert storage.get_run(run["id"])["status"] == "failed"


def test_reservation_commit_failure_rolls_back_and_never_dispatches(isolated_engine, monkeypatch):
    pool = DeferredPool()
    monkeypatch.setattr(engine, "POOL", pool)
    connect = storage.sqlite3.connect

    class CommitFault:
        def __init__(self, connection):
            object.__setattr__(self, "connection", connection)
        def __getattr__(self, key):
            return getattr(self.connection, key)
        def __setattr__(self, key, value):
            setattr(self.connection, key, value)
        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

    with monkeypatch.context() as fault:
        fault.setattr(storage.sqlite3, "connect", lambda *a, **kw: CommitFault(connect(*a, **kw)))
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            engine.submit("glopro", str(DAY))
    assert rows() == {} and pool.calls == []
    run = engine.submit("glopro", str(DAY))
    assert run["status"] == "queued" and len(pool.calls) == 1


@pytest.mark.parametrize("blocked_index", [0, 1])
@pytest.mark.parametrize("outcome", ["completed", "needs_review", "failed"])
def test_cleanup_attempts_every_upload_and_preserves_pipeline_outcome(synthetic_pipeline, monkeypatch, tmp_path, blocked_index, outcome):
    paths = [tmp_path / f"upload-{i}.txt" for i in range(3)]
    for path in paths:
        path.write_text("synthetic source")
    def process(*args, **kwargs):
        if outcome == "failed":
            raise ValueError("synthetic validation error")
        return engine.ProcessResult(outcome, report="synthetic")
    monkeypatch.setitem(engine.PROCESSORS, "glopro", process)
    unlink = Path.unlink
    attempted = []
    def reject_one(path, *args, **kwargs):
        if path in paths:
            attempted.append(path)
            if path == paths[blocked_index]:
                raise PermissionError("private path details")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", reject_one)
    run = engine.submit("glopro", str(DAY), "import", [{"path": str(p), "name": p.name} for p in paths])
    assert run["status"] == outcome
    assert attempted == paths
    assert [p for p in paths if p.exists()] == [paths[blocked_index]]
    saved = storage.get_run(run["id"])
    assert saved == run and saved["finished_at"]
    assert any(event.get("stage") == "cleanup" for event in saved["events"])
    assert "private path details" not in json.dumps(saved)
