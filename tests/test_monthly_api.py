"""Monthly API/UI checks use temporary history and no provider requests."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from operator_app import engine, main, storage
from test_api import isolated_api


class OctoberNow(datetime):
    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 10, 7, 8, tzinfo=timezone.utc)
        return current.astimezone(tz) if tz else current.replace(tzinfo=None)


@pytest.fixture
def monthly_api(isolated_api, monkeypatch):
    client, data, operators = isolated_api
    monkeypatch.setattr(main, "datetime", OctoberNow)
    monkeypatch.setattr(engine, "datetime", OctoberNow)
    monkeypatch.setattr(storage, "now_iso", lambda: "2026-10-07T08:00:00+00:00")
    monkeypatch.setattr(main.yandex_connection, "status", lambda: {"configured": False})
    return client, data, operators


@pytest.mark.parametrize("month,first,last,available", [
    ("2026-09", "2026-09-01", "2026-09-30", True),
    ("2026-10", "2026-10-01", "2026-10-31", False),
    ("2026-11", "2026-11-01", "2026-11-30", False),
    ("2024-02", "2024-02-01", "2024-02-29", True),
    ("2025-12", "2025-12-01", "2025-12-31", True),
])
def test_monthly_preview_is_read_only_and_covers_calendar_month(monthly_api, monkeypatch, month, first, last, available):
    client, _, _ = monthly_api
    monkeypatch.setattr(engine, "submit", lambda *args, **kwargs: pytest.fail("Preview must not start a run"))
    response = client.get("/api/operators/glopro/monthly-plan", params={"month": month})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["kind"] == "monthly_summary" and plan["month"] == month
    assert plan["period_start"] == first and plan["period_end"] == last
    assert plan["ordinary_period"] == plan["weekly_period"] == [first, last]
    assert plan["can_run"] is available and plan["future"] is not available
    assert storage.runs() == []


@pytest.mark.parametrize("month", ["", "2026-13", "2026-00", "2026-9", "2026-09-01", "not-a-month"])
def test_invalid_month_preview_does_not_create_history(monthly_api, month):
    client, _, _ = monthly_api
    assert client.get("/api/operators/glopro/monthly-plan", params={"month": month}).status_code == 400
    assert storage.runs() == []


def test_monthly_preview_rejects_other_provider(monthly_api):
    client, _, operators = monthly_api
    (operators / "yandex.md").write_text((operators / "glopro.md").read_text()
        .replace("id: glopro", "id: yandex").replace("kind: glopro", "kind: yandex\ncompany: Synthetic")
        .replace("days: [tue, fri]", "days: [tue]"))
    response = client.get("/api/operators/yandex/monthly-plan?month=2026-09")
    assert response.status_code == 400
    assert "только для GloPro" in response.json()["detail"]
    assert storage.runs() == []


def test_monthly_post_keeps_month_distinct_from_activation_date(monthly_api, monkeypatch):
    client, _, _ = monthly_api
    captured = []
    monkeypatch.setattr(engine, "submit", lambda *args, **kwargs: captured.append((args, kwargs)) or {"id": "synthetic", "status": "queued"})
    response = client.post("/api/run", json={"operator_id": "glopro", "report_month": "2026-09"})
    assert response.status_code == 202
    args, kwargs = captured.pop()
    assert args == ("glopro", None)
    assert kwargs["report_month"] == "2026-09"
    assert kwargs["period_start"] is None and kwargs["period_end"] is None and kwargs["edition_key"] is None
    assert storage.runs() == []


@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="Opt-in offline monthly UI regression")
@pytest.mark.parametrize("width", [1280, 390])
def test_monthly_ui_selection_history_and_retry(monthly_api, monkeypatch, width):
    from playwright.sync_api import expect, sync_playwright

    client, _, operators = monthly_api
    # A non-GloPro operator verifies the mode never leaks to another provider.
    (operators / "yandex.md").write_text((operators / "glopro.md").read_text()
        .replace("id: glopro", "id: yandex").replace("kind: glopro", "kind: yandex\ncompany: Synthetic")
        .replace("days: [tue, fri]", "days: [tue]"))
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    submissions, errors = [], []

    def submit(ident, run_date, **kwargs):
        submissions.append({"operator_id": ident, "run_date": run_date, **kwargs})
        plan = engine.monthly_plan(main.get_operator(ident), kwargs["report_month"], now=OctoberNow.now(timezone.utc))
        record = storage.create_run(ident, plan["run_date"], plan["period_start"], plan["period_end"], "manual", plan=plan, scope={"clients": [], "excluded_clients": []})
        record.update(status="completed", report="Синтетический месячный отчёт", source_count=1, client_count=1,
                      total_litres="1234.500", total_customer_amount="98765.43")
        storage.save_run(record)
        return record

    monkeypatch.setattr(engine, "submit", submit)
    held = []
    hold_once = {"value": False}

    def fulfill(route):
        request = route.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == "testserver"
        if hold_once["value"] and parsed.query == "month=2026-08":
            hold_once["value"] = False
            held.append(route)
            return
        if request.method == "POST":
            assert parsed.path == "/api/run"
            response = client.post(parsed.path, json=json.loads(request.post_data))
        else:
            assert request.method == "GET"
            response = client.get(parsed.path + ("?" + parsed.query if parsed.query else ""))
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": 950}, locale="ru-RU", reduced_motion="reduce")
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", fulfill)
            page.goto("http://testserver/")
            page.locator('[data-action="manual-run"]').first.click()
            page.locator("#run-mode").select_option("monthly")
            expect(page.locator("#run-month")).to_have_value("2026-09")
            expect(page.locator("#run-month")).to_have_attribute("max", "2026-09")
            expect(page.locator("#run-date")).to_be_hidden()
            expect(page.locator("#run-period-preview")).to_contain_text("01.09 — 30.09.2026")
            expect(page.locator("#run-period-preview")).to_contain_text("сумма к оплате нам")
            expect(page.locator("#run-submit")).to_be_enabled()
            if os.environ.get("MONTHLY_UI_SCREENSHOTS"):
                screenshots = Path(os.environ["MONTHLY_UI_SCREENSHOTS"])
                screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / f"monthly-modal-{width}.png"), animations="disabled")
            # Invalid future input is rejected by the preview as well as max.
            page.locator("#run-month").fill("2026-10")
            expect(page.locator("#run-period-preview")).to_contain_text("Месяц ещё не завершён")
            expect(page.locator("#run-submit")).to_be_disabled()
            # Slow monthly response must not overwrite a different selected mode.
            hold_once["value"] = True
            with page.expect_request("**/monthly-plan?month=2026-08"):
                page.locator("#run-month").fill("2026-08")
            page.locator("#run-operator").select_option("yandex")
            expect(page.locator("#run-mode-field")).to_be_hidden()
            expect(page.locator("#run-month")).to_be_hidden()
            expect(page.locator("#run-date")).to_be_visible()
            expect(page.locator("#run-period-preview")).to_contain_text("Яндекс:")
            assert len(held) == 1
            with page.expect_response("**/monthly-plan?month=2026-08"):
                fulfill(held.pop())
            expect(page.locator("#run-period-preview")).to_contain_text("Яндекс:")
            page.locator("#run-operator").select_option("glopro")
            page.locator("#run-mode").select_option("monthly")
            expect(page.locator("#run-submit")).to_be_enabled()
            assert page.locator("#run-dialog").evaluate("el => el.scrollWidth <= el.clientWidth")
            page.locator("#run-submit").click()
            expect(page.locator("#run-dialog")).not_to_be_visible()
            expect(page.locator("#all-runs")).to_contain_text("Месячный отчёт · все фирмы")
            assert len(submissions) == 1
            assert submissions[0]["report_month"] == "2026-08" and submissions[0]["run_date"] is None
            page.locator("#all-runs [data-run-details]").click()
            expect(page.locator("#details-title")).to_contain_text("Месячный отчёт")
            expect(page.locator("#run-details")).to_contain_text("август 2026")
            expect(page.locator(".monthly-totals")).to_contain_text("Всего 1 234,500 л · К оплате нам 98 765,43 ₽")
            if os.environ.get("MONTHLY_UI_SCREENSHOTS"):
                page.screenshot(path=str(screenshots / f"monthly-details-{width}.png"), animations="disabled")
            page.locator("[data-repeat-monthly]").click()
            expect(page.locator("#run-mode")).to_have_value("monthly")
            expect(page.locator("#run-month")).to_have_value("2026-08")
            expect(page.locator("#run-submit")).to_be_enabled()
            page.locator("#run-submit").click()
            expect(page.locator("#run-dialog")).not_to_be_visible()
            assert len(submissions) == 2
            assert submissions[1]["report_month"] == "2026-08" and submissions[1]["run_date"] is None
            assert errors == []
        finally:
            browser.close()
