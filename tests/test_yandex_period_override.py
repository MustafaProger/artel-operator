from datetime import date

import pytest

from operator_app import config


def instruction(override="'2026-10-20': ['2026-10-06', '2026-10-19']"):
    return f"""---
id: yandex
name: Яндекс Заправки
kind: yandex
company: Тестовая организация
enabled: true
schedule:
  days: [tue]
  time: '07:00'
  timezone: Europe/Moscow
  month_close_all_from: '2026-09-29'
  period_overrides:
    {override}
---
Недельные отчёты.
"""


def test_override_only_applies_to_its_date():
    conf = config.parse_operator(instruction())
    for day, expected in [
        (date(2026, 10, 13), ["2026-10-06", "2026-10-12"]),
        (date(2026, 10, 20), ["2026-10-06", "2026-10-19"]),
        (date(2026, 10, 27), ["2026-10-20", "2026-10-26"]),
        (date(2026, 11, 1), ["2026-10-27", "2026-10-31"]),
    ]:
        plan = config.run_plan(conf, day)
        assert [plan["period_start"], plan["period_end"]] == expected
        assert plan["weekly_period"] == expected


@pytest.mark.parametrize("override", [
    "'2026-10-20': ['2026-10-06', '2026-10-20']",
    "'2026-10-20': ['2026-10-20', '2026-10-19']",
    "'2026-10-20': ['2026-09-29', '2026-10-19']",
    "'2026-10-21': ['2026-10-06', '2026-10-20']",
    "'2026-10-20': ['2026-10-06']",
])
def test_invalid_period_is_rejected(override):
    with pytest.raises(ValueError, match="Исключение Яндекса"):
        config.parse_operator(instruction(override))


def test_successful_override_satisfies_schedule(monkeypatch):
    from operator_app import engine, storage

    conf = config.parse_operator(instruction())
    record = {
        "trigger": "manual", "status": "completed",
        "period_start": "2026-10-06", "period_end": "2026-10-19",
        "events": [{"activity_check_complete": True}],
    }
    monkeypatch.setattr(storage, "operator_runs", lambda *args: [record])
    assert engine._schedule_satisfied(conf, date(2026, 10, 20))
    record["period_start"] = "2026-10-13"
    assert not engine._schedule_satisfied(conf, date(2026, 10, 20))
