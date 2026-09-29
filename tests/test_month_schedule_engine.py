"""Month-close integration checks using temporary history and synthetic workbooks.

All portal methods are mocked; these tests neither run a real scheduler nor
publish to the user's notes. Calendar-only boundary coverage lives separately.
"""
from contextlib import nullcontext
from datetime import date, datetime
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from openpyxl import load_workbook

from operator_app import config, engine, storage
from operator_app.glopro import GloProConnector
from test_engine import isolated_engine, source_file
from test_weekly import weekly_file


ZONE = ZoneInfo("Europe/Moscow")


@pytest.fixture
def month_engine(isolated_engine, monkeypatch):
    data, operators = isolated_engine
    path = operators / "glopro.md"
    path.write_text(path.read_text().replace(
        "  days: [tue, fri]",
        "  days: [tue, fri]\n  month_boundary: close_previous_month\n"
        "  month_boundary_from: '2026-09-29'",
    ))

    class Frozen(datetime):
        current = datetime(2026, 10, 6, 10, tzinfo=ZONE)

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz)

    monkeypatch.setattr(engine, "datetime", Frozen)
    monkeypatch.setattr(storage, "credentials", lambda: ("synthetic", "synthetic"))
    return data, operators, Frozen


def fake_download(conf, record, directory, progress):
    path = source_file(directory / "ordinary.xlsx", tx_date=date.fromisoformat(record["period_end"]))
    progress({"stage": "downloaded", "activity_check_complete": True})
    return [{"path": str(path), "client": 'ООО "ТЕСТ"', "client_id": "1"}]


def fake_empty_download(conf, record, directory, progress):
    progress({"stage": "skipped", "reason": "no_operations", "client": 'ООО "ТЕСТ"', "client_id": "1"})
    progress({"stage": "downloaded", "activity_check_complete": True})
    return []


def upload(path):
    return {"path": str(path), "name": path.name}


def test_catchup_partitions_required_month_close_and_never_replays(month_engine, monkeypatch):
    _, _, frozen = month_engine
    monkeypatch.setattr(engine, "HANDLERS", {"glopro": fake_download})
    storage.set_setting("enabled_since:glopro", "2026-09-29T00:00:00+03:00")
    for step in range(3):
        engine.tick(frozen.now(ZONE))
        if step == 1:
            storage.init()
    records = storage.runs()
    assert len(records) == 3
    assert {r["run_date"]: [r["period_start"], r["period_end"]] for r in records} == {
        "2026-09-29": ["2026-09-25", "2026-09-28"],
        "2026-10-01": ["2026-09-29", "2026-09-30"],
        "2026-10-06": ["2026-10-01", "2026-10-05"],
    }
    assert all(r["status"] == "completed" and r["trigger"] == "schedule" for r in records)
    assert all(r["plan"]["ordinary_period"] == [r["period_start"], r["period_end"]] for r in records)


def test_new_manual_run_on_replaced_friday_is_rejected(month_engine):
    with pytest.raises(ValueError):
        engine.submit("glopro", "2026-10-02")
    assert storage.runs() == []


def test_month_close_import_skips_weekly_clients_before_period_validation(month_engine, tmp_path):
    ordinary = source_file(tmp_path / "ordinary.xlsx", tx_date=date(2026, 9, 30))
    china = weekly_file(tmp_path / "china.xlsx")
    nk = weekly_file(tmp_path / "nk.xlsx", 'ООО "НК АРТЭЛЬ"')
    record = engine.submit("glopro", "2026-10-01", "import", [upload(p) for p in (ordinary, china, nk)])
    assert record["status"] == "completed"
    assert record["company_periods"] == [{"client": 'ООО "ТЕСТ"', "period": ["2026-09-29", "2026-09-30"]}]
    assert len(record["schedule_skipped_clients"]) == 2
    assert record["failures"] == []
    assert "Китай" not in record["report"] and "НК АРТЭЛЬ" not in record["report"]


def test_next_tuesday_includes_first_october_and_preserves_weekly_period(month_engine, tmp_path):
    ordinary = source_file(tmp_path / "ordinary.xlsx", tx_date=date(2026, 10, 1))
    book = load_workbook(ordinary)
    book.active.append([date(2026, 10, 5), "ДТ ЭКТО", 100, 8000, 8300, -1])
    book.save(ordinary)
    china = weekly_file(tmp_path / "china.xlsx", start="29.09.2026", end="05.10.2026")
    record = engine.submit("glopro", "2026-10-06", "import", [upload(p) for p in (ordinary, china)])
    assert record["status"] == "completed"
    assert {r["client"]: r["period"] for r in record["company_periods"]} == {
        'ООО "ТЕСТ"': ["2026-10-01", "2026-10-05"],
        "Китай": ["2026-09-29", "2026-10-05"],
    }
    assert "200,000 л | 16 600,00 рублей" in record["report"]


