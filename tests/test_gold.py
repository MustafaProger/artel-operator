from datetime import date
from decimal import Decimal
import pytest
from openpyxl import Workbook
from operator_app.calculator import calculate_workbook, merge_reports, CalculationError, company_identity

CLIENT = 'ООО "ТЕСТ"'
STATION = 'АЗС ЛУКОЙЛ 50264'
ADDRESS = 'Россия, Московская область, г. Реутов, МКАД, 3 км, внешняя сторона (вл5)'


def source(tmp_path, discount=4, day=date(2026, 10, 28), address=ADDRESS, card='123', refund=False):
    w = Workbook(); s = w.active
    s.append(['Клиент', 'Дата', 'Услуга', 'Количество', 'Стоимость ТО', 'Стоимость', 'Скидка, %', 'Карта', 'ТО', 'Адрес ТО'])
    s.append([CLIENT, day, 'ДТ ЭКТО', 100, 8000, 7680, discount, card, STATION, address])
    if refund:
        s.append([CLIENT, day, 'ДТ ЭКТО', -10, -800, -768, 4.04, card, STATION, address])
    p = tmp_path / 'gold.xlsx'; w.save(p); return p


def rules(kind):
    return {'card_evidence': {company_identity(CLIENT): {'123': {kind: {'source': 'test', 'row': 1}}}}}


@pytest.mark.parametrize('kind,price', [('virtual', '74.00'), ('plastic', '74.40')])
def test_gold_signed_amounts_and_merge(tmp_path, kind, price):
    r = calculate_workbook(source(tmp_path, refund=True), rules(kind))
    assert r['fuels'][0]['supplier_price'] == price
    assert r['fuels'][0]['customer_total'] == '6912.00'
    assert r['fuels'][0]['litres'] == '90.000'
    assert r['audit']['raw_fuels'][0]['supplier_basis_by_card_type'] == {'gold_' + kind: '7200'}
    merged = merge_reports([r, r])
    assert merged['fuels'][0]['supplier_price'] == price
    assert merged['fuels'][0]['customer_total'] == '13824.00'


@pytest.mark.parametrize('kwargs', [{'day': date(2026, 10, 26)}, {'address': 'Другой адрес'}, {'card': 'unknown'}, {'discount': 5}])
def test_gold_rejects_unconfirmed(tmp_path, kwargs):
    with pytest.raises(CalculationError):
        calculate_workbook(source(tmp_path, **kwargs), rules('plastic'))


def test_gold_rejects_conflicting_card_evidence(tmp_path):
    evidence = rules('plastic')
    evidence['card_evidence'][company_identity(CLIENT)]['123']['virtual'] = {}
    with pytest.raises(CalculationError, match='однозначно'):
        calculate_workbook(source(tmp_path), evidence)


def test_gold_can_use_same_card_regular_operation(tmp_path):
    from openpyxl import load_workbook
    p = source(tmp_path); w = load_workbook(p); s = w.active
    s.append([CLIENT, date(2026,10,28), 'ДТ', 100, 8000, 8160, -2, '123', 'Обычная АЗС', 'Адрес'])
    w.save(p)
    r = calculate_workbook(p)
    assert r['card_type'] == 'plastic'
    assert r['fuels'][0]['supplier_price'] == '77.40'
    assert r['fuels'][0]['customer_total'] == '15840.00'


def test_special_customer_keeps_existing_rule(tmp_path):
    p = source(tmp_path, address='Другой адрес')
    r = calculate_workbook(p, {'special_clients': [CLIENT]})
    assert r['fuels'][0]['supplier_price'] == '79.20'
    assert r['fuels'][0]['customer_total'] == '8160.00'


def test_evidence_manifest_is_hash_verified(tmp_path):
    import hashlib, json
    from operator_app.gold import load_card_evidence
    p = source(tmp_path, discount=-2)
    (tmp_path / 'glopro-card-evidence.json').write_text(json.dumps([{'path': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}]))
    assert 'plastic' in load_card_evidence(tmp_path)[company_identity(CLIENT)]['123']
    p.write_bytes(b'changed')
    with pytest.raises(CalculationError, match='Изменился'):
        load_card_evidence(tmp_path)


def test_hamlet_abbreviation_preserves_station_identity():
    from operator_app.gold import station_matches
    addr = 'Россия, Ростовская область, Миллеровский р-он, хут. Грай-Воронец, М-4 «Дон», 827 км, слева'
    assert station_matches('АЗС ЛУКОЙЛ 61326', addr)
    assert not station_matches('АЗС ЛУКОЙЛ 61326', addr.replace('слева', 'справа'))


@pytest.mark.parametrize('refund_station,expected', [('Та же АЗС', True), ('Другая АЗС', False)])
def test_rounded_refund_only_with_paired_purchase(tmp_path, refund_station, expected):
    from datetime import datetime
    day = datetime(2026, 11, 4, 11, 28, 44)
    book = Workbook(); sheet = book.active
    sheet.append(['Клиент','Дата','Услуга','Количество','Стоимость ТО','Стоимость','Скидка, %',
                  'Карта','ТО','Адрес ТО','Цена ТО','Цена','Операция'])
    sheet.append([CLIENT,day,'ДТ',200,16216,16540,-2,'123','Та же АЗС','Адрес',81.08,82.70,'Дебет'])
    sheet.append([CLIENT,day,'ДТ',-0.01,-0.81,-0.83,-2.47,'123',refund_station,'Адрес',81.08,82.70,'Возврат'])
    path = tmp_path / 'return.xlsx'; book.save(path)
    if expected:
        report = calculate_workbook(path)
        assert report['audit']['paired_refunds'] == [{'row': 3, 'debit_row': 2, 'card_type': 'plastic'}]
        assert report['fuels'][0]['customer_total'] == '16539.17'
    else:
        with pytest.raises(CalculationError):
            calculate_workbook(path)
