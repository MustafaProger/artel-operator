from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import re
import yaml

from .clients import validate_excluded_clients
from .calculator import weekly_kind

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


def period_for(run_date: date) -> tuple[date, date]:
    if run_date.weekday() not in (1, 4):
        raise ValueError("Плановая дата активации должна быть вторником или пятницей")
    return run_date - timedelta(days=4 if run_date.weekday() == 1 else 3), run_date - timedelta(days=1)


def latest_run_date(now: datetime | None = None) -> date:
    zone = ZoneInfo("Europe/Moscow")
    today = (now or datetime.now(zone)).astimezone(zone).date()
    while today.weekday() not in (1, 4):
        today -= timedelta(days=1)
    return today


def client_period_for(run_date: date, client: str, client_id: str | None = None) -> tuple[date, date] | None:
    standard = period_for(run_date)
    if weekly_kind(client, client_id):
        if run_date.weekday() != 1:
            return None
        return run_date - timedelta(days=7), run_date - timedelta(days=1)
    return standard


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
    if conf["kind"] == "seo":
        anchor = date.fromisoformat(str(conf["schedule"]["anchor_date"]))
        elapsed = (day - anchor).days
        return elapsed >= 0 and elapsed % conf["schedule"]["every_days"] == 0
    return day.weekday() in [DAYS[d] for d in conf["schedule"]["days"]]


def seo_slot(conf: dict, day: date) -> date:
    anchor = date.fromisoformat(str(conf["schedule"]["anchor_date"]))
    if day < anchor:
        raise ValueError("SEO: дата раньше начала расписания")
    return day - timedelta(days=(day - anchor).days % conf["schedule"]["every_days"])
