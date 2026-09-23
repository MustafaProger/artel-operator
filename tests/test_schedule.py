from datetime import date, datetime
from zoneinfo import ZoneInfo
import pytest
import yaml
from operator_app.config import period_for, next_run, parse_operator, ROOT


@pytest.mark.parametrize('day,start,end', [('2026-09-22','2026-09-18','2026-09-21'),('2026-09-25','2026-09-22','2026-09-24'),('2027-01-01','2026-12-29','2026-12-31')])
def test_periods(day,start,end):
    assert period_for(date.fromisoformat(day)) == (date.fromisoformat(start),date.fromisoformat(end))


def test_reject_unscheduled_date():
    with pytest.raises(ValueError):
        period_for(date(2026,9,19))


def test_next_run_moscow_from_utc():
    conf = parse_operator((ROOT/'operators/glopro.md').read_text())
    conf['enabled'] = True
    assert next_run(conf, datetime(2026,9,22,3,59,tzinfo=ZoneInfo('UTC'))) == '2026-09-22T07:00:00+03:00'
    assert next_run(conf, datetime(2026,9,22,4,0,tzinfo=ZoneInfo('UTC'))) == '2026-09-25T07:00:00+03:00'


def test_new_kind_cannot_execute_merely_from_text():
    text = (ROOT/'operators/glopro.md').read_text().replace('kind: glopro','kind: arbitrary').replace('enabled: false','enabled: true')
    with pytest.raises(ValueError):
        parse_operator(text)


def exclusion_config(exclusions, *, present=True):
    settings = {
        "id": "test", "name": "Test", "enabled": False, "kind": "glopro",
        "schedule": {"days": ["tue", "fri"], "time": "07:00", "timezone": "Europe/Moscow"},
    }
    if present:
        settings["excluded_clients"] = exclusions
    return "---\n" + yaml.safe_dump(settings, allow_unicode=True) + "---\nInstructions"


def test_exclusions_default_to_empty_and_preserve_exact_configured_identities():
    assert parse_operator(exclusion_config(None, present=False))["excluded_clients"] == []
    exclusions = [{"id": "78749", "name": "Китай"}, {"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'}]
    assert parse_operator(exclusion_config(exclusions))["excluded_clients"] == exclusions


@pytest.mark.parametrize("exclusions", [
    None, "Китай", {}, ["Китай"], [{}], [{"id": "78749"}], [{"name": "Китай"}],
    [{"id": "", "name": "Китай"}], [{"id": "*", "name": "Китай"}],
    [{"id": 78749, "name": "Китай"}], [{"id": "0", "name": "Китай"}],
    [{"id": "78749", "name": "  "}], [{"id": "78749", "name": "*"}],
    [{"id": "78749", "name": "Китай*"}], [{"id": "78749", "name": True}],
    [{"id": "78749", "name": "Китай", "contains": True}],
    [{"id": "78749", "name": "Китай"}, {"id": "78749", "name": "Другой"}],
])
def test_exclusions_reject_incomplete_broad_or_malformed_selectors(exclusions):
    with pytest.raises(ValueError, match="excluded_clients"):
        parse_operator(exclusion_config(exclusions))
