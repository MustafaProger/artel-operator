from contextlib import nullcontext
from datetime import date
from unittest.mock import patch

import pytest
from openpyxl import load_workbook

from operator_app import engine
from operator_app.glopro import GloProConnector, GloProError, activity_from_recent
from test_engine import isolated_engine, source_file
from test_api import isolated_api


def test_outputs_are_alphabetical_without_empty_companies(isolated_engine, tmp_path):
    sources = []
    for name, empty in [('ООО "Я"', False), ('АО "А"', False), ('ООО "Пусто"', True)]:
        path = source_file(tmp_path / (name + '.xlsx'), client=name, empty=empty)
        sources.append({"path": str(path), "name": path.name})
    run = engine.submit('glopro', '2026-09-18', 'import', sources)
    assert run['status'] == 'completed'
    assert run['report'].index('АО "А"') < run['report'].index('ООО "Я"')
    assert 'Пусто' not in run['report']
    root = engine.DATA / 'runs' / run['id']
    book = load_workbook(root / 'Активация — 18.09.2026.xlsx')
    assert [row[0] for row in list(book.active.values)[1:]] == ['АО "А"', 'ООО "Я"']
    book.close()
    assert not any('telegram' in f['name'].lower() for f in run['files'])


def test_telegram_endpoints_removed(isolated_api):
    client, _, _ = isolated_api
    assert 'telegram' not in client.get('/api/state').json()
    assert client.post('/api/telegram/code', json={}).status_code == 404
    assert client.post('/api/runs/any/telegram/send', json={}).status_code == 404


def payload(*dates):
    return {'success': True, 'data': {'items': [{'DATETIME_TRN': value} for value in dates], 'more': False}, 'messages': []}


def test_latest_ten_is_not_treated_as_full_history():
    first, last = date(2026, 9, 15), date(2026, 9, 21)
    assert activity_from_recent(payload('2026-09-20 12:00:00'), first, last)['has_operations']
    assert not activity_from_recent(payload('2026-09-14 23:59:59'), first, last)['has_operations']
    assert not activity_from_recent(payload('2026-09-22 00:00:00', '2026-09-14 23:59:59'), first, last)['has_operations']
    assert not activity_from_recent({'success': False, 'data': False, 'messages': []}, first, last)['has_operations']
    for obj in [payload('2026-09-22 00:00:00'), payload(), {}, [], {'success': False, 'data': False, 'messages': ['Ошибка']}, payload('2026-09-14 00:00:00', '2026-09-20 00:00:00')]:
        with pytest.raises(GloProError):
            activity_from_recent(obj, first, last)


def test_empty_client_never_generates_or_downloads_file(tmp_path):
    connector = GloProConnector('fixture', 'fixture')
    events = []
    with patch.object(connector, '_browser_page', return_value=nullcontext(object())), \
         patch.object(connector, '_login'), \
         patch.object(connector, '_list_clients', return_value=[{'id': '1', 'name': 'Фирма'}]), \
         patch.object(connector, '_contracts', return_value=[{'id': '10'}]), \
         patch.object(connector, '_prepare_report', return_value={'checked': True, 'activity': {'has_operations': False}}), \
         patch.object(connector, '_download') as download:
        result = connector.download_reports('2026-09-18', '2026-09-21', tmp_path, progress=events.append)
    assert result == [] and not list(tmp_path.rglob('*.xlsx'))
    download.assert_not_called()
    assert events[-1]['activity_check_complete']
    assert any(e.get('reason') == 'no_operations' for e in events)


def test_all_verified_empty_is_no_data_but_unknown_empty_is_error(isolated_engine, monkeypatch):
    def empty(conf, record, directory, progress):
        progress({'stage': 'skipped', 'reason': 'no_operations', 'client': 'Фирма'})
        progress({'stage': 'downloaded', 'activity_check_complete': True})
        return []
    monkeypatch.setattr(engine, 'HANDLERS', {'glopro': empty})
    run = engine.submit('glopro', '2026-09-18')
    assert run['status'] == 'no_data'
    assert len(run['empty_clients']) == 1
    monkeypatch.setattr(engine, 'HANDLERS', {'glopro': lambda *args: []})
    assert engine.submit('glopro', '2026-09-18')['status'] == 'failed'
