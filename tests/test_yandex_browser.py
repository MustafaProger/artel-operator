"""Offline checks for the cabinet table; never connect to Yandex."""
import os
from pathlib import Path
from datetime import date
from urllib.parse import urlencode
import pytest
from playwright.sync_api import sync_playwright
from operator_app.yandex import verify_page, YandexError
from operator_app.yandex_cabinet import Cabinet, CabinetError, API
from operator_app.yandex_connection import URL
import json
from test_yandex import STAFF,START,END

pytestmark=pytest.mark.skipif(os.environ.get('RUN_BROWSER_TESTS')!='1',reason='Opt-in local browser fixture')


def test_browser_session_binds_requests_to_verified_company():
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            endpoint=API+'/corp-cabinet/1.0/clients'
            html='''<a href="/">ООО НК АРТЭЛЬ</a><button>Все фильтры</button>
            <script>fetch("''' + endpoint + '''", {headers: {"x-csrf-token":"fixture", "x-yataxi-selected-corp-client-id":"cccccccccccccccccccccccccccccccc"}})</script>'''
            def route(request):
                if request.request.url==endpoint:
                    request.fulfill(json=dict(id='c'*32,name='ООО НК АРТЭЛЬ'),headers={'Access-Control-Allow-Origin':'*'})
                else:request.fulfill(body=html,content_type='text/html; charset=utf-8')
            page.route('**/*',route)
            with page.expect_response(endpoint) as response:page.goto(URL)
            verify_page(page,'ООО НК АРТЭЛЬ')
            cabinet=Cabinet(page.context,response.value,'ООО НК АРТЭЛЬ')
            assert cabinet.company_id=='c'*32 and cabinet.headers['x-csrf-token']=='fixture'
            with pytest.raises(CabinetError,match='другая организация'):Cabinet(page.context,response.value,'Другая')
        finally:browser.close()


def test_offer_is_never_accepted_automatically():
    from operator_app.yandex import verify_page
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page()
            page.set_content('''<a href="/">ООО НК АРТЭЛЬ</a><button>Все фильтры</button><div role="dialog"><h2>В Заправках появились зарядные станции</h2><button onclick="this.textContent='Принято'">Понятно</button><p>Нажимая кнопку, вы принимаете условия Оферты</p></div>''')
            with pytest.raises(YandexError,match='оферту'):verify_page(page,'ООО НК АРТЭЛЬ')
            assert page.get_by_role('button',name='Понятно',exact=True).is_visible()
        finally:browser.close()


def test_reports_open_by_keyboard_when_promotion_covers_button():
    from operator_app.yandex import open_reports
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('''<button onclick="document.querySelector('#history').hidden=false">Отчёты</button>
            <button id="history" hidden>Все отчёты</button>
            <div style="position:fixed;inset:0;background:white">Скидка 5% на мойки</div>''')
            open_reports(page)
            assert page.get_by_role('button', name='Все отчёты', exact=True).is_visible()
        finally:
            browser.close()


@pytest.mark.parametrize('content_kind', ['valid', 'wrong_user', 'broken_xlsx'])
def test_exact_download_under_overlay_preserves_rejected_source(tmp_path, content_kind):
    """Real Chromium keyboard/download events; all HTTP is local fixture data."""
    from operator_app.yandex import download_employee
    from operator_app.yandex_cabinet import REPORTS, discover_employees
    from test_yandex_dynamic import order, manifest
    from test_yandex_reliability import write_book
    identity = 'f' * 32
    raw = order(name='Тестовый сотрудник')
    employee = discover_employees(manifest([raw]))[0]
    source = tmp_path / 'fixture.xlsx'
    content = dict(raw)
    if content_kind == 'wrong_user':
        content['user_info'] = dict(fullname='Чужой сотрудник', phone='79999999999')
    write_book(source, [content])
    binary = source.read_bytes() if content_kind != 'broken_xlsx' else b'PK\x03\x04truncated'
    calls = []
    class FixtureCabinet:
        def request(self, path, payload=None):
            calls.append((path, payload))
            if path.endswith('history'): return {'reports': [{'task_id':'e'*32}]}
            if path.endswith('tab-columns'):
                return {'tanker': [{'tab':'report.report', 'columns':[{'id':c} for c in
                    ['due_data','user_fullname','user_phone','order_id','fuel_type','fuel_filled','status','price']]}]}
            if path.endswith('generate'):
                assert payload['user_ids'] == [raw['user_id']]
                assert payload['since_date'] == str(START) and payload['till_date'] == str(END)
                return {'task_id':identity}
            if path.endswith('status'):
                assert payload == {'task_id':identity}
                return {'task_id':identity,'status':'complete'}
            raise AssertionError(path)
    html = '''<a href="/">ООО НК АРТЭЛЬ</a><button>Все фильтры</button>
      <button onclick="document.getElementById('reports').hidden=false">Отчёты</button>
      <section id="reports" hidden><button>Все отчёты</button>
      <div id="list-option-''' + 'e'*32 + '''"><button onclick="location.href='/old'">Скачать</button></div>
      <div id="list-option-''' + identity + '''"><button onclick="location.href='/fresh'">Скачать</button></div></section>
      <div style="position:fixed;inset:0;z-index:999;background:white">Реклама
      <button id="accept" onclick="this.textContent='Принято'">Принять оферту</button></div>'''
    requests = []
    def route(r):
        from urllib.parse import urlparse
        path = urlparse(r.request.url).path
        requests.append(path)
        if path == '/fresh':
            r.fulfill(body=binary, headers={'Content-Disposition':'attachment; filename=report.xlsx',
                'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
        elif path == '/old':
            raise AssertionError('Must not select a historical report')
        else:
            r.fulfill(body=html, content_type='text/html; charset=utf-8')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(accept_downloads=True, service_workers='block')
            context.route('**/*', route)
            page = context.new_page()
            page.goto('https://fixture.invalid/cabinet')
            folder=tmp_path/'download';folder.mkdir();events=[]
            if content_kind == 'valid':
                result=download_employee(FixtureCabinet(),page,folder,employee,START,END,'ООО НК АРТЭЛЬ',[],events.append)
                assert result['report_id'] == identity
                assert Path(result['path']).read_bytes() == binary
            else:
                with pytest.raises(Exception):
                    download_employee(FixtureCabinet(),page,folder,employee,START,END,'ООО НК АРТЭЛЬ',[],events.append)
                rejected=folder/'На проверку'/f'{identity}.xlsx'
                assert rejected.read_bytes() == binary and rejected.stat().st_mode & 0o777 == 0o600
                assert not list(folder.glob('Яндекс.*.xlsx'))
                assert events[-1]['stage']=='validation_failed'
            assert requests.count('/fresh')==1 and '/old' not in requests
            assert len([c for c in calls if c[0]==REPORTS+'generate'])==1
            assert page.get_by_role('button',name='Принять оферту',exact=True).is_visible()
        finally:
            browser.close()
