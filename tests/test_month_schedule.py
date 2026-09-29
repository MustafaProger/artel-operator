"""Accounting dates remain contiguous when execution dates move to month day 1."""
from datetime import date, datetime, timedelta, timezone
import json

import pytest
import yaml

from operator_app.config import (
    ROOT, client_period_for, latest_run_date, next_run, parse_operator, period_for,
    plan_for_clients, run_plan, schedule_matches,
)


def glopro(active_from="2026-09-29"):
    return {
        "id": "test", "name": "Test", "kind": "glopro", "enabled": True,
        "schedule": {"days": ["tue", "fri"], "time": "07:00", "timezone": "Europe/Moscow",
                     "month_boundary": "close_previous_month", "month_boundary_from": active_from},
        "clients": [], "excluded_clients": [],
    }


def dates(first, last):
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)


def test_required_september_october_plan():
    conf = glopro()
    expected = {
        date(2026, 9, 29): (["2026-09-25", "2026-09-28"], ["2026-09-22", "2026-09-28"], "regular", None),
        date(2026, 10, 1): (["2026-09-29", "2026-09-30"], None, "month_close", "2026-10-02"),
        date(2026, 10, 6): (["2026-10-01", "2026-10-05"], ["2026-09-29", "2026-10-05"], "regular", None),
        date(2026, 10, 9): (["2026-10-06", "2026-10-08"], None, "regular", None),
    }
    for day, (ordinary, weekly, kind, moved_from) in expected.items():
        plan = run_plan(conf, day)
        assert (plan["ordinary_period"], plan["weekly_period"], plan["kind"], plan["moved_from"]) == (ordinary, weekly, kind, moved_from)
        assert [plan["period_start"], plan["period_end"]] == ordinary
        assert schedule_matches(conf, day)
        assert json.loads(json.dumps(plan)) == plan
    assert not schedule_matches(conf, date(2026, 10, 2))
    with pytest.raises(ValueError, match=r"2026-10-02.*2026-10-01.*2026-10-06"):
        run_plan(conf, date(2026, 10, 2))
    assert client_period_for(date(2026, 10, 1), "Обычная", conf=conf) == (date(2026, 9, 29), date(2026, 9, 30))
    assert client_period_for(date(2026, 10, 1), "Китай", conf=conf) is None
    assert client_period_for(date(2026, 10, 6), "Обычная", conf=conf) == (date(2026, 10, 1), date(2026, 10, 5))
    assert client_period_for(date(2026, 10, 6), "Китай", conf=conf) == (date(2026, 9, 29), date(2026, 10, 5))


@pytest.mark.parametrize("month_start", [
    date(2027, 2, 1),  # Monday: ordinary Tue moves, weekly Tue remains.
    date(2026, 12, 1), # Tuesday: usual run also closes the month.
    date(2027, 9, 1),  # Wednesday
    date(2026, 10, 1), # Thursday
    date(2027, 1, 1),  # Friday: previous year
    date(2027, 5, 1),  # Saturday
    date(2026, 11, 1), # Sunday
])
def test_month_start_on_every_weekday(month_start):
    conf = glopro()
    original = month_start
    while original.weekday() not in (1, 4):
        original += timedelta(days=1)
    previous_slot, _ = period_for(original)
    plan = run_plan(conf, month_start)
    assert plan["ordinary_period"] == [previous_slot.isoformat(), (month_start - timedelta(days=1)).isoformat()]
    assert plan["kind"] == "month_close"
    assert plan["moved_from"] == (original.isoformat() if original != month_start else None)
    following = original + timedelta(days=3 if original.weekday() == 1 else 4)
    assert run_plan(conf, following)["ordinary_period"] == [month_start.isoformat(), (following - timedelta(days=1)).isoformat()]
    if month_start != original and original.weekday() == 1:
        weekly_plan = run_plan(conf, original)
        assert weekly_plan["kind"] == "weekly_only"
        assert weekly_plan["ordinary_period"] is None
        assert weekly_plan["weekly_period"] == [(original - timedelta(days=7)).isoformat(), (original - timedelta(days=1)).isoformat()]
        assert weekly_plan["ordinary_moved_to"] == month_start.isoformat()
        assert client_period_for(original, "Обычная", conf=conf) is None
        assert client_period_for(original, "Китай", conf=conf) is not None
    elif month_start != original:
        assert not schedule_matches(conf, original)


