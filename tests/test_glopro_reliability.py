"""Offline GloPro fault injection. Virtual clock; no real portal traffic."""
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import Mock
import hashlib
import zipfile

import pytest

from operator_app import glopro, config, engine
from operator_app.glopro import GloProConnector, GloProError, activity_from_recent, _PaginationEvidence
from test_glopro import DownloadPage, Download, REPORT_PARAMETERS, report_url, report_query, workbook
import test_glopro as fixtures
from test_output_rules import payload
from test_engine import isolated_engine, source_file


@pytest.mark.parametrize('seed', range(12))
def test_generated_recent_windows_never_invent_empty_period(seed):
    rng=Random(2909+seed)
    for case in range(40):
        first=date(2025,1,1)+timedelta(days=rng.randrange(900));last=first+timedelta(days=rng.randrange(1,8))
        stamps=sorted([datetime.combine(first,datetime.min.time())+timedelta(hours=rng.randrange(-72,300))
                       for _ in range(rng.randrange(1,11))],reverse=True)
        data=payload(*(s.strftime('%Y-%m-%d %H:%M:%S') for s in stamps))
        data['data']['more']=rng.choice([True,False])
        result=activity_from_recent(data,first,last)
        in_period=any(first<=s.date()<=last for s in stamps)
        expected=True if in_period else False if min(stamps).date()<first else None
        assert result['has_operations'] is expected,(seed,case)
        assert bool(result.get('requires_period_report')) is (expected is None)
        assert result['checked_rows']==len(stamps)


@pytest.mark.parametrize('bad', [None, [], {}, {'success':True,'data':None},
    {'success':True,'data':{'items':[]}}, {'success':False,'data':False,'messages':['error']},
    payload('not a date'), payload('2026-02-30 00:00:00'), payload('2026-09-18 00:00:00Z'),
    payload('2026-09-17 00:00:00','2026-09-18 00:00:00'),
    {'success':True,'data':{'items':[{'WRONG':'2026-09-17'}]}},
    {'success':True,'data':{'items':[None]}}])
def test_invalid_recent_window_stops_instead_of_skipping(bad):
    with pytest.raises(GloProError):activity_from_recent(bad,date(2026,9,15),date(2026,9,21))


@pytest.mark.parametrize('year', [2024,2026,2027,2028])
def test_full_year_standard_and_special_periods(year):
    day=date(year,1,1)
    while day.year==year:
        if day.weekday() not in (1,4):
            with pytest.raises(ValueError):config.period_for(day)
        else:
            start,end=config.period_for(day)
            assert end==day-timedelta(days=1)
            assert (end-start).days+1 == (4 if day.weekday()==1 else 3)
            assert start.weekday() == (4 if day.weekday()==1 else 1)
            assert config.client_period_for(day,'Тестовая фирма','1')==(start,end)
            for ident in ('78749','78756'):
                special=config.client_period_for(day,'Переименованная фирма',ident)
                if day.weekday()==4:assert special is None
                else:assert special==(day-timedelta(days=7),end)
        day+=timedelta(days=1)


@pytest.mark.parametrize('seed',range(16))
def test_shuffled_pagination_responses_need_all_offsets(seed):
    rng=Random(2100+seed); evidence=_PaginationEvidence()
    size=rng.randrange(2,12);count=rng.randrange(3,14)
    chunks=[];expected={}
    for page_index in range(count):
        items=[{'CLIENT_ID':page_index*size+i+1,'CLIENT_NAME':f'Фирма {page_index*size+i+1}'} for i in range(size)]
        expected.update({str(o['CLIENT_ID']):o['CLIENT_NAME'] for o in items})
        chunks.append((page_index*size,{'success':True,'data':{'items':items,'count':size,'more':page_index<count-1}}))
    rng.shuffle(chunks)
    for index,(offset,data) in enumerate(chunks):
        evidence.add(offset,data)
        if index<count-1:assert evidence.complete_ids() is None
    assert evidence.complete_ids()==expected
    # Re-emitting the identical response is allowed; changed data is not.
    offset,data=chunks[0];evidence.add(offset,data)
    data['data']['items'][0]['CLIENT_NAME']='Другая фирма'
    with pytest.raises(GloProError):evidence.add(offset,data)


class EventPage(DownloadPage):
    """Emit typed context events at a virtual time, preserving callback cleanup."""
    def __init__(self, events):
        super().__init__();self.typed=list(events)
    def emit_due(self):
        pending=[]
        for due,kind,value in self.typed:
            if due<=self.clock:
                callback=self.context.listeners.get(kind)
                if callback:callback(value)
            else:pending.append((due,kind,value))
        self.typed=pending


def download(connector,page,destination,monkeypatch):
    monkeypatch.setattr(connector,'_download_parameters',lambda p:dict(REPORT_PARAMETERS))
    monkeypatch.setattr(glopro.time,'monotonic',lambda:page.clock)
    return connector._download(page,destination)


