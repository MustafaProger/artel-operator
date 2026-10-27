"""Offline failures while reading GloPro and preparing, never generating, reports."""
from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from operator_app import glopro
from operator_app.glopro import GloProConnector, GloProError


CLIENT = {"id": "1", "name": "Тестовая фирма", "url": "https://lk.glopro.ru/clients/client/1"}
CONTRACT = {"id": "10", "url": CLIENT["url"] + "?contract_id=10"}
FIRST, LAST = date(2026, 9, 1), date(2026, 9, 30)


class DateField:
    def __init__(self):
        self.value = "2026-01-01"

    def wait_for(self, **kwargs):
        pass

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return "date"

    def fill(self, value):
        self.value = value

    def press(self, key):
        pass


@pytest.fixture
def preparation(monkeypatch):
    page = Mock()
    page.url, page.clock = CLIENT["url"], 0
    page.wait_for_timeout.side_effect = lambda ms: setattr(page, "clock", page.clock + ms / 1000)
    monkeypatch.setattr(glopro.time, "monotonic", lambda: page.clock)
    login = Mock()
    login.count.return_value = 0
    selector = Mock()
    selector.count.return_value, selector.input_value.return_value = 1, CONTRACT["id"]
    selector.locator.return_value.evaluate_all.return_value = [CONTRACT["id"]]
    account, active, reports, template = Mock(), Mock(), Mock(), Mock()
    template.select_option.return_value = ["19"]
    panel = Mock()
    panel.inner_text.return_value = "Последние транзакции: 18.09.2026"
    panel.locator.return_value.count.return_value = 0
    form, client_field, contract_field, xlsx = Mock(), Mock(), Mock(), Mock()
    client_field.count.return_value, client_field.input_value.return_value = 1, CLIENT["id"]
    contract_field.count.return_value, contract_field.input_value.return_value = 1, CONTRACT["id"]
    xlsx.locator.return_value.is_checked.return_value = True
    start, end = DateField(), DateField()
    form.locator.side_effect = {
        'input[name="period_start"]': start,
        'input[name="period_end"]': end,
        '.combobox_outer:has(input[name^="client_choose_single"]) input[name="combobox_value"]': client_field,
        '.combobox_outer:has(input[name^="contract_choose_single"]) input[name="combobox_value"]': contract_field,
        '[format="xlsx"]': xlsx,
    }.__getitem__
    page.locator.side_effect = {
        "form#login": login,
        'select[name="contracts_list"]': selector,
        '.ajax_contract_block a[href="#account"][ajax_tab]': account,
        '.ajax_contract_block a[href="#account"].active': active,
        '.ajax_contract_block .tab-content': panel,
        '.ajax_contract_block a[href="#reports"][ajax_tab]': reports,
        '#reports2 select.report_select': template,
        '#reports2 .report_template_block[report="19"]': form,
    }.__getitem__
    connector = GloProConnector("fixture", "fixture")
    goto = Mock()
    monkeypatch.setattr(connector, "_goto", goto)
    monkeypatch.setattr(connector, "_check_activity", Mock(return_value={"has_operations": True}))
    return SimpleNamespace(**locals())


def pipeline(fixture, tmp_path, monkeypatch):
    connector, page = fixture.connector, fixture.page
    monkeypatch.setattr(connector, "_browser_page", lambda: nullcontext(page))
    monkeypatch.setattr(connector, "_login", Mock())
    monkeypatch.setattr(connector, "_list_clients", lambda p: [CLIENT])
    monkeypatch.setattr(connector, "_contracts", lambda p, c: [CONTRACT])
    download = Mock(return_value={})
    monkeypatch.setattr(connector, "_download", download)
    events = []
    run = lambda: connector.download_reports(FIRST.isoformat(), LAST.isoformat(), tmp_path, progress=events.append)
    return run, download, events


@pytest.mark.parametrize("failure", [TimeoutError("PRIVATE_CALL_LOG"), RuntimeError("net::ERR_FAILED PRIVATE_URL")])
def test_form_read_retries_reload_exact_contract_before_single_generation(preparation, tmp_path, monkeypatch, failure):
    fixture = preparation
    fixture.template.wait_for.side_effect = [failure, None]
    run, download, events = pipeline(fixture, tmp_path, monkeypatch)
    results = run()
    assert len(results) == 1
    assert fixture.template.wait_for.call_count == 2
    fixture.goto.assert_called_once_with(fixture.page, CONTRACT["url"])
    download.assert_called_once()
    assert events[-1]["activity_check_complete"]
    assert fixture.start.value == FIRST.isoformat() and fixture.end.value == LAST.isoformat()


