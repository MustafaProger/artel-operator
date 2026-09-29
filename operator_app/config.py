from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import re
import yaml

from .clients import client_is_excluded, validate_excluded_clients
from .calculator import WEEKLY_CLIENTS, weekly_kind

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("OPERATOR_DATA_DIR", str(ROOT / "data"))).resolve()
OPERATORS = Path(os.environ.get("OPERATOR_CONFIG_DIR", str(ROOT / "operators"))).resolve()
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_operator(markdown: str, expected_id: str | None = None) -> dict:
    if not markdown.startswith("---\n"):
        raise ValueError("Инструкция должна начинаться с YAML-блока между строками ---")
    parts = markdown.split("\n---", 2)
    if len(parts) < 2:
        raise ValueError("Не закрыт YAML-блок")
    try:
        conf = yaml.safe_load(parts[0][4:])
    except yaml.YAMLError as exc:
        raise ValueError("Ошибка YAML: проверьте отступы и кавычки") from exc
    if not isinstance(conf, dict):
        raise ValueError("YAML должен содержать настройки оператора")
    ident = conf.get("id", "")
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", ident):
        raise ValueError("ID: латиница, цифры, дефис; начинать с буквы")
    if expected_id and ident != expected_id:
        raise ValueError("ID в инструкции не совпадает с именем файла")
    if not isinstance(conf.get("name"), str) or not conf["name"].strip():
        raise ValueError("Укажите name")
    if not isinstance(conf.get("enabled", False), bool):
        raise ValueError("enabled должен быть true или false")
    conf.setdefault("enabled", False)
    conf.setdefault("kind", "glopro")
    if conf["kind"] not in {"glopro", "yandex", "seo"} and conf["enabled"]:
        raise ValueError("Для нового kind нужен Python-обработчик; сохраните enabled: false")
    schedule = conf.get("schedule")
    if not isinstance(schedule, dict):
        raise ValueError("Укажите schedule: days, time, timezone")
    if conf["kind"] == "seo":
        if type(schedule.get("every_days")) is not int or not 3 <= schedule["every_days"] <= 5:
            raise ValueError("SEO: schedule.every_days должен быть от 3 до 5")
        try:
            date.fromisoformat(str(schedule.get("anchor_date")))
        except ValueError:
            raise ValueError("SEO: schedule.anchor_date должен быть датой YYYY-MM-DD") from None
        for key in ("site_root", "codex_path", "node_path", "python_path"):
            if not isinstance(conf.get(key), str) or not Path(conf[key]).is_absolute():
                raise ValueError(f"SEO: укажите абсолютный путь {key}")
        if conf.get("site_url") != "https://rusplast-zavod.ru":
            raise ValueError("SEO: разрешён сайт https://rusplast-zavod.ru")
    elif not isinstance(schedule.get("days"), list) or not schedule["days"] or any(d not in DAYS for d in schedule["days"]):
        raise ValueError("schedule.days: список дней mon..sun")
    if conf["kind"] == "glopro" and any(d not in ("tue", "fri") for d in schedule["days"]):
        raise ValueError("Расчёт GloPro поддерживает плановые даты во вторник и пятницу")
    if not isinstance(schedule.get("time"), str) or not re.fullmatch(r"[0-2]\d:[0-5]\d", schedule["time"]):
        raise ValueError("schedule.time задаётся строкой '08:45'")
    time.fromisoformat(schedule["time"])
    try:
        ZoneInfo(schedule["timezone"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("Неизвестный часовой пояс")
    if "month_boundary" in schedule:
        if schedule["month_boundary"] != "close_previous_month":
            raise ValueError("schedule.month_boundary: поддерживается close_previous_month")
        if conf["kind"] != "glopro":
            raise ValueError("schedule.month_boundary применяется только к GloPro")
        if (len(schedule["days"]) != 2 or set(schedule["days"]) != {"tue", "fri"}
                or schedule["timezone"] != "Europe/Moscow"):
            raise ValueError("schedule.month_boundary: нужны вторник и пятница, Europe/Moscow")
        try:
            active_from = str(schedule["month_boundary_from"])
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", active_from):
                raise ValueError()
            date.fromisoformat(active_from)
        except (KeyError, ValueError):
            raise ValueError("schedule.month_boundary_from: укажите дату YYYY-MM-DD") from None
        schedule["month_boundary_from"] = active_from
    elif "month_boundary_from" in schedule:
        raise ValueError("schedule.month_boundary_from требует schedule.month_boundary")
    if conf["kind"] == "yandex":
        if schedule["days"] != ["tue"] or schedule["timezone"] != "Europe/Moscow":
            raise ValueError("Яндекс: только вторник, часовой пояс Europe/Moscow")
        if not isinstance(conf.get("company"), str) or not conf["company"].strip():
            raise ValueError("Укажите организацию Яндекса в company")
        employees = conf.get("employee_aliases", conf.get("employees", []))
        if not isinstance(employees, list):
            raise ValueError("employee_aliases должен быть списком подписей сотрудников")
        conf["employee_aliases"] = employees
        for entry in employees:
            if (not isinstance(entry, dict)
                or not re.fullmatch(r"[А-Яа-яЁёA-Za-z -]{1,60}", str(entry.get("name", "")))
                or not isinstance(entry.get("source_name"), str) or not entry["source_name"].strip()
                or not re.fullmatch(r"[0-9a-f]{32}", str(entry.get("user_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("phone_sha256", "")))):
                raise ValueError("Яндекс: для сотрудника нужны name, source_name, user_id и phone_sha256")
        for key in ("user_id",):
            if len({e[key] for e in employees}) != len(employees):
                raise ValueError("Сотрудники Яндекса не должны повторяться")
    clients = conf.setdefault("clients", [])
    if not isinstance(clients, list) or any(not isinstance(c, (str, dict)) for c in clients):
        raise ValueError("clients должен быть списком названий или объектов {id, name}")
    validate_excluded_clients(conf.setdefault("excluded_clients", []))
    rules = conf.setdefault("rules", {})
    if not isinstance(rules, dict):
        raise ValueError("rules должен быть объектом")
    for k, v in rules.items():
        if k.endswith("_multiplier"):
            try:
                amount = Decimal(str(v))
                if not amount.is_finite() or not Decimal("0") < amount < Decimal("10"):
                    raise ValueError()
            except Exception:
                raise ValueError(f"Некорректный коэффициент {k}")
    conf["markdown"] = markdown
    conf["description"] = parts[1].strip().split("\n")[0].lstrip("# ") if parts[1].strip() else ""
    return conf


def load_operators() -> list[dict]:
    return [parse_operator(p.read_text(encoding="utf-8"), p.stem) for p in sorted(OPERATORS.glob("*.md"))]


def get_operator(ident: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", ident):
        raise ValueError("Неверный ID")
    path = OPERATORS / f"{ident}.md"
    if not path.is_file():
        raise ValueError("Оператор не найден")
    return parse_operator(path.read_text(encoding="utf-8"), ident)


def period_for(run_date: date, conf: dict | None = None) -> tuple[date, date]:
    """Primary period; omitted config retains the original Tue/Fri contract."""
    if conf is not None:
        plan = run_plan(conf, run_date)
        return date.fromisoformat(plan["period_start"]), date.fromisoformat(plan["period_end"])
    if run_date.weekday() not in (1, 4):
        raise ValueError("Плановая дата активации должна быть вторником или пятницей")
    return run_date - timedelta(days=4 if run_date.weekday() == 1 else 3), run_date - timedelta(days=1)


def latest_run_date(now: datetime | None = None, conf: dict | None = None) -> date:
    """Latest calendar date, including today before its configured start time."""
    zone = ZoneInfo(conf["schedule"]["timezone"] if conf else "Europe/Moscow")
    today = (now or datetime.now(zone)).astimezone(zone).date()
    if conf and conf["kind"] == "seo":
        return seo_slot(conf, today)
    for offset in range(8):
        candidate = today - timedelta(days=offset)
        if schedule_matches(conf, candidate) if conf else candidate.weekday() in (1, 4):
            return candidate
    raise ValueError("В расписании нет доступной даты запуска для выбранных фирм")


def client_period_for(run_date: date, client: str, client_id: str | None = None,
                      conf: dict | None = None) -> tuple[date, date] | None:
    if conf is not None:
        plan = run_plan(conf, run_date)
        key = "weekly_period" if weekly_kind(client, client_id) else "ordinary_period"
        period = plan[key]
        return tuple(date.fromisoformat(day) for day in period) if period else None
    standard = period_for(run_date)
    if weekly_kind(client, client_id):
        if run_date.weekday() != 1:
            return None
        return run_date - timedelta(days=7), run_date - timedelta(days=1)
    return standard


def _month_policy(conf: dict) -> bool:
    return (conf.get("kind") == "glopro"
            and conf["schedule"].get("month_boundary") == "close_previous_month")


def _ordinary_run_date(conf: dict, slot: date) -> date:
    """Map an original Tue/Fri slot to its ordinary reporting execution date.

    This mapping deliberately ignores weekly-only executions: they do not
    consume any of the ordinary clients' operation dates.
    """
    start, end = period_for(slot)
    if _month_policy(conf) and (start.year, start.month) != (end.year, end.month):
        boundary = end.replace(day=1)
        if boundary >= date.fromisoformat(conf["schedule"]["month_boundary_from"]):
            return boundary
    return slot


def _selected_clients(conf: dict) -> list[dict]:
    requested = conf.get("clients", [])
    return [({"name": entry} if isinstance(entry, str) else entry) for entry in requested]


def _has_weekly_clients(conf: dict) -> bool:
    candidates = _selected_clients(conf) or [
        {"id": ident, "name": item["name"]} for ident, item in WEEKLY_CLIENTS.items()
    ]
    return any(
        weekly_kind(item.get("name") or item.get("client") or "", item.get("id", item.get("client_id")))
        and not client_is_excluded(item, conf.get("excluded_clients", []))
        for item in candidates
    )


def _has_ordinary_clients(conf: dict) -> bool:
    candidates = _selected_clients(conf)
    # With unrestricted selection the actual ordinary firms are discovered in
    # the portal; a calendar must not guess they are absent from a static list.
    if not candidates:
        return True
    return any(
        not weekly_kind(item.get("name") or item.get("client") or "", item.get("id", item.get("client_id")))
        and not client_is_excluded(item, conf.get("excluded_clients", []))
        for item in candidates
    )


def _raw_schedule_matches(conf: dict, day: date) -> bool:
    if conf["kind"] == "seo":
        anchor = date.fromisoformat(str(conf["schedule"]["anchor_date"]))
        elapsed = (day - anchor).days
        return elapsed >= 0 and elapsed % conf["schedule"]["every_days"] == 0
    return day.weekday() in [DAYS[d] for d in conf["schedule"]["days"]]


def _plan_for(conf: dict, day: date) -> dict | None:
    ordinary = weekly = None
    moved_from = None
    kind = "regular"
    if conf["kind"] == "glopro":
        if _month_policy(conf):
            # A moved run can precede its original slot by at most three days.
            slot = day
            while slot.weekday() not in (1, 4):
                slot += timedelta(days=1)
            if _ordinary_run_date(conf, slot) == day:
                previous_slot, _ = period_for(slot)
                ordinary = (_ordinary_run_date(conf, previous_slot), day - timedelta(days=1))
                if day != slot:
                    moved_from = slot.isoformat()
                if (day.day == 1
                        and day >= date.fromisoformat(conf["schedule"]["month_boundary_from"])):
                    kind = "month_close"
        elif _raw_schedule_matches(conf, day):
            ordinary = period_for(day)
        if day.weekday() == 1 and _raw_schedule_matches(conf, day):
            weekly = (day - timedelta(days=7), day - timedelta(days=1))
        if ordinary is None and weekly is not None:
            kind = "weekly_only"
        primary = ordinary or weekly
    elif conf["kind"] == "yandex":
        if not _raw_schedule_matches(conf, day):
            return None
        weekly = primary = (day - timedelta(days=7), day - timedelta(days=1))
    elif conf["kind"] == "seo":
        if not _raw_schedule_matches(conf, day):
            return None
        primary = (day, day)
    else:
        return None
    if primary is None:
        return None
    result = {
        "run_date": day.isoformat(),
        "period_start": primary[0].isoformat(), "period_end": primary[1].isoformat(),
        "ordinary_period": [value.isoformat() for value in ordinary] if ordinary else None,
        "weekly_period": [value.isoformat() for value in weekly] if weekly else None,
        "kind": kind, "moved_from": moved_from,
    }
    if conf["kind"] == "glopro":
        # Save calendar eligibility before applying client selection. A later
        # retry can add a client without recomputing the calendar under a new
        # policy or inventing an ordinary interval on a moved Tuesday.
        result["calendar_periods"] = {
            "ordinary": result["ordinary_period"], "weekly": result["weekly_period"],
            "kind": kind, "moved_from": moved_from,
            "ordinary_moved_to": (_ordinary_run_date(conf, day).isoformat()
                                  if kind == "weekly_only" else None),
            "filter_ordinary": (_month_policy(conf)
                                and day >= date.fromisoformat(conf["schedule"]["month_boundary_from"])),
        }
        try:
            return plan_for_clients(result, conf)
        except ValueError:
            return None
    return result


def plan_for_clients(plan: dict, conf: dict) -> dict:
    """Project a saved calendar onto current clients, never recalculating dates.

    Legacy snapshots without latent calendar fields can retain only periods
    they actually saved. Do not reconstruct missing intervals from a possibly
    changed current policy.
    """
    result = deepcopy(plan)
    if conf["kind"] != "glopro":
        return result
    calendar = result.get("calendar_periods") or {
        "ordinary": result.get("ordinary_period"), "weekly": result.get("weekly_period"),
        "kind": result.get("kind", "regular"), "moved_from": result.get("moved_from"),
        "ordinary_moved_to": result.get("ordinary_moved_to"), "filter_ordinary": True,
    }
    ordinary = calendar.get("ordinary")
    weekly = calendar.get("weekly")
    if calendar.get("filter_ordinary") and not _has_ordinary_clients(conf):
        ordinary = None
    if not _has_weekly_clients(conf):
        weekly = None
    primary = ordinary or weekly
    if not primary:
        raise ValueError(f"На {result['run_date']} нет доступного периода для выбранных фирм.")
    result["ordinary_period"] = list(ordinary) if ordinary else None
    result["weekly_period"] = list(weekly) if weekly else None
    result["period_start"], result["period_end"] = primary
    result["kind"] = calendar.get("kind", "regular") if ordinary else "weekly_only"
    result["moved_from"] = calendar.get("moved_from") if ordinary else None
    result.pop("ordinary_moved_to", None)
    result.pop("explanation", None)
    if result["kind"] == "month_close":
        result["explanation"] = "Закрытие предыдущего месяца; операции с 1-го числа войдут в следующий обычный отчёт."
    elif result["kind"] == "weekly_only":
        result["explanation"] = "Только Китай и НК АРТЭЛЬ за предыдущие вторник–понедельник."
        if _has_ordinary_clients(conf) and calendar.get("ordinary_moved_to"):
            result["ordinary_moved_to"] = calendar["ordinary_moved_to"]
            result["explanation"] += " Обычное актирование перенесено на 1-е число."
    return result


def run_plan(conf: dict, day: date) -> dict:
    """Return one JSON-serializable plan shared by execution and all previews."""
    result = _plan_for(conf, day)
    if result is not None:
        return result
    if (_month_policy(conf)
            and day >= date.fromisoformat(conf["schedule"]["month_boundary_from"])
            and not _has_ordinary_clients(conf) and not _has_weekly_clients(conf)):
        raise ValueError(f"На {day.isoformat()} нет доступных фирм для актирования: выбранные фирмы исключены.")
    following = next((day + timedelta(days=offset) for offset in range(1, 9)
                      if _plan_for(conf, day + timedelta(days=offset)) is not None), None)
    suggestion = f" Следующая дата запуска: {following.isoformat()}." if following else ""
    if _month_policy(conf) and _has_ordinary_clients(conf) and day.weekday() in (1, 4):
        moved_to = _ordinary_run_date(conf, day)
        if moved_to != day:
            raise ValueError(f"Актирование {day.isoformat()} перенесено на {moved_to.isoformat()} для закрытия месяца.{suggestion}")
    raise ValueError(f"На {day.isoformat()} актирование не запланировано.{suggestion}")


def next_run(conf: dict, now: datetime | None = None) -> str | None:
    if not conf["enabled"]:
        return None
    zone = ZoneInfo(conf["schedule"]["timezone"])
    current = (now or datetime.now(zone)).astimezone(zone)
    for delta in range(8):
        day = current.date() + timedelta(days=delta)
        if schedule_matches(conf, day):
            when = datetime.combine(day, time.fromisoformat(conf["schedule"]["time"]), zone)
            if when > current:
                return when.isoformat()
    return None


def schedule_matches(conf: dict, day: date) -> bool:
    if _month_policy(conf):
        return _plan_for(conf, day) is not None
    return _raw_schedule_matches(conf, day)


def seo_slot(conf: dict, day: date) -> date:
    anchor = date.fromisoformat(str(conf["schedule"]["anchor_date"]))
    if day < anchor:
        raise ValueError("SEO: дата раньше начала расписания")
    return day - timedelta(days=(day - anchor).days % conf["schedule"]["every_days"])