@pytest.mark.parametrize('delay',[0,0.2,19.8,20,20.2,45,119.8,120,124,130])
@pytest.mark.parametrize('noise',['none','foreign','aborted'])
def test_generation_time_and_context_event_matrix(delay,noise,tmp_path,monkeypatch):
    source=tmp_path/'source.xlsx';workbook(source)
    events=[]
    if noise=='foreign':
        events=[(0,'response',SimpleNamespace(url=report_url(client_choose_single='999'),status=500)),
                (0,'download',Download(source,report_url(period_start='2020-01-01')))]
    if noise=='aborted':events=[(0,'requestfailed',SimpleNamespace(url=report_url(),failure='net::ERR_ABORTED'))]
    events.append((delay,'download',Download(source)))
    page=EventPage(events);target=tmp_path/'report.xlsx';target.write_bytes(b'previous successful file')
    before=target.read_bytes();connector=GloProConnector('fixture','fixture',timeout=120)
    if delay<=124:
        metadata=download(connector,page,target,monkeypatch)
        assert target.read_bytes()==source.read_bytes()
        assert metadata['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        with pytest.raises(GloProError):download(connector,page,target,monkeypatch)
        assert target.read_bytes()==before
    assert page.generate_count==1 and not page.context.listeners
    page.clock=1000;page.emit_due() # late events cannot mutate a finished download
    assert not page.context.listeners and not list(tmp_path.glob('*.partial'))


@pytest.mark.parametrize('failure',['http429','http500','network','duplicate','save','interrupted','empty','html','badzip','crc','missing_sheet'])
def test_failed_fresh_download_is_atomic_and_never_replayed(failure,tmp_path,monkeypatch):
    source=tmp_path/'source.xlsx';workbook(source)
    if failure=='empty':source.write_bytes(b'')
    if failure=='html':source.write_bytes(b'<html>login</html>')
    if failure=='badzip':source.write_bytes(b'PK\x03\x04broken')
    if failure=='missing_sheet':
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('[Content_Types].xml','x');z.writestr('xl/workbook.xml','x')
    if failure=='crc':
        data=bytearray(source.read_bytes())
        with zipfile.ZipFile(source) as z:
            info=z.getinfo('xl/workbook.xml');index=info.header_offset+30+len(info.filename.encode())+len(info.extra)
        data[index]^=1;source.write_bytes(data)
    item=Download(source)
    if failure=='save':
        def fail_save(path):Path(path).write_bytes(b'partial');raise OSError('PRIVATE_CALL_LOG')
        item.save_as=fail_save
    if failure=='interrupted':item.failure=lambda:'PRIVATE_SIGNED_URL'
    events=[(0,'download',item)]
    if failure=='duplicate':events.append((0,'download',Download(source)))
    if failure.startswith('http'):events=[(0,'response',SimpleNamespace(url=report_url(),status=int(failure[4:])))]
    if failure=='network':events=[(0,'requestfailed',SimpleNamespace(url=report_url(),failure='net::ERR_FAILED PRIVATE'))]
    page=EventPage(events);target=tmp_path/'previous.xlsx';target.write_bytes(b'preserve me')
    with pytest.raises(GloProError) as error:
        download(GloProConnector('x','y'),page,target,monkeypatch)
    assert 'PRIVATE' not in str(error.value)
    assert target.read_bytes()==b'preserve me'
    assert not page.context.listeners and page.generate_count==1
    assert not list(tmp_path.glob('*.partial'))


@pytest.mark.parametrize('seed',range(20))
def test_download_url_scope_permutations(seed):
    rng=Random(9100+seed);pairs=report_query();rng.shuffle(pairs)
    index=rng.randrange(1,100)
    pairs=[(k.replace('additional[0]',f'additional[{index}]'),v) for k,v in pairs]
    assert glopro._matches_report_download(report_url(pairs),REPORT_PARAMETERS)
    critical=[(k,v) for k,v in pairs if not k.endswith('[weight]')]
    for field,value in critical:
        altered=[(k,'999' if v!='999' else '998') if k==field else (k,v) for k,v in pairs]
        assert not glopro._matches_report_download(report_url(altered),REPORT_PARAMETERS)
        assert not glopro._matches_report_download(report_url(pairs+[(field,value)]),REPORT_PARAMETERS)


@pytest.mark.parametrize('method',['GET','POST'])
@pytest.mark.parametrize('transport',['api','direct'])
@pytest.mark.parametrize('failure',[False,True])
def test_bridge_retries_and_budgets(method,transport,failure):
    route=fixtures.GloProTests.Route(url=report_url(),fail=failure);route.request.method=method
    glopro._route_request(route,transport,report_timeout=120000)
    fetches=[kw for name,kw in route.calls if name=='fetch']
    if transport=='direct':assert not fetches
    else:
        assert len(fetches)==1 and fetches[0]['max_retries']==0 and fetches[0]['max_redirects']==0
        assert fetches[0]['timeout']==(120000 if method=='GET' else 20000)


@pytest.mark.parametrize('ids',[['10','10'],['10','20','10']])
def test_duplicate_contract_selector_cannot_generate_same_report_twice(ids,monkeypatch):
    connector=GloProConnector('x','y');monkeypatch.setattr(connector,'_goto',Mock())
    p=Mock();p.locator.return_value.locator.return_value.evaluate_all.return_value=ids
    with pytest.raises(GloProError):connector._contracts(p,{'id':'1','url':'https://lk.glopro.ru/clients/client/1'})


@pytest.mark.parametrize('last_failure',[False,True])
def test_all_clients_and_contracts_complete_before_completion_marker(tmp_path,monkeypatch,last_failure):
    connector=GloProConnector('x','y');monkeypatch.setattr(connector,'_browser_page',lambda:nullcontext(object()))
    monkeypatch.setattr(connector,'_login',Mock())
    clients=[{'id':str(i),'name':f'Фирма {i:02}'} for i in range(1,18)]
    monkeypatch.setattr(connector,'_list_clients',lambda p:list(reversed(clients)))
    monkeypatch.setattr(connector,'_contracts',lambda p,c:[{'id':f"{c['id']}{i}"} for i in (1,2)])
    visited=[]
    def prepare(p,c,contract,first,last):
        visited.append((c['id'],contract['id'],str(first),str(last)))
        if last_failure and c['id']=='17' and contract['id']=='172':raise GloProError('last contract failed')
        return {'checked':True,'activity':{'has_operations': None,'requires_period_report':True}}
    monkeypatch.setattr(connector,'_prepare_report',prepare)
    def save(p,path):workbook(path);return glopro._validate_workbook(path)
    monkeypatch.setattr(connector,'_download',save)
    events=[]
    if last_failure:
        with pytest.raises(GloProError):connector.download_reports('2026-09-25','2026-09-28',tmp_path,progress=events.append,run_date='2026-09-29')
        assert not any(e.get('activity_check_complete') for e in events)
    else:
        result=connector.download_reports('2026-09-25','2026-09-28',tmp_path,progress=events.append,run_date='2026-09-29')
        assert len(result)==34 and events[-1]['activity_check_complete']
        assert len({r['path'] for r in result})==34
    assert len(visited)==34 and len(set(visited))==34
    assert all(v[2:]==('2026-09-25','2026-09-28') for v in visited)


@pytest.mark.parametrize('seed',range(3))
def test_repeated_glopro_pipeline_rejects_last_file_failure(isolated_engine,tmp_path,monkeypatch,seed):
    """Three varied 13-run sequences; previous successful artifacts stay immutable."""
    import json
    data,operators=isolated_engine
    notes=tmp_path/'notes'
    settings=operators/'glopro.md'
    settings.write_text(settings.read_text().replace('rules: {}',f'obsidian_output: {notes}\nrules: {{}}'))
    rng=Random(101+seed)
    sequence=['good']+[rng.choice(['last_error','invalid_last','period_last','duplicate','good']) for _ in range(12)]
    mode=['good']
    def handler(conf,record,directory,progress):
        sources=[]
        for i in range(3):
            if i==2 and mode[0]=='last_error':raise GloProError('Последняя фирма недоступна')
            path=source_file(directory/f'{i}.xlsx',client=f'Фирма {i}',
                             tx_date=date(2026,9,14) if i==2 and mode[0]=='period_last' else date(2026,9,17),
                             supplier=8000+i,customer=8300+i)
            if i==2 and mode[0]=='invalid_last':path.write_bytes(b'not an Excel workbook')
            sources.append({'path':str(path),'client':f'Фирма {i}','client_id':str(i+1),'contract_id':str(i+10)})
        if mode[0]=='duplicate':sources.append(dict(sources[0]))
        progress({'stage':'downloaded','activity_check_complete':True})
        return sources
    monkeypatch.setitem(engine.HANDLERS,'glopro',handler)
    note=notes/'Активация — 18.09.2026.md';before=None;preserved={}
    for mode[0] in sequence:
        run=engine.submit('glopro','2026-09-18')
        root=data/'runs'/run['id']
        if mode[0]=='good':
            assert run['status']=='completed'
            audit=json.loads((root/'Проверка расчётов.json').read_text())
            from decimal import Decimal
            assert sum(Decimal(r['totals']['supplier_basis']) for r in audit['reports'])==Decimal(24003)
            assert sum(Decimal(r['totals']['customer_total']) for r in audit['reports'])==Decimal(24903)
            if before is None:note.write_text(note.read_text()+'\n## Ручной раздел\nСохранить.\n')
            before=note.read_bytes()
            preserved.update({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()})
        else:
            assert run['status'] in {'failed','needs_review'} and not run['report']
            assert note.read_bytes()==before
        assert 'Ручной раздел' in note.read_text()
        assert all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in preserved.items())