def test_preview_readiness_timeout_reloads_before_success(preparation):
    fixture = preparation
    fixture.panel.inner_text.side_effect = lambda: "Последние транзакции" if fixture.goto.call_count else "Загрузка"
    result = fixture.connector._prepare_report(fixture.page, CLIENT, CONTRACT, FIRST, LAST)
    assert result["checked"] and result["activity"]["has_operations"]
    fixture.goto.assert_called_once_with(fixture.page, CONTRACT["url"])
    assert fixture.page.clock >= 30


@pytest.mark.parametrize("failure", ["account", "preview", "report"])
def test_exhausted_read_never_generates_or_invents_empty_result(preparation, tmp_path, monkeypatch, failure):
    fixture = preparation
    expected_stage = {"account": "вкладка счёта", "preview": "данные счёта", "report": "вкладка отчётов"}[failure]
    if failure == "account":
        fixture.account.wait_for.side_effect = TimeoutError("PRIVATE_AUTH_BODY")
    elif failure == "preview":
        fixture.panel.inner_text.return_value = "Загрузка"
    else:
        fixture.template.wait_for.side_effect = TimeoutError("PRIVATE_AUTH_BODY")
    preserved = tmp_path / "earlier.xlsx"
    preserved.write_bytes(b"previous successful source")
    run, download, events = pipeline(fixture, tmp_path, monkeypatch)
    with pytest.raises(GloProError) as error:
        run()
    assert expected_stage in str(error.value) and "3 попыток" in str(error.value)
    assert "PRIVATE" not in str(error.value)
    assert fixture.goto.call_count == 2
    download.assert_not_called()
    assert not any(e.get("activity_check_complete") or e.get("reason") == "no_operations" for e in events)
    assert preserved.read_bytes() == b"previous successful source"


@pytest.mark.parametrize("failure", ["scope", "template", "format", "activity", "auth", "schema"])
def test_business_auth_and_schema_failures_are_not_retried(preparation, failure):
    fixture = preparation
    if failure == "scope":
        fixture.client_field.input_value.return_value = "999"
    elif failure == "template":
        fixture.template.select_option.return_value = []
    elif failure == "format":
        fixture.xlsx.locator.return_value.is_checked.return_value = False
    elif failure == "activity":
        fixture.connector._check_activity.side_effect = GloProError("GloPro не подтвердил наличие операций.")
    elif failure == "auth":
        fixture.login.count.return_value = 1
    else:
        fixture.template.wait_for.side_effect = RuntimeError("PRIVATE_UNEXPECTED_BROWSER_STATE")
    with pytest.raises(GloProError) as error:
        fixture.connector._prepare_report(fixture.page, CLIENT, CONTRACT, FIRST, LAST)
    assert "PRIVATE" not in str(error.value)
    fixture.goto.assert_not_called()
    assert fixture.account.wait_for.call_count <= 1


def test_contract_selector_read_recovers_without_duplicate_manifest(preparation):
    fixture = preparation
    fixture.selector.wait_for.side_effect = [TimeoutError("PRIVATE"), None]
    assert fixture.connector._contracts(fixture.page, CLIENT) == [CONTRACT]
    assert fixture.goto.call_count == 2
    assert all(call.args == (fixture.page, CLIENT["url"]) for call in fixture.goto.call_args_list)


def test_contract_selector_timeout_fails_instead_of_empty_contracts(preparation):
    fixture = preparation
    fixture.selector.wait_for.side_effect = TimeoutError("PRIVATE")
    with pytest.raises(GloProError, match="список договоров.*3 попыток") as error:
        fixture.connector._contracts(fixture.page, CLIENT)
    assert "PRIVATE" not in str(error.value)
    assert fixture.goto.call_count == 3


@pytest.mark.parametrize("ids", [["10", "10"], ["unknown"]])
def test_invalid_contracts_are_not_retried(preparation, ids):
    fixture = preparation
    fixture.selector.locator.return_value.evaluate_all.return_value = ids
    with pytest.raises(GloProError):
        fixture.connector._contracts(fixture.page, CLIENT)
    fixture.goto.assert_called_once()
