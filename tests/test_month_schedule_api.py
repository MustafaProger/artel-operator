"""Calendar previews are read-only and match the persisted execution plan."""
from datetime import date, datetime, timezone
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from operator_app import config, engine, main, storage


class ExampleNow(datetime):
    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 9, 29, 7, 0, tzinfo=timezone.utc)
        return current.astimezone(tz) if tz else current.replace(tzinfo=None)


@pytest.fixture
def calendar_api(tmp_path, monkeypatch):
    data, operators = tmp_path / "data", tmp_path / "operators"
    operators.mkdir()
    for module in (config, storage, engine, main):
        monkeypatch.setattr(module, "DATA", data)
    for module in (config, main):
        monkeypatch.setattr(module, "OPERATORS", operators)
    monkeypatch.setattr(main, "datetime", ExampleNow)
    monkeypatch.setattr(storage, "credentials", lambda: None)
    monkeypatch.setattr(main.yandex_connection, "status", lambda: {"configured": False})
    monkeypatch.setattr(engine, "submit", lambda *a, **kw: pytest.fail("Preview must never enqueue a report"))
    (operators / "glopro.md").write_text("""---
id: glopro
name: GloPro
kind: glopro
enabled: true
schedule:
  days: [tue, fri]
  time: '07:00'
  timezone: Europe/Moscow
  month_boundary: close_previous_month
  month_boundary_from: '2026-09-29'
clients: []
excluded_clients: []
rules: {}
---
Test calendar.
""", encoding="utf-8")
    storage.init()
    # Deliberately omit lifespan: no scheduler, credentials or external calls.
    client = TestClient(main.app)
    yield client, operators
    client.close()


@pytest.mark.parametrize("run_day,ordinary,weekly,kind,future", [
    ("2026-09-29", ["2026-09-25", "2026-09-28"], ["2026-09-22", "2026-09-28"], "regular", False),
    ("2026-10-01", ["2026-09-29", "2026-09-30"], None, "month_close", True),
    ("2026-10-06", ["2026-10-01", "2026-10-05"], ["2026-09-29", "2026-10-05"], "regular", True),
    ("2026-11-01", ["2026-10-30", "2026-10-31"], None, "month_close", True),
    ("2026-11-03", None, ["2026-10-27", "2026-11-02"], "weekly_only", True),
])
def test_preview_uses_common_model_without_running(calendar_api, run_day, ordinary, weekly, kind, future):
    client, _ = calendar_api
    response = client.get(f"/api/operators/glopro/plan?run_date={run_day}")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["run_date"] == run_day
    assert plan["ordinary_period"] == ordinary
    assert plan["weekly_period"] == weekly
    assert plan["kind"] == kind
    assert plan["future"] is future
    assert plan["can_run"] is not future
    assert storage.runs() == []


def test_cancelled_friday_preview_explains_move_and_next_date(calendar_api):
    client, _ = calendar_api
    response = client.get("/api/operators/glopro/plan?run_date=2026-10-02")
    assert response.status_code == 400
    message = response.json()["detail"]
    assert "2026-10-01" in message and "2026-10-06" in message
    assert storage.runs() == []


def test_state_has_same_next_and_default_plans_as_preview(calendar_api):
    client, _ = calendar_api
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    operator = response.json()["operators"][0]
    assert operator["next_run"] == "2026-10-01T07:00:00+03:00"
    assert operator["next_plan"] == client.get("/api/operators/glopro/plan?run_date=2026-10-01").json()
    assert operator["latest_plan"] == client.get("/api/operators/glopro/plan?run_date=2026-09-29").json()


def test_completed_early_manual_run_is_not_advertised_as_next(calendar_api, monkeypatch):
    client, operators = calendar_api

    class BeforeClosingHour(datetime):
        @classmethod
        def now(cls, tz=None):
            current = cls(2026, 10, 1, 2, 30, tzinfo=timezone.utc)
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", BeforeClosingHour)
    conf = config.get_operator("glopro")
    plan = config.run_plan(conf, date(2026, 10, 1))
    record = storage.create_run("glopro", date(2026, 10, 1), date(2026, 9, 29), date(2026, 9, 30),
                                "manual", plan=plan, scope={"clients": [], "excluded_clients": []})
    record["status"] = "completed"
    storage.save_run(record)
    operator = client.get("/api/state").json()["operators"][0]
    assert operator["next_run"] == "2026-10-06T07:00:00+03:00"
    assert operator["next_plan"]["ordinary_period"] == ["2026-10-01", "2026-10-05"]
    assert operator["latest_plan"]["run_date"] == "2026-10-01"
    saved = client.put("/api/operators/glopro", json={"markdown": (operators / "glopro.md").read_text()})
    assert saved.status_code == 200
    assert saved.json()["next_run"] == operator["next_run"]


