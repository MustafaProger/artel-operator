"""Offline checks for the cabinet table; never connect to Yandex."""
import os
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
