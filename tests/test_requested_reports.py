from datetime import date
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook
from playwright.sync_api import TimeoutError

from operator_app import engine, storage
from operator_app.glopro import GloProConnector, GloProError
from operator_app.yandex_reports import record_period
from test_engine import isolated_engine
from test_yandex import source, yandex_config


@pytest.mark.parametrize('failure', ['timeout', 'network', 429, 503])
def test_activity_read_recovers_without_generating_report(failure):
    page = Mock()
    good = Mock(status=200)
    good.json.return_value = {'success': True, 'data': {'items': [{'DATETIME_TRN': '2026-09-30 12:00:00'}]}}
    bad = (TimeoutError('private call log') if failure == 'timeout' else
           RuntimeError('net::ERR_CONNECTION_RESET private') if failure == 'network' else Mock(status=failure))
    page.context.request.post.side_effect = [bad, good]
    result = GloProConnector('fixture', 'fixture')._check_activity(page, {'id': '1'}, date(2026, 9, 29), date(2026, 9, 30))
    assert result['has_operations'] is True
    assert page.context.request.post.call_count == 2
    good.dispose.assert_called_once()
    if isinstance(bad, Mock):
        bad.dispose.assert_called_once()


def test_activity_retry_exhaustion_never_marks_period_empty():
    page = Mock()
    page.context.request.post.side_effect = TimeoutError('SECRET')
    with pytest.raises(GloProError, match='3 попыток') as failure:
        GloProConnector('fixture', 'fixture')._check_activity(page, {'id': '1'}, date(2026, 9, 29), date(2026, 9, 30))
    assert 'SECRET' not in str(failure.value)
    assert page.context.request.post.call_count == 3


@pytest.mark.parametrize('response', [Mock(status=401), Mock(status=302), Mock(status=200)])
def test_invalid_activity_response_is_not_retried(response):
    response.json.return_value = {'success': True, 'data': {'items': []}}
    page = Mock()
    page.context.request.post.return_value = response
    with pytest.raises(GloProError):
        GloProConnector('fixture', 'fixture')._check_activity(page, {'id': '1'}, date(2026, 9, 29), date(2026, 9, 30))
    assert page.context.request.post.call_count == 1
    response.dispose.assert_called_once()


def test_requested_yandex_period_reaches_validation_and_outputs(isolated_engine, tmp_path, monkeypatch):
    _, operators = isolated_engine
    yandex_config(operators)
    path = source(tmp_path / 'requested.xlsx')
    book = load_workbook(path)
    book.active['D2'] = '29.09.2026 - 30.09.2026'
    book.active['A8'] = '29.09.2026'
    book.save(path)
    book.close()
    def download(conf, record, directory, progress):
        assert record_period(record) == (date(2026, 9, 29), date(2026, 9, 30))
        return [dict(path=str(path), name=path.name)]
    monkeypatch.setitem(engine.HANDLERS, 'yandex', download)
    # Verify the processor's file/range path; completeness checks have separate tests.
    original = engine.PROCESSORS['yandex']
    monkeypatch.setitem(engine.PROCESSORS, 'yandex', lambda *a, **kw: original(*a, imported=True))
    record = engine.submit('yandex', '2026-10-01', period_start='2026-09-29', period_end='2026-09-30')
    assert record['status'] == 'completed'
    assert '29.09 - 30.09' in record['report']
    saved = storage.get_run(record['id'])
    assert saved['period_end'] == '2026-09-30'


@pytest.mark.parametrize('kwargs', [dict(period_start='2026-09-29'),
    dict(period_start='2026-09-30', period_end='2026-09-29'),
    dict(period_start='2026-09-29', period_end='2026-10-01'),
    dict(period_start='2026-09-29', period_end='2026-09-30', trigger='schedule')])
def test_requested_yandex_period_rejects_invalid_or_scheduled_override(isolated_engine, kwargs):
    _, operators = isolated_engine
    yandex_config(operators)
    with pytest.raises(ValueError):
        engine.submit('yandex', '2026-10-01', **kwargs)
    assert storage.runs() == []


@pytest.mark.parametrize('exhausted', [False, True])
def test_yandex_download_retry_keeps_confirmed_task_id(monkeypatch, tmp_path, exhausted):
    from operator_app import yandex
    from test_yandex_reliability import fake_download_environment, START, END, COMPANY_NAME
    identity = 'f' * 32
    cabinet, browser, employee, calls = fake_download_environment(monkeypatch, tmp_path, [dict(task_id=identity, status='complete')])
    browser.goto.side_effect = [TimeoutError('PRIVATE'), TimeoutError('PRIVATE') if exhausted else None]
    if exhausted:
        with pytest.raises(TimeoutError):
            yandex.download_employee(cabinet, browser, tmp_path, employee, START, END, COMPANY_NAME, [], lambda e: None)
        assert not list(tmp_path.glob('*.xlsx'))
    else:
        result = yandex.download_employee(cabinet, browser, tmp_path, employee, START, END, COMPANY_NAME, [], lambda e: None)
        assert result['report_id'] == identity
        browser.locator.assert_called_once_with(f'[id$="-option-{identity}"]')
    assert browser.goto.call_count == 2
    assert browser.close.call_count == 2
    assert len([p for p, _ in calls if p.endswith('generate')]) == 1