def test_leap_february_and_year_change():
    conf = glopro("2023-01-01")
    assert run_plan(conf, date(2024, 3, 1))["ordinary_period"] == ["2024-02-27", "2024-02-29"]
    assert run_plan(conf, date(2025, 3, 1))["ordinary_period"] == ["2025-02-28", "2025-02-28"]
    assert run_plan(conf, date(2025, 3, 7))["ordinary_period"] == ["2025-03-01", "2025-03-06"]
    assert run_plan(conf, date(2026, 1, 1))["ordinary_period"] == ["2025-12-30", "2025-12-31"]
    assert run_plan(conf, date(2026, 1, 6))["ordinary_period"] == ["2026-01-01", "2026-01-05"]


def test_multiple_years_no_gaps_duplicates_or_cross_month_ordinary_periods():
    conf = glopro("2023-01-01")
    previous_end = None
    ordinary_days = set()
    weekly_runs = []
    for day in dates(date(2023, 1, 1), date(2029, 1, 1)):
        if not schedule_matches(conf, day):
            continue
        plan = run_plan(conf, day)
        if plan["weekly_period"]:
            weekly_runs.append(day)
            assert plan["weekly_period"] == [(day - timedelta(days=7)).isoformat(), (day - timedelta(days=1)).isoformat()]
        if not plan["ordinary_period"]:
            continue
        first, last = map(date.fromisoformat, plan["ordinary_period"])
        assert first <= last < day
        assert (first.year, first.month) == (last.year, last.month)
        if previous_end:
            assert first == previous_end + timedelta(days=1)
        covered = set(dates(first, last))
        assert ordinary_days.isdisjoint(covered)
        ordinary_days.update(covered)
        previous_end = last
    assert weekly_runs == [day for day in dates(date(2023, 1, 1), date(2029, 1, 1)) if day.weekday() == 1]
    assert set(dates(date(2023, 1, 1), date(2028, 12, 31))) <= ordinary_days


def test_activation_preserves_old_calendar_and_partial_past_boundary():
    conf = glopro()
    for day in dates(date(2026, 1, 1), date(2026, 9, 28)):
        assert schedule_matches(conf, day) == (day.weekday() in (1, 4))
        if day.weekday() in (1, 4):
            assert period_for(day, conf) == period_for(day)
    conf = glopro("2026-10-02")
    assert not schedule_matches(conf, date(2026, 10, 1))
    assert period_for(date(2026, 10, 2), conf) == (date(2026, 9, 29), date(2026, 10, 1))
    assert period_for(date(2026, 10, 6), conf) == (date(2026, 10, 2), date(2026, 10, 5))


@pytest.mark.parametrize("clients,exclusions,weekly_enabled", [
    ([], [], True),
    (["Обычная"], [], False),
    (["Китай"], [], True),
    ([{"id": "78756", "name": "Переименован"}], [], True),
    ([{"id": "111", "name": "Китай"}], [], False),
    ([], [{"id": "78749", "name": "Китай"}], True),
    ([], [{"id": "78749", "name": "Китай"}, {"id": "78756", "name": 'ООО "НК АРТЭЛЬ"'}], False),
    (["Китай"], [{"id": "78749", "name": "Китай"}], False),
])
def test_weekly_only_respects_selected_and_excluded_clients(clients, exclusions, weekly_enabled):
    conf = glopro()
    conf.update(clients=clients, excluded_clients=exclusions)
    assert schedule_matches(conf, date(2026, 11, 3)) is weekly_enabled
    if weekly_enabled:
        assert run_plan(conf, date(2026, 11, 3))["kind"] == "weekly_only"
    elif clients == ["Китай"] and exclusions:
        with pytest.raises(ValueError, match="нет доступных фирм"):
            run_plan(conf, date(2026, 11, 3))
    else:
        with pytest.raises(ValueError, match=r"2026-11-03.*2026-11-01.*2026-11-06"):
            run_plan(conf, date(2026, 11, 3))