@pytest.mark.parametrize("run_day,expected", [
    ("2026-10-01", {"1": ["2026-09-29", "2026-09-30"]}),
    ("2026-10-06", {
        "1": ["2026-10-01", "2026-10-05"],
        "78749": ["2026-09-29", "2026-10-05"],
        "78756": ["2026-09-29", "2026-10-05"],
    }),
    ("2026-11-03", {
        "78749": ["2026-10-27", "2026-11-02"],
        "78756": ["2026-10-27", "2026-11-02"],
    }),
])
def test_connector_uses_same_plan_before_opening_contracts(month_engine, tmp_path, run_day, expected):
    conf = config.get_operator("glopro")
    plan = engine.plan_run(conf, date.fromisoformat(run_day))
    connector = GloProConnector("synthetic", "synthetic")
    clients = [{"id": "78749", "name": "Китай"},
               {"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'},
               {"id": "1", "name": 'ООО "ТЕСТ"'}]
    events = []
    with patch.object(connector, "_browser_page", return_value=nullcontext(object())), \
         patch.object(connector, "_login"), \
         patch.object(connector, "_list_clients", return_value=clients), \
         patch.object(connector, "_contracts", return_value=[{"id": "10"}]) as contracts, \
         patch.object(connector, "_prepare_report", return_value={"checked": True}) as prepare, \
         patch.object(connector, "_download", return_value={}):
        sources = connector.download_reports(plan["period_start"], plan["period_end"], tmp_path,
            run_date=run_day, plan=plan, progress=events.append)
    assert {call.args[1]["id"] for call in contracts.call_args_list} == set(expected)
    assert {call.args[1]["id"]: [call.args[3].isoformat(), call.args[4].isoformat()]
            for call in prepare.call_args_list} == expected
    assert {item["client_id"]: [item["start"], item["end"]] for item in sources} == expected


@pytest.mark.parametrize("later_history_count", [0, 105])
def test_saved_plan_retry_survives_config_policy_change(month_engine, tmp_path, later_history_count):
    _, operators, _ = month_engine
    first = engine.submit("glopro", "2026-10-01", "import", [upload(
        source_file(tmp_path / "first.xlsx", tx_date=date(2026, 9, 30)))])
    before = storage.get_run(first["id"])
    for _ in range(later_history_count):
        unrelated = storage.create_run("unrelated", date(2026, 10, 1), date(2026, 9, 29), date(2026, 9, 30), "manual")
        unrelated["status"] = "completed"
        storage.save_run(unrelated)
    if later_history_count:
        assert first["id"] not in {record["id"] for record in storage.runs()}
    path = operators / "glopro.md"
    path.write_text(path.read_text().replace("  month_boundary: close_previous_month\n", "")
                    .replace("  month_boundary_from: '2026-09-29'\n", ""))
    retry = engine.submit("glopro", "2026-10-01", "import", [upload(
        source_file(tmp_path / "retry.xlsx", tx_date=date(2026, 9, 30)))])
    assert retry["status"] == "completed"
    assert retry["plan"] == first["plan"]
    assert [retry["period_start"], retry["period_end"]] == ["2026-09-29", "2026-09-30"]
    assert storage.get_run(first["id"]) == before


def test_old_cross_month_retry_keeps_legacy_period_and_history(month_engine, tmp_path):
    old = storage.create_run("glopro", date(2026, 10, 2), date(2026, 9, 29), date(2026, 10, 1), "schedule")
    old.update(status="failed", error="Synthetic old download failure")
    storage.save_run(old)
    before = json.loads(json.dumps(old))
    retry = engine.submit("glopro", "2026-10-02", "import", [upload(
        source_file(tmp_path / "retry.xlsx", tx_date=date(2026, 10, 1)))])
    assert retry["status"] == "completed"
    assert [retry["period_start"], retry["period_end"]] == ["2026-09-29", "2026-10-01"]
    assert storage.get_run(old["id"]) == before


def test_legacy_special_only_recovery_does_not_expand_ordinary_period(month_engine):
    old = storage.create_run("glopro", date(2026, 9, 22), date(2026, 9, 15), date(2026, 9, 21), "manual")
    old.update(status="completed", company_periods=[{"client": "Китай", "period": ["2026-09-15", "2026-09-21"]}])
    storage.save_run(old)
    plan = engine.plan_run(config.get_operator("glopro"), date(2026, 9, 22))
    assert plan["ordinary_period"] == ["2026-09-18", "2026-09-21"]
    assert plan["weekly_period"] == ["2026-09-15", "2026-09-21"]
    assert storage.get_run(old["id"]) == old


def test_legacy_overlap_blocks_new_month_close_without_rewriting_history(month_engine):
    old = storage.create_run("glopro", date(2026, 10, 2), date(2026, 9, 29), date(2026, 10, 1), "schedule")
    old["status"] = "completed"
    storage.save_run(old)
    with pytest.raises(ValueError):
        engine.submit("glopro", "2026-10-01")
    assert storage.runs() == [old]


@pytest.mark.parametrize("status", ["completed", "no_data"])
def test_successful_manual_download_same_scope_is_not_auto_repeated(month_engine, monkeypatch, status):
    _, _, frozen = month_engine
    frozen.current = datetime(2026, 10, 1, 10, tzinfo=ZONE)
    monkeypatch.setattr(engine, "HANDLERS", {"glopro": fake_download if status == "completed" else fake_empty_download})
    manual = engine.submit("glopro", "2026-10-01")
    assert manual["status"] == status
    storage.set_setting("enabled_since:glopro", "2026-10-01T00:00:00+03:00")
    engine.tick(frozen.now(ZONE))
    assert [r["id"] for r in storage.runs()] == [manual["id"]]


def test_partial_import_does_not_suppress_full_scheduled_download(month_engine, monkeypatch, tmp_path):
    _, _, frozen = month_engine
    frozen.current = datetime(2026, 10, 1, 10, tzinfo=ZONE)
    imported = engine.submit("glopro", "2026-10-01", "import", [upload(
        source_file(tmp_path / "partial.xlsx", tx_date=date(2026, 9, 30)))])
    monkeypatch.setattr(engine, "HANDLERS", {"glopro": fake_download})
    storage.set_setting("enabled_since:glopro", "2026-10-01T00:00:00+03:00")
    engine.tick(frozen.now(ZONE))
    records = storage.runs()
    assert len(records) == 2
    assert {r["trigger"] for r in records} == {"import", "schedule"}
    assert storage.get_run(imported["id"]) == imported


def test_manual_download_with_old_client_scope_does_not_suppress_schedule(month_engine, monkeypatch):
    _, operators, frozen = month_engine
    frozen.current = datetime(2026, 10, 1, 10, tzinfo=ZONE)
    path = operators / "glopro.md"
    path.write_text(path.read_text().replace("rules: {}", 'clients: [{id: "1", name: "ТЕСТ"}]\nrules: {}'))
    monkeypatch.setattr(engine, "HANDLERS", {"glopro": fake_download})
    engine.submit("glopro", "2026-10-01")
    path.write_text(path.read_text().replace('clients: [{id: "1", name: "ТЕСТ"}]', "clients: []"))
    storage.set_setting("enabled_since:glopro", "2026-10-01T00:00:00+03:00")
    engine.tick(frozen.now(ZONE))
    assert {r["trigger"] for r in storage.runs()} == {"manual", "schedule"}


def test_replaced_tuesday_import_processes_only_weekly_clients(month_engine, tmp_path):
    _, _, frozen = month_engine
    frozen.current = datetime(2026, 11, 3, 10, tzinfo=ZONE)
    china = weekly_file(tmp_path / "china.xlsx", start="27.10.2026", end="02.11.2026")
    ordinary = source_file(tmp_path / "ordinary.xlsx", tx_date=date(2026, 11, 2))
    record = engine.submit("glopro", "2026-11-03", "import", [upload(p) for p in (china, ordinary)])
    assert record["status"] == "completed"
    assert record["plan"]["ordinary_period"] is None
    assert record["plan"]["weekly_period"] == ["2026-10-27", "2026-11-02"]
    assert record["company_periods"] == [{"client": "Китай", "period": ["2026-10-27", "2026-11-02"]}]
    assert 'ООО "ТЕСТ"' not in record["report"]
    assert record["failures"] == []


def test_replaced_tuesday_catchup_separates_month_close_and_weekly_execution(month_engine, monkeypatch):
    _, _, frozen = month_engine
    frozen.current = datetime(2026, 11, 3, 10, tzinfo=ZONE)
    storage.set_setting("enabled_since:glopro", "2026-10-30T00:00:00+03:00")

    def download(conf, record, directory, progress):
        if record["run_date"] != "2026-11-03":
            return fake_download(conf, record, directory, progress)
        path = weekly_file(directory / "china.xlsx", start="27.10.2026", end="02.11.2026")
        return [{"path": str(path), "client": "Китай", "client_id": "78749"}]

    monkeypatch.setattr(engine, "HANDLERS", {"glopro": download})
    engine.tick(frozen.now(ZONE))
    records = {record["run_date"]: record for record in storage.runs()}
    assert set(records) == {"2026-10-30", "2026-11-01", "2026-11-03"}
    assert records["2026-11-01"]["plan"]["ordinary_period"] == ["2026-10-30", "2026-10-31"]
    assert records["2026-11-01"]["plan"]["weekly_period"] is None
    assert records["2026-11-03"]["plan"]["ordinary_period"] is None
    assert records["2026-11-03"]["company_periods"] == [{"client": "Китай", "period": ["2026-10-27", "2026-11-02"]}]
    assert all(record["status"] == "completed" for record in records.values())


@pytest.mark.parametrize("first_group", ["ordinary", "weekly"])
@pytest.mark.parametrize("remove_policy", [False, True])
def test_expanded_scope_uses_saved_calendar_periods_for_both_client_groups(month_engine, tmp_path, first_group, remove_policy):
    _, operators, _ = month_engine
    path = operators / "glopro.md"
    selected = ('clients: [{id: "1", name: "ТЕСТ"}]' if first_group == "ordinary"
                else 'clients: [{id: "78749", name: "Китай"}]')
    path.write_text(path.read_text().replace("rules: {}", selected + "\nrules: {}"))
    source = (source_file(tmp_path / "first.xlsx", tx_date=date(2026, 10, 1)) if first_group == "ordinary"
              else weekly_file(tmp_path / "first.xlsx", start="29.09.2026", end="05.10.2026"))
    first = engine.submit("glopro", "2026-10-06", "import", [upload(source)])
    before = storage.get_run(first["id"])
    assert first["status"] == "completed"
    assert first["plan"]["weekly_period" if first_group == "ordinary" else "ordinary_period"] is None
    markdown = path.read_text().replace(selected, "clients: []")
    if remove_policy:
        markdown = markdown.replace("  month_boundary: close_previous_month\n", "").replace("  month_boundary_from: '2026-09-29'\n", "")
    path.write_text(markdown)
    plan = engine.plan_run(config.get_operator("glopro"), date(2026, 10, 6))
    assert plan["ordinary_period"] == ["2026-10-01", "2026-10-05"]
    assert plan["weekly_period"] == ["2026-09-29", "2026-10-05"]
    ordinary = source_file(tmp_path / "expanded-ordinary.xlsx", tx_date=date(2026, 10, 1))
    china = weekly_file(tmp_path / "expanded-china.xlsx", start="29.09.2026", end="05.10.2026")
    expanded = engine.submit("glopro", "2026-10-06", "import", [upload(p) for p in (ordinary, china)])
    assert expanded["status"] == "completed"
    assert {item["client"]: item["period"] for item in expanded["company_periods"]} == {
        'ООО "ТЕСТ"': ["2026-10-01", "2026-10-05"],
        "Китай": ["2026-09-29", "2026-10-05"],
    }
    assert storage.get_run(first["id"]) == before


@pytest.mark.parametrize("status", ["completed", "failed", "needs_review", "no_data"])
def test_next_run_omits_attempted_schedule_when_same_day_time_changes(month_engine, status):
    _, operators, frozen = month_engine
    frozen.current = datetime(2026, 10, 1, 10, tzinfo=ZONE)
    attempted = storage.create_run("glopro", date(2026, 10, 1), date(2026, 9, 29), date(2026, 9, 30), "schedule")
    attempted["status"] = status
    storage.save_run(attempted)
    path = operators / "glopro.md"
    path.write_text(path.read_text().replace("time: '09:00'", "time: '12:00'"))
    assert engine.next_scheduled_run(config.get_operator("glopro"), frozen.now(ZONE)) == "2026-10-06T12:00:00+03:00"
    assert storage.get_run(attempted["id"]) == attempted


def test_legacy_friday_retry_remains_available_after_days_change(month_engine):
    _, operators, _ = month_engine
    old = storage.create_run("glopro", date(2026, 9, 25), date(2026, 9, 22), date(2026, 9, 24), "schedule")
    old["status"] = "failed"
    storage.save_run(old)
    path = operators / "glopro.md"
    markdown = path.read_text().replace("  days: [tue, fri]", "  days: [tue]")
    markdown = markdown.replace("  month_boundary: close_previous_month\n", "").replace("  month_boundary_from: '2026-09-29'\n", "")
    path.write_text(markdown)
    plan = engine.plan_run(config.get_operator("glopro"), date(2026, 9, 25))
    assert plan["ordinary_period"] == ["2026-09-22", "2026-09-24"]
    assert plan["weekly_period"] is None
    assert storage.get_run(old["id"]) == old
