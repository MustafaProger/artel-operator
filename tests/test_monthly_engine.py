"""Monthly downloads use synthetic portal manifests and reconciled XLSX only."""
from contextlib import nullcontext
from datetime import date, datetime, timezone
import json
from unittest.mock import patch
import zipfile

import pytest

from operator_app import config, engine, storage
from operator_app.glopro import GloProConnector
from test_month_schedule_engine import month_engine
from test_engine import isolated_engine
from test_weekly import weekly_file


CLIENTS = [{"id": "1", "name": 'ООО "ТЕСТ"'}, {"id": "78749", "name": "Китай"},
           {"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'}, {"id": "4", "name": "Без операций"}]


def monthly_download(conf, record, directory, progress):
    assert conf.get("clients") == conf.get("excluded_clients") == []
    period = [record["period_start"], record["period_end"]]
    progress({"stage": "clients", "clients": CLIENTS, "total": len(CLIENTS)})
    sources = []
    for client in CLIENTS:
        ident, name = client["id"], client["name"]
        progress({"stage": "contracts", "client_id": ident, "contract_ids": [ident]})
        if ident == "4":
            progress({"stage": "skipped", "reason": "no_operations", "client": name,
                      "client_id": ident, "contract_id": ident, "period": period,
                      "evidence": {"has_operations": False}})
            continue
        path = weekly_file(directory / f"{ident}.xlsx", client=name, start="01.09.2026", end="30.09.2026")
        sources.append({"path": str(path), "client": name, "client_id": ident, "contract_id": ident,
                        "start": period[0], "end": period[1]})
    progress({"stage": "downloaded", "activity_check_complete": True})
    return sources


@pytest.mark.parametrize("month,first,last", [("2024-02", "2024-02-01", "2024-02-29"),
    ("2025-02", "2025-02-01", "2025-02-28"), ("2025-12", "2025-12-01", "2025-12-31")])
def test_full_calendar_month_and_identical_company_periods(month_engine, month, first, last):
    plan = engine.monthly_plan(config.get_operator("glopro"), month)
    assert plan["ordinary_period"] == plan["weekly_period"] == [first, last]
    assert plan["can_run"] and not plan["future"]


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-9", "0000-01", "9999-12", "", "2026-09-01"])
def test_bad_month_rejected_before_enqueuing(month_engine, month):
    with pytest.raises(ValueError):
        engine.submit(report_month=month)
    assert storage.runs() == []


def test_month_completion_uses_configured_timezone(month_engine):
    conf = config.get_operator("glopro")
    assert not engine.monthly_plan(conf, "2026-09", datetime(2026, 9, 30, 20, 59, tzinfo=timezone.utc))["can_run"]
    assert engine.monthly_plan(conf, "2026-09", datetime(2026, 9, 30, 21, tzinfo=timezone.utc))["can_run"]


@pytest.mark.parametrize("kwargs", [{"run_date": "2026-10-01"}, {"trigger": "schedule"}, {"uploads": []},
    {"period_start": "2026-09-01", "period_end": "2026-09-30"}, {"edition_key": "monthly"}])
def test_monthly_rejects_conflicting_modes(month_engine, kwargs):
    with pytest.raises(ValueError, match="Месячный"):
        engine.submit(report_month="2026-09", **kwargs)
    assert storage.runs() == []


@pytest.mark.parametrize("month", ["2026-10", "2026-11"])
def test_incomplete_month_rejected_before_download(month_engine, month):
    with pytest.raises(ValueError, match="завершённый"):
        engine.submit(report_month=month)
    assert storage.runs() == []


def test_full_month_includes_weekly_zeroes_and_keeps_activation_untouched(month_engine, monkeypatch, tmp_path):
    data, operators, _ = month_engine
    notes = tmp_path / "notes"
    notes.mkdir()
    original = notes / "Активация — 01.10.2026.md"
    original.write_text("Существующая активация")
    path = operators / "glopro.md"
    path.write_text(path.read_text().replace("rules: {}", f"rules: {{}}\nclients: [Только одна]\nexcluded_clients: [{{id: '78749', name: Китай}}]\nobsidian_output: '{notes}'"))
    monkeypatch.setitem(engine.HANDLERS, "glopro", monthly_download)
    record = engine.submit(report_month="2026-09")
    assert record["status"] == "completed", record.get("error")
    assert record["client_count"] == 4
    assert record["total_customer_amount"] == "333.30"
    assert record["total_litres"] == "3.015"
    assert original.read_text() == "Существующая активация"
    assert len(list(notes.iterdir())) == 1
    assert record["scope"] == {"clients": [], "excluded_clients": []}
    root = data / "runs" / record["id"]
    assert not list(root.glob("Активация*"))
    assert (root / "Месячный отчёт GloPro — 2026-09.xlsx").exists()
    with zipfile.ZipFile(next(root.glob("Готовый комплект*.zip"))) as bundle:
        assert bundle.testzip() is None
        assert "Месячный отчёт GloPro — 2026-09.xlsx" in bundle.namelist()
    audit = json.loads((root / "Проверка расчётов.json").read_text())
    assert len(audit["coverage"]) == 4
    assert len(audit["sources"]) == 3


def test_monthly_history_does_not_replace_calendar_plan_or_schedule(month_engine, monkeypatch):
    monkeypatch.setitem(engine.HANDLERS, "glopro", monthly_download)
    conf = config.get_operator("glopro")
    expected = engine.plan_run(conf, date(2026, 10, 1))
    record = engine.submit(report_month="2026-09")
    assert record["status"] == "completed", record.get("error")
    assert engine.plan_run(conf, date(2026, 10, 1)) == expected
    assert not engine._schedule_satisfied(conf, date(2026, 10, 1))
    assert engine.plan_run(conf, date(2026, 10, 6))["ordinary_period"] == ["2026-10-01", "2026-10-05"]
    repeated = engine.submit(report_month="2026-09")
    assert repeated["plan"] == record["plan"]
    assert repeated["total_customer_amount"] == record["total_customer_amount"]


@pytest.mark.parametrize("fault", ["missing_source", "duplicate_source", "no_completion", "no_manifest", "no_contract", "wrong_empty_period", "missing_evidence"])
def test_incomplete_coverage_never_produces_ready_monthly_report(month_engine, monkeypatch, fault):
    data, _, _ = month_engine
    def incomplete(conf, record, directory, progress):
        events = []
        sources = monthly_download(conf, record, directory, events.append)
        if fault == "missing_source":
            sources.pop()
        elif fault == "duplicate_source":
            sources.append(sources[0])
        for event in events:
            if fault == "no_completion" and event.get("activity_check_complete"):
                continue
            if fault == "no_manifest" and event.get("stage") == "clients":
                continue
            if fault == "no_contract" and event.get("client_id") == "4" and event.get("stage") == "contracts":
                event["contract_ids"] = []
            if event.get("reason") == "no_operations":
                if fault == "wrong_empty_period":
                    event["period"] = ["2026-09-29", "2026-09-30"]
                if fault == "missing_evidence":
                    event.pop("evidence")
            progress(event)
        return sources
    monkeypatch.setitem(engine.HANDLERS, "glopro", incomplete)
    record = engine.submit(report_month="2026-09")
    assert record["status"] == "failed"
    assert record["report"] == ""
    root = data / "runs" / record["id"]
    assert not list(root.glob("Месячный отчёт*"))
    assert not list(root.glob("Готовый комплект*"))


def test_all_verified_empty_still_has_full_month_summary(month_engine, monkeypatch):
    def download(conf, record, directory, progress):
        progress({"stage": "clients", "clients": CLIENTS})
        for client in CLIENTS:
            progress({"stage": "contracts", "client_id": client["id"], "contract_ids": ["a"]})
            progress({"stage": "skipped", "reason": "no_operations", "client": client["name"],
                      "client_id": client["id"], "contract_id": "a", "period": [record["period_start"], record["period_end"]],
                      "evidence": {"has_operations": False}})
        progress({"stage": "downloaded", "activity_check_complete": True})
        return []
    monkeypatch.setitem(engine.HANDLERS, "glopro", download)
    record = engine.submit(report_month="2026-09")
    assert record["status"] == "no_data", record.get("error")
    assert record["client_count"] == 4
    assert record["total_customer_amount"] == "0.00"
    assert record["report"]
    assert any(f["name"].endswith(".xlsx") for f in record["files"])


@pytest.mark.parametrize("name,expected", [('ООО "ТЕСТ"', "180.08"), ('ООО "ПЕРФЕКТ"', "204.01"), ('ООО "НК АРТЭЛЬ"', "180.08")])
def test_multiple_contracts_and_empty_contract_round_only_after_merge(month_engine, monkeypatch, name, expected):
    def download(conf, record, directory, progress):
        progress({"stage": "clients", "clients": [{"id": "1", "name": name}]})
        progress({"stage": "contracts", "client_id": "1", "contract_ids": ["a", "b", "c"]})
        period = [record["period_start"], record["period_end"]]
        progress({"stage": "skipped", "reason": "no_operations", "client": name, "client_id": "1",
                  "contract_id": "c", "period": period, "evidence": {"has_operations": False}})
        sources = []
        for ident in ("a", "b"):
            path = weekly_file(directory / f"{ident}.xlsx", client=name, start="01.09.2026", end="30.09.2026",
                cards=[("1", "Тестовый держатель", [("ДТ", "1.0004", "100.004", "90.04")])])
            sources.append({"path": str(path), "client": name, "client_id": "1", "contract_id": ident,
                            "start": period[0], "end": period[1]})
        progress({"stage": "downloaded", "activity_check_complete": True})
        return sources
    monkeypatch.setitem(engine.HANDLERS, "glopro", download)
    record = engine.submit(report_month="2026-09")
    assert record["status"] == "completed", record.get("error")
    assert record["client_count"] == 1
    assert record["total_litres"] == "2.001"
    assert record["total_customer_amount"] == expected
    assert record["companies"][0]["client_id"] == "1"


def test_connector_monthly_uses_same_period_and_emits_contract_manifest(month_engine, tmp_path):
    conf = config.get_operator("glopro")
    plan = engine.monthly_plan(conf, "2026-09")
    connector = GloProConnector("synthetic", "synthetic")
    events = []
    with patch.object(connector, "_browser_page", return_value=nullcontext(object())), \
         patch.object(connector, "_login"), patch.object(connector, "_list_clients", return_value=CLIENTS), \
         patch.object(connector, "_contracts", return_value=[{"id": "10"}, {"id": "11"}]), \
         patch.object(connector, "_prepare_report", return_value={"checked": True}) as prepare, \
         patch.object(connector, "_download", return_value={}):
        sources = connector.download_reports(plan["period_start"], plan["period_end"], tmp_path,
            run_date=plan["run_date"], plan=plan, progress=events.append)
    assert len(sources) == 8
    assert all(call.args[3:] == (date(2026, 9, 1), date(2026, 9, 30)) for call in prepare.call_args_list)
    assert next(e for e in events if e["stage"] == "clients")["clients"]
    assert len([e for e in events if e["stage"] == "contracts"]) == 4
    assert len(engine._monthly_coverage({"events": events, "period_start": plan["period_start"], "period_end": plan["period_end"]}, sources)) == 4