def test_next_and_latest_share_calendar_in_moscow():
    conf = glopro()
    assert next_run(conf, datetime(2026, 9, 30, 22, tzinfo=timezone.utc)) == "2026-10-01T07:00:00+03:00"
    assert latest_run_date(datetime(2026, 9, 30, 22, tzinfo=timezone.utc), conf) == date(2026, 10, 1)
    assert next_run(conf, datetime(2026, 10, 1, 4, tzinfo=timezone.utc)) == "2026-10-06T07:00:00+03:00"
    assert latest_run_date(datetime(2026, 10, 2, 12, tzinfo=timezone.utc), conf) == date(2026, 10, 1)
    assert next_run(conf, datetime(2026, 10, 30, 4, tzinfo=timezone.utc)) == "2026-11-01T07:00:00+03:00"
    assert next_run(conf, datetime(2026, 11, 1, 4, tzinfo=timezone.utc)) == "2026-11-03T07:00:00+03:00"
    conf["clients"] = ["Обычная"]
    assert next_run(conf, datetime(2026, 11, 1, 4, tzinfo=timezone.utc)) == "2026-11-06T07:00:00+03:00"
    assert latest_run_date(datetime(2026, 11, 3, 12, tzinfo=timezone.utc), conf) == date(2026, 11, 1)
    conf["enabled"] = False
    assert next_run(conf) is None


def test_only_weekly_selection_does_not_add_empty_closure_runs():
    conf = glopro()
    conf["clients"] = ["Китай"]
    assert not schedule_matches(conf, date(2026, 10, 1))
    assert not schedule_matches(conf, date(2026, 10, 2))
    plan = run_plan(conf, date(2026, 10, 6))
    assert plan["ordinary_period"] is None
    assert plan["weekly_period"] == ["2026-09-29", "2026-10-05"]
    assert "ordinary_moved_to" not in plan
    assert next_run(conf, datetime(2026, 9, 29, 4, tzinfo=timezone.utc)) == "2026-10-06T07:00:00+03:00"
    # Before activation both historic standard slots keep their original plan.
    assert run_plan(conf, date(2026, 9, 25))["ordinary_period"] == ["2026-09-22", "2026-09-24"]


def test_all_selected_clients_excluded_has_no_available_new_runs():
    conf = glopro()
    conf["clients"] = ["Китай"]
    conf["excluded_clients"] = [{"id": "78749", "name": "Китай"}]
    assert next_run(conf, datetime(2026, 10, 1, tzinfo=timezone.utc)) is None
    with pytest.raises(ValueError, match="нет доступной даты"):
        latest_run_date(datetime(2026, 10, 15, tzinfo=timezone.utc), conf)


def test_policy_is_explicit_and_legacy_signatures_keep_original_periods():
    conf = glopro()
    conf["schedule"].pop("month_boundary")
    conf["schedule"].pop("month_boundary_from")
    assert not schedule_matches(conf, date(2026, 10, 1))
    assert run_plan(conf, date(2026, 10, 2))["ordinary_period"] == ["2026-09-29", "2026-10-01"]
    assert period_for(date(2026, 10, 2)) == (date(2026, 9, 29), date(2026, 10, 1))
    assert client_period_for(date(2026, 10, 2), "Обычная") == period_for(date(2026, 10, 2))
    with pytest.raises(ValueError):
        period_for(date(2026, 10, 1))


