"""Opt-in browser regression using a synthetic portal and no external requests."""
from datetime import date
import io
import os
import time
from urllib.parse import parse_qs, urlparse
import zipfile

import pytest
from playwright.sync_api import sync_playwright

from operator_app.glopro import GloProConnector, _browser_channel


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_TESTS") != "1",
    reason="Set RUN_BROWSER_TESTS=1 to run the offline Chrome/Chromium regression",
)


PORTAL_FIXTURE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<style>
  .custom-control-input { display: none; }
  .custom-radio { display: inline-block; padding: 12px; cursor: pointer; }
  #reports { display: none; }
</style>
<select name="contracts_list"><option value="112042">Договор</option></select>
<div class="ajax_contract_block">
  <a href="#account" ajax_tab onclick="showTab('account', this)">Счёт</a>
  <a href="#reports" ajax_tab onclick="showTab('reports', this)">Отчёты</a>
  <div class="tab-content">
    <section id="account">Последние транзакции: 21.09.2026</section>
    <section id="reports"><div id="reports2">
      <select class="report_select">
        <option value="19">Транзакционный отчет со скидкой</option>
      </select>
      <div class="report_template_block" report="19">
        <input name="period_start" value="2026-01-01">
        <input name="period_end" value="2026-01-02">
        <div class="combobox_outer">
          <input name="client_choose_single" value="Фирма">
          <input type="hidden" name="combobox_value" value="83706">
        </div>
        <div class="combobox_outer">
          <input name="contract_choose_single-19_0" value="Договор">
          <input type="hidden" name="combobox_value" value="112042">
        </div>
        <span class="custom-control custom-radio" format="xls" onclick="chooseFormat(this)">
          <input type="radio" name="format" class="custom-control-input" checked>
          <span class="custom-control-label">XLS</span>
        </span>
        <span class="custom-control custom-radio" format="xlsx" onclick="chooseFormat(this)">
          <input type="radio" name="format" class="custom-control-input">
          <span class="custom-control-label">XLSX</span>
        </span>
        <span onclick="generateReport($(this))">Сформировать</span>
        <div class="ordered_reports"></div>
      </div>
    </div></section>
  </div>
</div>
<script>
  window.$ = element => element;
  window.generateCount = 0;
  function showTab(id, link) {
    document.querySelectorAll('.ajax_contract_block a').forEach(a => a.classList.remove('active'));
    link.classList.add('active');
    for (const name of ['account', 'reports']) {
      document.getElementById(name).style.display = name === id ? 'block' : 'none';
    }
  }
  function chooseFormat(control) {
    control.querySelector('input').checked = true;
  }
  function generateReport(button) {
    window.generateCount++;
    const form = button.closest('.report_template_block');
    const contract = form.querySelector('input[name^="contract_choose_single"]');
    const client = form.querySelector('input[name^="client_choose_single"]');
    const value = input => input.closest('.combobox_outer').querySelector('input[name="combobox_value"]').value;
    const params = new URLSearchParams({
      build: '1', report_id: form.getAttribute('report'),
      period_start: form.querySelector('input[name="period_start"]').value,
      period_end: form.querySelector('input[name="period_end"]').value,
      format: form.querySelector('input[name="format"]:checked').parentElement.getAttribute('format'),
      'additional[0][name]': contract.name,
      'additional[0][value]': value(contract),
      'additional[0][weight]': '0',
      'additional[0][data][parent][name]': client.name,
      'additional[0][data][parent][value]': value(client)
    });
    setTimeout(() => {
      form.querySelector('.ordered_reports').innerHTML =
        '<a href="/upload/report/old-one.xlsx">Старый отчёт 1</a>' +
        '<a href="/upload/report/old-two.xlsx">Старый отчёт 2</a>';
    }, 30);
    window.open('/reports/generate/?' + params.toString());
  }
</script></html>"""


def test_hidden_xlsx_radio_and_late_history_do_not_break_native_download(tmp_path, monkeypatch):
    workbook = io.BytesIO()
    with zipfile.ZipFile(workbook, "w") as archive:
        for name in ("[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml"):
            archive.writestr(name, "<test/>")
    workbook_bytes = workbook.getvalue()
    requests = []

    def route(request_route):
        parsed = urlparse(request_route.request.url)
        requests.append((parsed.netloc, parsed.path, parse_qs(parsed.query)))
        if parsed.scheme != "https" or parsed.netloc != "lk.glopro.ru":
            request_route.abort()
        elif parsed.path == "/reports/generate/":
            # The renderer loads queue history while the attachment is pending.
            time.sleep(0.6)
            request_route.fulfill(status=200, body=workbook_bytes, headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Content-Disposition": "attachment; filename=report.xlsx",
            })
        elif parsed.path == "/clients/client/83706":
            request_route.fulfill(status=200, body=PORTAL_FIXTURE, content_type="text/html")
        else:
            request_route.abort()

    with sync_playwright() as playwright:
        options = {"headless": True}
        channel = _browser_channel()
        if channel:
            options["channel"] = channel
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(accept_downloads=True, service_workers="block")
            context.route("**/*", route)
            page = context.new_page()
            page.set_default_timeout(3_000)
            url = "https://lk.glopro.ru/clients/client/83706"
            page.goto(url)
            connector = GloProConnector("fixture-user", "fixture-password", timeout=5)
            monkeypatch.setattr(connector, "_check_activity", lambda *args: {"has_operations": True})
            xlsx_radio = page.locator('[format="xlsx"] input[type="radio"]')
            assert not xlsx_radio.is_visible()
            assert not xlsx_radio.is_checked()
            assert page.locator('[format="xls"] input[type="radio"]').is_checked()

            preview = connector._prepare_report(
                page, {"id": "83706", "url": url},
                {"id": "112042", "url": url + "?contract_id=112042"},
                date(2026, 9, 18), date(2026, 9, 21),
            )
            assert preview["checked"]
            assert xlsx_radio.is_checked()
            assert not xlsx_radio.is_visible()
            target = tmp_path / "report.xlsx"
            metadata = connector._download(page, target)

            assert target.read_bytes() == workbook_bytes
            assert metadata["bytes"] == len(workbook_bytes)
            assert page.evaluate("generateCount") == 1
            assert page.locator(".ordered_reports a").count() == 2
            generated = [query for _, path, query in requests if path == "/reports/generate/"]
            assert len(generated) == 1
            assert generated[0]["format"] == ["xlsx"]
            assert generated[0]["period_start"] == ["2026-09-18"]
            assert generated[0]["period_end"] == ["2026-09-21"]
            assert not any(path.startswith("/upload/report/") for _, path, _ in requests)
            assert all(host == "lk.glopro.ru" for host, _, _ in requests)
        finally:
            browser.close()