def test_saved_legacy_friday_is_preserved_and_overlapping_new_plan_is_rejected(calendar_api):
    client, _ = calendar_api
    record = storage.create_run("glopro", date(2026, 10, 2), date(2026, 9, 29), date(2026, 10, 1), "manual")
    record["status"] = "completed"
    storage.save_run(record)
    response = client.get("/api/operators/glopro/plan?run_date=2026-10-02")
    assert response.status_code == 200, response.text
    assert response.json()["ordinary_period"] == ["2026-09-29", "2026-10-01"]
    assert response.json()["from_history"] is True
    conflict = client.get("/api/operators/glopro/plan?run_date=2026-10-06")
    assert conflict.status_code == 400
    assert "2026-10-02" in conflict.json()["detail"]
    # The same conflict is visible on the dashboard without hiding all state.
    state = client.get("/api/state")
    assert state.status_code == 200
    assert state.json()["operators"][0]["next_plan"]["can_run"] is False
    assert "пересекается" in state.json()["operators"][0]["next_plan"]["error"]
    assert storage.runs() == [record]


def test_policy_is_not_applied_to_an_operator_without_opt_in(calendar_api):
    client, operators = calendar_api
    configured = (operators / "glopro.md").read_text()
    legacy = configured.replace("id: glopro", "id: legacy").replace("  month_boundary: close_previous_month\n", "").replace("  month_boundary_from: '2026-09-29'\n", "")
    (operators / "legacy.md").write_text(legacy)
    friday = client.get("/api/operators/legacy/plan?run_date=2026-10-02")
    assert friday.status_code == 200
    assert friday.json()["ordinary_period"] == ["2026-09-29", "2026-10-01"]
    assert client.get("/api/operators/legacy/plan?run_date=2026-10-01").status_code == 400


@pytest.mark.parametrize("value", ["not-a-date", "2026-02-30", "2026-10-04"])
def test_invalid_preview_never_creates_history(calendar_api, value):
    client, _ = calendar_api
    assert client.get("/api/operators/glopro/plan", params={"run_date": value}).status_code == 400
    assert storage.runs() == []


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in offline calendar UI regression")
def test_browser_shared_preview_and_stale_responses(calendar_api):
    """Serve the real UI/API in memory; every browser request stays offline."""
    from playwright.sync_api import expect, sync_playwright

    client, _ = calendar_api
    writes, errors, held = [], [], []
    hold_once = {"value": False}

    def fulfill(route):
        parsed = urlsplit(route.request.url)
        assert parsed.netloc == "testserver"
        if route.request.method != "GET":
            writes.append(route.request.url)
            route.abort()
            return
        if hold_once["value"] and parsed.query == "run_date=2026-10-01":
            hold_once["value"] = False
            held.append(route)
            return
        response = client.get(parsed.path + ("?" + parsed.query if parsed.query else ""))
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", fulfill)
            page.goto("http://testserver/")
            expect(page.locator("#next-run-period")).to_contain_text("29.09 — 30.09.2026")
            expect(page.locator("#next-run-period")).to_contain_text("Закрытие месяца")
            page.locator('[data-action="manual-run"]').first.click()
            expect(page.locator("#run-date")).to_have_value("2026-09-29")
            expect(page.locator("#run-submit")).to_be_enabled()
            page.locator("#run-date").fill("2026-10-01")
            expect(page.locator("#run-period-preview")).to_contain_text("29.09 — 30.09.2026")
            expect(page.locator("#run-period-preview")).to_contain_text("будущая дата")
            expect(page.locator("#run-submit")).to_be_disabled()
            page.locator("#run-date").fill("2026-10-02")
            expect(page.locator("#run-period-preview")).to_contain_text("2026-10-01")
            expect(page.locator("#run-period-preview")).to_contain_text("2026-10-06")
            expect(page.locator("#run-submit")).to_be_disabled()
            page.locator("#run-date").fill("2026-10-06")
            expect(page.locator("#run-period-preview")).to_contain_text("Обычные фирмы: 01.10 — 05.10.2026")

            # An older slow request arrives after the most recent valid preview.
            hold_once["value"] = True
            with page.expect_request("**/plan?run_date=2026-10-01"):
                page.locator("#run-date").fill("2026-10-01")
            expect(page.locator("#run-submit")).to_be_disabled()
            page.locator("#run-date").fill("2026-09-29")
            expect(page.locator("#run-period-preview")).to_contain_text("Обычные фирмы: 25.09 — 28.09.2026")
            expect(page.locator("#run-submit")).to_be_enabled()
            assert len(held) == 1
            with page.expect_response("**/plan?run_date=2026-10-01"):
                fulfill(held.pop())
            expect(page.locator("#run-period-preview")).to_contain_text("Обычные фирмы: 25.09 — 28.09.2026")
            expect(page.locator("#run-submit")).to_be_enabled()
            page.locator('#run-dialog [data-close="run-dialog"]').first.click()
            page.locator('[data-action="import"]').first.click()
            page.locator("#import-date").fill("2026-10-06")
            expect(page.locator("#import-period-preview")).to_contain_text("Обычные фирмы: 01.10 — 05.10.2026")
            expect(page.locator("#import-submit")).to_be_disabled()
            assert writes == [] and errors == [] and storage.runs() == []
        finally:
            browser.close()