@pytest.mark.parametrize("schedule_update,kind", [
    ({"month_boundary": "split"}, "glopro"),
    ({"month_boundary_from": "bad-date"}, "glopro"),
    ({"month_boundary_from": None}, "glopro"),
    ({"days": ["tue"]}, "glopro"),
    ({"days": ["tue", "fri", "fri"]}, "glopro"),
    ({"timezone": "UTC"}, "glopro"),
    ({}, "yandex"),
])
def test_policy_validation(schedule_update, kind):
    conf = glopro()
    conf["kind"] = kind
    conf["schedule"].update(schedule_update)
    with pytest.raises(ValueError, match="month_boundary"):
        parse_operator("---\n" + yaml.safe_dump(conf) + "---\nDescription")


def test_missing_activation_or_orphan_activation_rejected():
    for missing in ("month_boundary", "month_boundary_from"):
        conf = glopro()
        conf["schedule"].pop(missing)
        with pytest.raises(ValueError, match="month_boundary"):
            parse_operator("---\n" + yaml.safe_dump(conf) + "---\nDescription")


def test_current_definition_opted_in_but_other_definitions_unaffected():
    conf = parse_operator((ROOT / "operators/glopro.md").read_text())
    assert conf["schedule"]["month_boundary"] == "close_previous_month"
    assert conf["schedule"]["month_boundary_from"] == "2026-09-29"
    for path in (ROOT / "operators").glob("*.md"):
        if path.name != "glopro.md":
            assert "month_boundary" not in parse_operator(path.read_text())["schedule"]


def test_yandex_and_seo_keep_original_calendars():
    yandex = {"kind": "yandex", "enabled": True,
              "schedule": {"days": ["tue"], "time": "07:00", "timezone": "Europe/Moscow"}}
    assert not schedule_matches(yandex, date(2026, 10, 1))
    assert run_plan(yandex, date(2026, 10, 6))["weekly_period"] == ["2026-09-29", "2026-10-05"]
    assert latest_run_date(datetime(2026, 10, 2, tzinfo=timezone.utc), yandex) == date(2026, 9, 29)
    seo = {"kind": "seo", "enabled": True,
           "schedule": {"every_days": 3, "anchor_date": "2026-09-29", "time": "07:00", "timezone": "Europe/Moscow"}}
    assert not schedule_matches(seo, date(2026, 10, 1))
    assert run_plan(seo, date(2026, 10, 2))["period_start"] == "2026-10-02"
    assert latest_run_date(datetime(2026, 10, 4, tzinfo=timezone.utc), seo) == date(2026, 10, 2)


def test_saved_weekly_only_scope_retains_latent_ordinary_boundary_after_policy_change():
    conf = glopro()
    conf["clients"] = ["Китай"]
    saved = run_plan(conf, date(2026, 10, 6))
    assert saved["ordinary_period"] is None
    assert saved["calendar_periods"]["ordinary"] == ["2026-10-01", "2026-10-05"]
    original = json.dumps(saved, sort_keys=True)
    conf["clients"] = []
    conf["schedule"].pop("month_boundary")
    conf["schedule"].pop("month_boundary_from")
    restored = plan_for_clients(saved, conf)
    assert restored["ordinary_period"] == ["2026-10-01", "2026-10-05"]
    assert restored["kind"] == "regular"
    assert restored["period_start"] == "2026-10-01"
    # Fresh legacy calculation would incorrectly move the start to Oct 2.
    assert run_plan(conf, date(2026, 10, 6))["period_start"] == "2026-10-02"
    restored["ordinary_period"][0] = "changed"
    assert restored["calendar_periods"]["ordinary"][0] == "2026-10-01"
    restored["calendar_periods"]["weekly"][0] = "changed"
    assert json.dumps(saved, sort_keys=True) == original


