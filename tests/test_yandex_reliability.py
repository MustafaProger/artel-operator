"""Synthetic fuel data only. Deterministic matrices and end-to-end failure sequences."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from random import Random
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
import hashlib
import json

import pytest
from openpyxl import Workbook

from operator_app import yandex, yandex_connection, storage, engine
from operator_app.yandex_cabinet import normalize_order, collect_orders, CabinetError, REPORTS
from operator_app.yandex_reports import read_report, number
from test_yandex import START, END, yandex_config
from test_yandex_dynamic import COMPANY, order as fixture_order, page, Pages, manifest
from test_engine import isolated_engine

COMPANY_NAME = 'ООО НК АРТЭЛЬ'
STATUSES = ('Completed', 'Cancelled', 'StationCanceled', 'UserCanceled', 'Refunded', 'Pending', 'Unknown', '', None)
VALUES = ('0', '-0.00', '0.001', '1.005', '-1', 'NaN', 'Infinity', None, True)


def order(*args, **changes):
    changes.setdefault('name', 'Тестовый сотрудник')
    return fixture_order(*args, **changes)


def write_book(path, raw_orders, *, omit=()):
    rows = [o for o in raw_orders if o['id'] not in omit]
    total = sum((Decimal(o['final_price']) for o in rows), Decimal(0))
    book = Workbook(); sheet = book.active; sheet.title = 'Отчёт'
    for key, value in [('Компания', COMPANY_NAME), ('Период', f'{START:%d.%m.%Y} - {END:%d.%m.%Y}'),
                       ('Данные указаны по часовому поясу', 'UTC+3'),
                       ('Количество заказов в отчётном периоде', len(rows)), ('Общая стоимость с НДС', str(total))]:
        sheet.append([key, None, None, value])
    sheet.append([])
    sheet.append(['Дата заказа', 'Имя пользователя', 'Телефон', 'Идентификатор заказа', 'Топливо',
                  'Запрос', 'Залито', 'Статус', 'Скидка', 'Стоимость'])
    for raw in rows:
        sheet.append([datetime.fromisoformat(raw['created_at']).strftime('%d.%m.%Y'), raw['user_info']['fullname'],
                      raw['user_info']['phone'], raw['id'], 'ДТ', 999,
                      str(Decimal(raw['liters_filled']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)),
                      'Завершён' if raw['status'] == 'Completed' else 'Отменён', 0, raw['final_price']])
    sheet.append([None] * 8 + ['Итого', str(total)])
    book.save(path); book.close()
    return path


@pytest.mark.parametrize('status', STATUSES)
@pytest.mark.parametrize('litres', VALUES)
@pytest.mark.parametrize('amount', VALUES)
def test_status_finance_cartesian_matrix(status, litres, amount):
    """729 distinct status/quantity/cost inputs, including non-finite values."""
    try:
        l, a = Decimal(str(litres)), Decimal(str(amount))
        valid = l.is_finite() and a.is_finite() and l >= 0 and a >= 0 and (l != 0 or a == 0)
        valid = valid and (status == 'Completed' or status in {'Cancelled', 'StationCanceled', 'UserCanceled'} and l == a == 0)
    except Exception:
        valid = False
    raw = order(status=status, liters_filled=litres, final_price=amount)
    if valid:
        normalized = normalize_order(raw, COMPANY, START, END)
        assert Decimal(normalized['litres']) == l and Decimal(normalized['amount']) == a
        assert normalized['active'] is (status == 'Completed' and l > 0)
    else:
        with pytest.raises(CabinetError):
            normalize_order(raw, COMPANY, START, END)


@pytest.mark.parametrize('timestamp,valid', [
    ('2026-09-15T00:00:00+03:00', True), ('2026-09-21T23:59:59.999999+03:00', True),
    ('2026-09-14T23:59:59.999999+03:00', False), ('2026-09-22T00:00:00+03:00', False),
    ('2026-09-15T12:00:00', False), ('2026-09-15T12:00:00+00:00', False),
    ('2026-09-15T12:00:00+04:00', False), ('2026-02-30T12:00:00+03:00', False),
])
def test_order_period_inclusive_microseconds(timestamp, valid):
    if valid:
        assert normalize_order(order(created_at=timestamp), COMPANY, START, END)['date'] == timestamp[:10]
    else:
        with pytest.raises(CabinetError):
            normalize_order(order(created_at=timestamp), COMPANY, START, END)


@pytest.mark.parametrize('seed', range(24))
def test_generated_pagination_and_complete_coverage(seed):
    rng = Random(29092026 + seed)
    count, limit = rng.randrange(1, 220), rng.randrange(1, 31)
    raw = [order(i + 1, uid=f'{(i % 7) + 100:032x}',
                 created_at=(datetime(2026, 9, 21, 23, 59) - timedelta(minutes=i)).isoformat() + '+03:00')
           for i in range(count)]
    pages = []
    for offset in range(0, count, limit):
        rows = raw[offset:offset + limit]
        pages.append(page(rows, f'next-{offset}' if len(rows) == limit else '', limit, total=count))
    if count % limit == 0:
        pages.append(page([], '', limit, total=count))
    result = collect_orders(Pages(pages), START, END, limit=limit)
    assert [o['id'] for o in result['orders']] == [o['id'] for o in raw]
    assert result['complete'] and result['order_count'] == count
    # Move one ID across the page boundary: a duplicate cannot prove coverage.
    if len(pages) > 1 and pages[1]['orders']:
        broken = deepcopy(pages); broken[1]['orders'][0] = deepcopy(raw[0])
        with pytest.raises(CabinetError):
            collect_orders(Pages(broken), START, END, limit=limit)


@pytest.mark.parametrize('mutation', ['duplicate', 'duplicate_changed', 'missing_date', 'outside_period'])
def test_corrupt_expected_manifest_cannot_justify_missing_cancellation(tmp_path, mutation):
    raw = [order(), order(2, status='UserCanceled', liters_filled='0', final_price='0')]
    expected = manifest(raw)['orders']
    path = write_book(tmp_path/'report.xlsx', raw, omit=[raw[1]['id']])
    if mutation.startswith('duplicate'):
        extra = deepcopy(expected[1])
        if mutation == 'duplicate_changed': extra['amount'] = '1'
        expected.insert(1, extra)
    elif mutation == 'missing_date':
        expected[1].pop('date')
    else:
        expected[1]['date'] = '2026-09-22'
    with pytest.raises(ValueError):
        read_report(path, START, END, COMPANY_NAME, expected_orders=expected)


@pytest.mark.parametrize('seed', range(20))
def test_decimal_rows_and_omitted_cancellations_generated(seed, tmp_path):
    rng = Random(7300 + seed)
    raw = []
    for i in range(1, rng.randrange(3, 20)):
        status = rng.choice(['Completed', 'Completed', 'StationCanceled', 'UserCanceled', 'Cancelled'])
        litres = str(Decimal(rng.randrange(10000, 1000000)) / 1000) if status == 'Completed' else '0'
        amount = str(Decimal(rng.randrange(1, 10000000)) / 100) if status == 'Completed' else '0'
        raw.append(order(i, status=status, liters_filled=litres, final_price=amount))
    omit = [o['id'] for o in raw if o['status'] in {'StationCanceled', 'UserCanceled'} and rng.choice([True, False])]
    if len(omit) == len(raw): omit.pop()
    expected = manifest(raw)['orders']
    path = write_book(tmp_path/'generated.xlsx', raw, omit=omit)
    report = read_report(path, START, END, COMPANY_NAME, expected_orders=expected)
    expected_litres = sum((Decimal(o['liters_filled']).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) for o in raw if o['status']=='Completed'), Decimal(0))
    expected_cost = sum((Decimal(o['final_price']) for o in raw), Decimal(0))
    assert Decimal(report['litres']) == expected_litres and Decimal(report['amount']) == expected_cost
    assert report['cabinet_order_count'] == len(raw)
    assert {o['id'] for o in report['orders']} | {o['id'] for o in report['cabinet_only_zero_cancellations']} == {o['id'] for o in raw}
    # A one-kopeck API change must be detected even if Excel totals agree internally.
    changed = deepcopy(expected); changed[0]['amount'] = str(Decimal(changed[0]['amount']) + Decimal('.01'))
    with pytest.raises(ValueError):
        read_report(path, START, END, COMPANY_NAME, expected_orders=changed)


def fake_download_environment(monkeypatch, tmp_path, statuses):
    identity = 'f' * 32
    raw = order()
    employee = yandex.discover_employees(manifest([raw]))[0]
    columns = ['due_data', 'user_fullname', 'user_phone', 'order_id', 'fuel_type', 'fuel_filled', 'status', 'price']
    calls = []
    states = iter(statuses)
    def request(path, payload=None):
        calls.append((path, payload))
        if path.endswith('history'): return {'reports': []}
        if path.endswith('tab-columns'): return {'tanker': [{'tab':'report.report', 'columns':[{'id':c} for c in columns]}]}
        if path.endswith('generate'): return {'task_id': identity}
        if path.endswith('status'): return next(states)
        raise AssertionError(path)
    cabinet = SimpleNamespace(request=request)
    browser = MagicMock()
    browser.context.new_page.return_value = browser
    clock = [0]
    browser.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms/1000)
    monkeypatch.setattr(yandex.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(yandex, 'verify_page', Mock())
    monkeypatch.setattr(yandex, 'open_reports', Mock())
    browser.expect_download.return_value.__enter__.return_value.value.save_as.side_effect = lambda dest: write_book(dest, [raw])
    return cabinet, browser, employee, calls


@pytest.mark.parametrize('kind', ['delayed', 'timeout', 'wrong_task', 'unknown', 'failed'])
def test_report_task_state_machine_never_replays_generate(kind, monkeypatch, tmp_path):
    identity = 'f'*32
    states = [{'task_id': identity, 'status': 'processing'}] * (130 if kind == 'timeout' else 4)
    states += [{'task_id': 'e'*32 if kind == 'wrong_task' else identity,
                'status': {'unknown':'mystery','failed':'failed'}.get(kind,'complete')}]
    cabinet, browser, employee, calls = fake_download_environment(monkeypatch, tmp_path, states)
    events=[]
    if kind=='delayed':
        result=yandex.download_employee(cabinet,browser,tmp_path,employee,START,END,COMPANY_NAME,[],events.append)
        assert result['report_id']==identity
        browser.locator.assert_called_once_with(f'[id$="-option-{identity}"]')
        browser.locator.return_value.get_by_role.assert_called_once_with('button',name='Скачать',exact=True)
        browser.locator.return_value.get_by_role.return_value.press.assert_called_once_with('Enter')
    else:
        with pytest.raises(yandex.YandexError):
            yandex.download_employee(cabinet,browser,tmp_path,employee,START,END,COMPANY_NAME,[],events.append)
        browser.expect_download.assert_not_called()
        assert not list(tmp_path.glob('*.xlsx'))
    assert len([p for p,_ in calls if p.endswith('generate')])==1


@pytest.mark.parametrize('field,value', [('amount','1'),('litres','31'),('status','Cancelled'),
    ('date','2026-09-16'),('user_id','b'*32),('phone_sha256','0'*64),('source_name','Новое имя')])
def test_snapshot_any_financial_or_identity_change_blocks_completion(field,value,monkeypatch,tmp_path):
    full=manifest([order()]); changed=deepcopy(full);changed['orders'][0][field]=value
    session=tmp_path/'session.json';session.write_text('{}')
    monkeypatch.setattr(yandex.yandex_connection,'session_path',lambda:session)
    monkeypatch.setattr(yandex,'browser_proxy',lambda **kwargs:nullcontext(None))
    manager=MagicMock();monkeypatch.setattr(yandex,'sync_playwright',lambda:manager)
    monkeypatch.setattr(yandex,'Cabinet',Mock());monkeypatch.setattr(yandex,'verify_page',Mock())
    monkeypatch.setattr(yandex,'collect_orders',Mock(side_effect=[full,changed]))
    monkeypatch.setattr(yandex,'download_employee',Mock(return_value={'path':'local.xlsx'}))
    save=Mock();monkeypatch.setattr(yandex.yandex_connection,'save_session',save)
    events=[]
    with pytest.raises(yandex.YandexError,match='изменились'):
        yandex.download_reports({'company':COMPANY_NAME},{'run_date':'2026-09-22'},tmp_path,events.append)
    assert not any(e.get('activity_check_complete') for e in events)
    save.assert_not_called()
    manager.__enter__.return_value.chromium.launch.return_value.close.assert_called_once()


@pytest.mark.parametrize('stage', ['serialize','replace'])
def test_session_write_failure_preserves_previous_private_session(tmp_path,monkeypatch,stage):
    monkeypatch.setattr(storage,'DATA',tmp_path)
    original=tmp_path/'yandex-session.json';original.write_text('{"cookies": []}');original.chmod(0o600)
    context=Mock()
    if stage=='serialize': context.storage_state.side_effect=RuntimeError('synthetic failure')
    else:
        context.storage_state.return_value={'cookies':[]}
        monkeypatch.setattr(yandex_connection.os,'replace',Mock(side_effect=OSError('synthetic failure')))
    before=original.read_bytes()
    with pytest.raises((OSError,RuntimeError)): yandex_connection.save_session(context)
    assert original.read_bytes()==before and original.stat().st_mode & 0o777==0o600
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('seed', range(3))
def test_repeated_local_pipeline_preserves_last_good_note(isolated_engine,tmp_path,monkeypatch,seed):
    data,operators=isolated_engine;notes=tmp_path/'notes';yandex_config(operators,notes)
    raw=[order(1),order(2,uid='b'*32,name='Другой сотрудник',phone='72222222222')]
    full=manifest(raw)
    rng = Random(seed + 2909)
    sequence=['good']+[rng.choice(['missing_last','last_error','wrong_sum','good']) for _ in range(12)]
    mode=['good']
    def handler(conf,record,directory,progress):
        progress({'stage':'discovered','manifest':full})
        sources=[]
        for i,item in enumerate(raw):
            if i==1 and mode[0]=='last_error': raise yandex.YandexError('Сбой последнего сотрудника')
            if i==1 and mode[0]=='missing_last': continue
            content=deepcopy(item)
            if i==1 and mode[0]=='wrong_sum': content['final_price']='1'
            path=write_book(directory/f'{i}.xlsx',[content])
            sources.append({'path':str(path),'user_id':item['user_id'],'report_id':f'{i+10:032x}'})
        progress({'activity_check_complete':True,'order_count':2,'employee_ids':[o['user_id'] for o in raw]})
        return sources
    monkeypatch.setitem(engine.HANDLERS,'yandex',handler)
    note=notes/'Яндекс Заправки — 22.09.2026.md';last_good=None;successful_artifacts={}
    for mode[0] in sequence:
        run=engine.submit('yandex','2026-09-22')
        if mode[0]=='good':
            assert run['status']=='completed'
            if last_good is None:
                note.write_text(note.read_text()+'\n## Ручные заметки\nСохранить этот текст.\n')
            last_good=note.read_bytes()
            successful_artifacts.update({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (data/'runs'/run['id']).rglob('*') if p.is_file()})
        else:
            assert run['status'] in {'failed','needs_review'} and not run['report']
            assert note.read_bytes()==last_good
        assert 'Сохранить этот текст.' in note.read_text()
        for p,sha in successful_artifacts.items(): assert hashlib.sha256(p.read_bytes()).hexdigest()==sha


def managed_note(uid, name, litres='30,00'):
    return (f'# Яндекс Заправки — 22.09.2026\n\n### {name}\n'
            f'<!-- yandex-employee:{uid} phone:{"1"*64} orders:{"2"*64} -->\n'
            f'15.09 - 21.09\nЯндекс заправки\nна склад ({name}) {litres} л на сумму закупки 1,00 рублей\n')


@pytest.mark.parametrize('opening,closing',[('```md','```'),('~~~','~~~'),('````md','````'),('  ```','  ```')])
def test_manual_fenced_example_is_not_a_managed_yandex_section(opening,closing):
    from operator_app.yandex_publication import merge_yandex_sections
    incoming=managed_note('a'*32,'Сотрудник')
    example='## Пример\n'+opening+'\n'+incoming.split('\n\n',1)[1]+closing+'\n\n## Ручные записи\nОставить.\n'
    result=merge_yandex_sections('# Заметка\n\n'+example,incoming)
    assert example in result
    assert result.count('на склад (Сотрудник)')==2


@pytest.mark.parametrize('count',[2,4,8])
def test_concurrent_yandex_note_writers_preserve_every_employee(tmp_path,monkeypatch,count):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import time
    from operator_app import yandex_publication as publication
    path=tmp_path/'note.md';path.write_text('# Ручная заметка\n\n## Важно\nОставить.\n');path.chmod(0o600)
    barrier=Barrier(count)
    merge=publication.merge_yandex_sections
    def slow_merge(existing,incoming):
        time.sleep(.005)
        return merge(existing,incoming)
    monkeypatch.setattr(publication,'merge_yandex_sections',slow_merge)
    def worker(index):
        barrier.wait(timeout=5)
        publication.save_yandex_sections(path,managed_note(f'{index+1:032x}',f'Сотрудник {index}'))
    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(worker,range(count)))
    result=path.read_text()
    assert '## Важно\nОставить.' in result
    assert all(result.count(f'yandex-employee:{index+1:032x}')==1 for index in range(count))
    assert path.stat().st_mode & 0o777==0o600
    assert not list(tmp_path.glob('.*.tmp'))


def test_yandex_writer_preserves_unrelated_stale_temp(tmp_path):
    from operator_app.yandex_publication import save_yandex_sections
    path=tmp_path/'note.md';stale=tmp_path/'note.md.tmp';stale.write_text('Unrelated recoverable data')
    save_yandex_sections(path,managed_note('a'*32,'Сотрудник'))
    assert stale.read_text()=='Unrelated recoverable data'


@pytest.mark.parametrize('fence',['```md','~~~'])
def test_unclosed_manual_fence_blocks_append_without_changing_note(tmp_path,fence):
    from operator_app.yandex_publication import save_yandex_sections
    path=tmp_path/'note.md';path.write_text('# Ручная заметка\n'+fence+'\nПример без закрывающей границы\n')
    original=path.read_bytes()
    with pytest.raises(ValueError,match='незакрытый'):
        save_yandex_sections(path,managed_note('a'*32,'Сотрудник'))
    assert path.read_bytes()==original
