from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError

from operator_app import yandex, yandex_cabinet as yc
from test_transient_recovery import cabinet, response
from test_yandex_reliability import fake_download_environment, START, END, COMPANY_NAME


@pytest.mark.parametrize("endpoint,label", [
    ("history", "загрузка списка отчётов"),
    ("tab-columns", "загрузка столбцов отчёта"),
    ("status", "проверка готовности отчёта"),
])
def test_read_failure_identifies_operation_and_http_without_private_body(monkeypatch, endpoint, label):
    failed = response(500, {"message": "private session data"})
    api, fetch = cabinet(monkeypatch, [failed] * 3)
    with pytest.raises(yc.CabinetError) as error:
        api.request(yc.REPORTS + endpoint)
    assert label in str(error.value)
    assert "HTTP 500" in str(error.value)
    assert "private" not in str(error.value)
    assert "Результат создания неизвестен" not in str(error.value)
    assert fetch.call_count == 3


@pytest.mark.parametrize("failure", [response(500), TimeoutError("private session data")])
def test_creation_failure_is_explicitly_unknown_and_never_replayed(monkeypatch, failure):
    api, fetch = cabinet(monkeypatch, [failure])
    with pytest.raises(yc.CabinetError) as error:
        api.request(yc.REPORTS + "generate", {"service": "tanker"})
    assert "создание отчёта" in str(error.value)
    assert "Результат создания неизвестен" in str(error.value)
    assert "private" not in str(error.value)
    assert fetch.call_count == 1


@pytest.mark.parametrize("endpoint", ["history", "tab-columns", "generate"])
def test_history_distinguishes_preparation_from_uncertain_creation(monkeypatch, tmp_path, endpoint):
    api, browser, employee, calls = fake_download_environment(monkeypatch, tmp_path, [])
    original = api.request
    events = []

    def failing_request(path, payload=None):
        if path.endswith(endpoint):
            if endpoint == "generate":
                assert events[-1]["report_creation_requested"] is True
            raise yc.CabinetError("HTTP 500")
        return original(path, payload)

    api.request = Mock(side_effect=failing_request)
    with pytest.raises(yc.CabinetError):
        yandex.download_employee(api, browser, tmp_path, employee, START, END,
                                 COMPANY_NAME, [], events.append)
    assert any(e.get("report_creation_requested") for e in events) is (endpoint == "generate")
    browser.expect_download.assert_not_called()
    assert not list(tmp_path.glob("*.xlsx"))