def test_saved_ordinary_only_scope_retains_latent_weekly_period():
    conf = glopro()
    conf["clients"] = ["Обычная"]
    saved = run_plan(conf, date(2026, 10, 6))
    assert saved["weekly_period"] is None
    assert saved["calendar_periods"]["weekly"] == ["2026-09-29", "2026-10-05"]
    conf["clients"] = ["Китай"]
    # Current schedule differences cannot disable a previously saved Tuesday.
    conf["schedule"]["days"] = ["fri"]
    restored = plan_for_clients(saved, conf)
    assert restored["ordinary_period"] is None
    assert restored["weekly_period"] == ["2026-09-29", "2026-10-05"]
    assert restored["period_start"] == "2026-09-29"
    assert restored["kind"] == "weekly_only"
    assert restored["moved_from"] is None


def test_moved_tuesday_has_no_latent_ordinary_interval_even_if_policy_changes():
    conf = glopro()
    saved = run_plan(conf, date(2026, 11, 3))
    assert saved["calendar_periods"]["ordinary"] is None
    assert saved["calendar_periods"]["ordinary_moved_to"] == "2026-11-01"
    conf["schedule"].pop("month_boundary")
    conf["schedule"].pop("month_boundary_from")
    assert plan_for_clients(saved, conf)["ordinary_period"] is None
    conf["clients"] = ["Обычная"]
    with pytest.raises(ValueError, match="нет доступного периода"):
        plan_for_clients(saved, conf)


def test_restoring_ordinary_client_restores_month_close_kind_on_natural_tuesday():
    conf = glopro()
    conf["clients"] = ["Китай"]
    saved = run_plan(conf, date(2026, 12, 1))
    assert saved["kind"] == "weekly_only"
    assert saved["calendar_periods"]["kind"] == "month_close"
    conf["clients"] = []
    restored = plan_for_clients(saved, conf)
    assert restored["kind"] == "month_close"
    assert restored["ordinary_period"] == ["2026-11-27", "2026-11-30"]
    assert restored["weekly_period"] == ["2026-11-24", "2026-11-30"]


def test_projection_keeps_saved_moved_from_and_rejects_clients_without_interval():
    conf = glopro()
    saved = run_plan(conf, date(2026, 10, 1))
    assert saved["calendar_periods"]["moved_from"] == "2026-10-02"
    conf["clients"] = ["Обычная"]
    conf["schedule"]["month_boundary_from"] = "2030-01-01"
    projected = plan_for_clients(saved, conf)
    assert projected["moved_from"] == "2026-10-02"
    assert projected["kind"] == "month_close"
    assert projected["ordinary_period"] == ["2026-09-29", "2026-09-30"]
    conf["clients"] = ["Китай"]
    with pytest.raises(ValueError, match="нет доступного периода"):
        plan_for_clients(saved, conf)
    conf["clients"] = ["Обычная"]
    conf["excluded_clients"] = [{"id": "123", "name": "Обычная"}]
    with pytest.raises(ValueError, match="нет доступного периода"):
        plan_for_clients(saved, conf)


def test_legacy_snapshots_keep_old_ordinary_projection_contract():
    conf = glopro()
    conf["schedule"].pop("month_boundary")
    conf["schedule"].pop("month_boundary_from")
    saved = run_plan(conf, date(2026, 10, 6))
    assert saved["calendar_periods"]["filter_ordinary"] is False
    conf = glopro()
    conf["clients"] = ["Китай"]
    assert plan_for_clients(saved, conf)["ordinary_period"] == ["2026-10-02", "2026-10-05"]
    # Older serialized plans can preserve only fields they actually recorded.
    saved.pop("calendar_periods")
    saved["weekly_period"] = None
    conf["clients"] = []
    projected = plan_for_clients(saved, conf)
    assert projected["ordinary_period"] == ["2026-10-02", "2026-10-05"]
    assert projected["weekly_period"] is None
