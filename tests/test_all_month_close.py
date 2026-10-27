from copy import deepcopy
from datetime import date, timedelta
import pytest
from operator_app import config, engine, storage
from test_month_schedule import glopro, dates
from test_month_schedule_engine import month_engine
from test_weekly import weekly_file
from test_engine import isolated_engine


def current(kind='glopro'):
    conf = glopro('2023-01-01')
    conf['schedule']['month_close_all_from'] = '2023-01-01'
    if kind == 'yandex':
        conf['kind'] = kind
        conf['schedule']['days'] = ['tue']
        conf['schedule'].pop('month_boundary')
        conf['schedule'].pop('month_boundary_from')
    return conf


@pytest.mark.parametrize('kind', ['glopro', 'yandex'])
def test_every_weekly_day_covered_once_in_one_month(kind):
    conf = current(kind)
    previous = None
    for day in dates(date(2023, 1, 1), date(2029, 1, 1)):
        if not config.schedule_matches(conf, day):
            continue
        period = config.run_plan(conf, day)['weekly_period']
        if not period:
            continue
        start, end = map(date.fromisoformat, period)
        assert start <= end < day
        assert (start.year, start.month) == (end.year, end.month)
        if previous:
            assert start == previous + timedelta(days=1)
        previous = end
    assert previous == date(2028, 12, 31)


def test_close_ignores_selection_and_exclusions_but_regular_respects_them():
    conf = current()
    conf.update(clients=['Обычная'], excluded_clients=[{'id':'78749', 'name':'Китай'}])
    plan = config.run_plan(conf,date(2026,10,1))
    assert plan['weekly_period'] == ['2026-09-29','2026-09-30']
    assert config.run_plan(conf,date(2026,10,6))['weekly_period'] is None


def test_explicit_migration_preserves_old_record(month_engine):
    _, operators, _ = month_engine
    path=operators/'glopro.md'
    old_conf=config.get_operator('glopro')
    old_plan=config.run_plan(old_conf,date(2026,10,6))
    old=storage.create_run('glopro','2026-10-06','2026-10-01','2026-10-05','manual',plan=old_plan,scope=engine._run_scope(old_conf))
    old.update(status='completed');storage.save_run(old)
    path.write_text(path.read_text().replace('  days: [tue, fri]', "  days: [tue, fri]\n  month_close_all_from: '2026-09-29'"))
    conf=config.get_operator('glopro')
    plan=engine.plan_run(conf,date(2026,10,6))
    assert plan['weekly_period']==['2026-10-01','2026-10-05']
    assert plan['supersedes_calendar_run']==old['id']
    assert storage.get_run(old['id'])['plan']==old_plan


def test_month_close_download_gets_all_clients(month_engine, monkeypatch):
    _, operators, _ = month_engine
    path=operators/'glopro.md'
    path.write_text(path.read_text().replace('  days: [tue, fri]', "  days: [tue, fri]\n  month_close_all_from: '2026-09-29'").replace('clients: []', 'clients: [Обычная]',1))
    def download(conf,record,directory,progress):
        assert conf['clients']==[] and conf['excluded_clients']==[]
        assert record['plan']['weekly_period']==['2026-09-29','2026-09-30']
        path=weekly_file(directory/'china.xlsx',start='29.09.2026',end='30.09.2026')
        return [{'path':str(path),'client':'Китай','client_id':'78749'}]
    monkeypatch.setitem(engine.HANDLERS,'glopro',download)
    record=engine.submit('glopro','2026-10-01')
    assert record['status']=='completed'
    assert record['company_periods']==[{'client':'Китай','period':['2026-09-29','2026-09-30']}]


def test_yandex_submit_preview_and_completed_closure_agree(month_engine, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from test_yandex import yandex_config
    from operator_app.main import operator_plan
    _, operators, frozen = month_engine
    yandex_config(operators,tmp_path/'notes')
    path=operators/'yandex.md'
    text=path.read_text().replace('schedule:', "schedule:\n  month_close_all_from: '2026-09-29'",1)
    path.write_text(text)
    monkeypatch.setitem(engine.HANDLERS,'yandex',engine.yandex_download)
    monkeypatch.setattr(engine,'POOL',SimpleNamespace(submit=lambda *args: None))
    conf=config.get_operator('yandex')
    record=engine.submit('yandex','2026-10-06')
    assert [record['period_start'],record['period_end']]==['2026-10-01','2026-10-05']
    preview=operator_plan(conf,date(2026,10,6),frozen.now())
    assert preview['weekly_period']==record['plan']['weekly_period']
    record.update(status='failed');storage.save_run(record)
    old=storage.create_run('yandex','2026-10-01','2026-09-29','2026-09-30','manual')
    old.update(status='completed',events=[{'activity_check_complete':True}]);storage.save_run(old)
    assert engine._schedule_satisfied(conf,date(2026,10,1))
    with pytest.raises(ValueError,match='Разные месяцы'):
        engine.submit('yandex','2026-10-06',period_start='2026-09-29',period_end='2026-10-05')
    old['trigger']='import'; storage.save_run(old)
    assert not engine._schedule_satisfied(conf,date(2026,10,1))


def test_split_weekly_workbook_rejects_original_cross_month_period(tmp_path):
    from operator_app.calculator import calculate_workbook, CalculationError
    source=weekly_file(tmp_path/'china.xlsx',start='29.09.2026',end='05.10.2026')
    with pytest.raises(CalculationError,match='Период внутри Excel'):
        calculate_workbook(source,{'run_date':'2026-10-06','date_from':date(2026,10,1),
                                  'date_to':date(2026,10,5),'month_close_all_from':'2026-09-29'})
