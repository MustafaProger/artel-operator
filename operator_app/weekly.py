"""Tuesday-only GloPro exceptions, reconciled against the downloaded workbook."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from .calculator import (
    CalculationError, _columns, _date, _decimal, _fuel, _litres, _money,
    _norm, _ru, company_identity,
)

AMOUNTS = ("litres", "supplier_basis", "customer_total")
MOSCOW = ZoneInfo("Europe/Moscow")


def _zero():
    return {key: Decimal(0) for key in AMOUNTS}


def _raw(amounts):
    return {key: str(amounts[key]) for key in AMOUNTS}


def _add(target, amounts):
    for key in AMOUNTS:
        target[key] += amounts[key]


def _period(rows, settings):
    first, last = settings.get("date_from"), settings.get("date_to")
    if settings.get("run_date"):
        run_date = _date(settings["run_date"], "плановая дата")
        if run_date.weekday() != 1:
            raise CalculationError("Эта фирма обрабатывается только во вторничном запуске")
        expected = (run_date - timedelta(days=7), run_date - timedelta(days=1))
        if (first and first != expected[0]) or (last and last != expected[1]):
            raise CalculationError("Запрошенный период не соответствует плановому вторнику")
        first, last = expected
    if not first or not last or first.weekday() != 1 or last - first != timedelta(days=6):
        raise CalculationError("Нужен недельный период со вторника по понедельник")
    expected = (datetime.combine(first, time.min, MOSCOW), datetime.combine(last, time(23, 59, 59), MOSCOW))
    periods = []
    stamp = r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?"
    for row in rows:
        values = [str(v).strip() for v in row if v is not None]
        if not any(re.match(r"^период\s*:", v, re.I) for v in values):
            continue
        match = re.search(r"\bс\s+" + stamp + r"\s+по\s+" + stamp + r"(?:\s|$)", " ".join(values), re.I)
        if not match:
            raise CalculationError("Не удалось прочитать период внутри Excel")
        try:
            start = datetime.strptime(match[1] + " " + (match[2] or "00:00:00"), "%d.%m.%Y %H:%M:%S").replace(tzinfo=MOSCOW)
            end = datetime.strptime(match[3] + " " + (match[4] or "23:59:59"), "%d.%m.%Y %H:%M:%S").replace(tzinfo=MOSCOW)
        except ValueError:
            raise CalculationError("Некорректный период внутри Excel") from None
        periods.append((start, end))
    if not periods:
        raise CalculationError("В Excel отсутствует период отчёта; даты операций не подтверждают полноту недели")
    if any(period != expected for period in periods):
        raise CalculationError(f"Период внутри Excel не совпадает с {first:%d.%m.%Y}–{last:%d.%m.%Y} (Europe/Moscow)")
    return expected


def _transaction_time(value, row):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
                try:
                    parsed = datetime.strptime(text, pattern)
                    break
                except ValueError:
                    pass
            if parsed is None:
                parsed = datetime.combine(_date(value, f"строка {row}"), time.min)
    return parsed.replace(tzinfo=MOSCOW) if parsed.tzinfo is None else parsed.astimezone(MOSCOW)


def calculate_weekly(rows, header_index, columns, client, kind, settings):
    first, last = _period(rows, settings)
    totals, cards, groups = _zero(), {}, {}
    checks, transaction_rows, summary_rows, dates = [], [], [], []
    current_card, current_holder = None, None
    service_totals = _zero()
    service_name = None
    file_checked = False

    def check(row, index, actual, scope):
        expected = {key: _decimal(row[columns[key]], f"итог {key}, строка {index}") for key in AMOUNTS}
        for key in AMOUNTS:
            # Allow only serialization noise, never hide a mismatch by rounding
            # the two sides to display precision before comparing them.
            if abs(actual[key] - expected[key]) > Decimal("0.0000001"):
                raise CalculationError(f"Строка {index}: не совпадает итог {scope}, {key}: операции {actual[key]}, Excel {expected[key]}")
        checks.append({"row": index, "scope": scope, "operations": _raw(actual), "source": _raw(expected)})

    for index, row in enumerate(rows[header_index + 1:], header_index + 2):
        if not any(v is not None and str(v).strip() for v in row):
            continue

        def cell(field):
            return row[columns[field]] if field in columns else None

        try:
            tx_time = _transaction_time(cell("date"), index)
        except CalculationError:
            tx_time = None
        if file_checked:
            if tx_time is not None:
                raise CalculationError(f"Строка {index}: операция после итога файла")
            continue  # Repeated totals by service and the footer are not operations.
        if tx_time is None:
            labels = [str(v).strip() for v in row if isinstance(v, str)]
            summary = next((_norm(v) for v in labels if re.match(r"^(итого|всего|общий итог)(?:\s|:|$)", _norm(v))), None)
            if summary:
                summary_rows.append(index)
                if re.match(r"^(итого по отчету|всего по отчету|общий итог по отчету)(?:\s|:|$)", summary):
                    check(row, index, totals, "файла")
                    file_checked = True
                elif summary.startswith("итого по карте"):
                    if not current_card or cards[current_card]["checked"]:
                        raise CalculationError(f"Строка {index}: итог карты без однозначной карты")
                    check(row, index, cards[current_card]["totals"], "карты " + current_card)
                    cards[current_card]["checked"] = True
                elif summary.startswith("итого по ") and service_name:
                    if _norm(summary.removeprefix("итого по ").rstrip(":")) != _norm(service_name):
                        raise CalculationError(f"Строка {index}: неясный итог услуги")
                    check(row, index, service_totals, "услуги " + service_name)
                    service_name, service_totals = None, _zero()
                else:
                    raise CalculationError(f"Строка {index}: неизвестный промежуточный итог")
                continue
            if {"date", "service", *AMOUNTS} <= _columns(row).keys():
                continue
            metadata = {}
            for label in labels:
                match = re.fullmatch(r"(Карта|Держатель|Услуга)\s*:\s*(.*)", label, re.I)
                if match:
                    metadata[_norm(match[1])] = match[2].strip()
            if metadata:
                if any(cell(key) not in (None, "") for key in AMOUNTS):
                    raise CalculationError(f"Строка {index}: метаданные содержат суммы")
                if "карта" in metadata:
                    if current_card and not cards[current_card]["checked"]:
                        raise CalculationError("Отсутствует итог предыдущей карты")
                    current_card = metadata["карта"]
                    current_holder = metadata.get("держатель")
                    if not current_card or current_card in cards:
                        raise CalculationError("Пустая или повторная карта в Excel")
                    cards[current_card] = {"totals": _zero(), "checked": False}
                    service_name, service_totals = None, _zero()
                elif "держатель" in metadata:
                    current_holder = metadata["держатель"]
                if "услуга" in metadata:
                    service_name, service_totals = metadata["услуга"], _zero()
                continue
            if any(_norm(v) in {"нет данных", "нет операций", "операции отсутствуют", "данные отсутствуют"} for v in labels):
                if any(cell(key) not in (None, "") for key in AMOUNTS):
                    raise CalculationError("Сообщение об отсутствии операций содержит суммы")
                continue
            # Footers without a file total must not allow a false reconciliation.
            if any(re.match(r"^(период|отчет|параметры|сформирован)\s*:", _norm(v)) for v in labels):
                continue
            raise CalculationError(f"Строка {index}: не распознана операция или итог")

        if not first <= tx_time <= last:
            raise CalculationError(f"Строка {index}: операция вне запрошенного периода Europe/Moscow")
        if cell("operation") and _norm(cell("operation")) not in {"дебет", "возврат"}:
            raise CalculationError(f"Строка {index}: неизвестный тип операции")
        fuel = _fuel(cell("service"), index)
        amounts = {key: _decimal(cell(key), f"{key}, строка {index}") for key in AMOUNTS}
        if any(amounts["litres"] * amounts[key] < 0 for key in AMOUNTS[1:]):
            raise CalculationError(f"Строка {index}: знаки литража и сумм не совпадают")
        if amounts["litres"] == 0 and any(amounts[key] != 0 for key in AMOUNTS[1:]):
            raise CalculationError(f"Строка {index}: нулевой литраж при ненулевой сумме")
        if _norm(cell("operation")) == "возврат" and any(value > 0 for value in amounts.values()):
            raise CalculationError(f"Строка {index}: возврат содержит положительные суммы")
        card = str(cell("card") or current_card or "").strip()
        holder = re.sub(r"\s+", " ", str(cell("holder") or current_holder or "")).strip()
        if not card:
            raise CalculationError(f"Строка {index}: не указана карта для сверки итогов")
        if kind == "nk_artel" and not holder:
            raise CalculationError(f"Строка {index}: отсутствует держатель карты")
        current_card = card
        card_group = cards.setdefault(card, {"totals": _zero(), "checked": False})
        if card_group["checked"]:
            raise CalculationError(f"Строка {index}: операция после итога карты")
        _add(card_group["totals"], amounts)
        _add(totals, amounts)
        _add(service_totals, amounts)
        key = (_norm(holder) if kind == "nk_artel" else "", fuel)
        group = groups.setdefault(key, {"holder": holder if kind == "nk_artel" else "", "fuel": fuel, **_zero()})
        _add(group, amounts)
        transaction_rows.append(index)
        dates.append(tx_time.date().isoformat())
    if not file_checked:
        raise CalculationError("Отсутствует итог файла для сверки")
    if any(not card["checked"] for card in cards.values()):
        raise CalculationError("Отсутствует итог карты для сверки")
    return _report(client, kind, settings["supplier"], groups.values(), {
        "sheet": "transactions", "header_row": header_index + 1,
        "columns": {key: index + 1 for key, index in columns.items()},
        "transaction_rows": transaction_rows, "skipped_summary_rows": summary_rows,
        "date_from": min(dates) if dates else None, "date_to": max(dates) if dates else None,
        "verified_period": [first.isoformat(), last.isoformat()], "reconciliation": checks,
        "card_count": len(cards), "operation_count": len(transaction_rows),
    })


def _report(client, kind, supplier, groups, audit):
    raw_groups = [{"holder": g["holder"], "fuel": g["fuel"], **_raw(g)} for g in groups]
    totals, fuels = _zero(), {}
    holders = []
    for group in raw_groups:
        amounts = {key: Decimal(group[key]) for key in AMOUNTS}
        if amounts["litres"] == 0 and any(amounts[key] for key in AMOUNTS[1:]):
            raise CalculationError("Итоговый литраж равен нулю при ненулевой сумме")
        _add(totals, amounts)
        _add(fuels.setdefault(group["fuel"], _zero()), amounts)
        if any(amounts.values()):
            holders.append({"holder": group["holder"], "fuel": group["fuel"],
                            "litres": _money(amounts["litres"]), "customer_total": _money(amounts["customer_total"])})
    fuel_rows = [{"fuel": fuel, **{key: (_litres(value) if key == "litres" else _money(value)) for key, value in amounts.items()}}
                 for fuel, amounts in sorted(fuels.items()) if any(amounts.values())]
    return {
        "client": client, "supplier": supplier, "weekly_kind": kind, "card_type": None,
        "status": "ready" if holders else "fully_reversed" if audit["operation_count"] else "no_data",
        "fuels": fuel_rows, "holders": sorted(holders, key=lambda g: (g["holder"].casefold(), g["fuel"] != "ДТ ЭКТО", g["fuel"])),
        "totals": {key: _litres(value) if key == "litres" else _money(value) for key, value in totals.items()},
        "supplier_total": _litres(totals["supplier_basis"] * Decimal("0.99")),
        "audit": {**audit, "raw_groups": raw_groups, "raw_totals": _raw(totals)},
    }


def merge_weekly_reports(reports):
    first = reports[0]
    if len({(company_identity(r["client"]), r["supplier"], r.get("weekly_kind")) for r in reports}) != 1:
        raise CalculationError("Нельзя объединять разные фирмы или правила недельного расчёта")
    periods = {tuple(r["audit"]["verified_period"]) for r in reports}
    if len(periods) != 1:
        raise CalculationError("Периоды договоров не совпадают")
    groups = {}
    for report in reports:
        if report["status"] not in {"ready", "no_data", "fully_reversed"}:
            raise CalculationError("Объединять можно только проверенные отчёты")
        for item in report["audit"]["raw_groups"]:
            key = (_norm(item["holder"]), item["fuel"])
            group = groups.setdefault(key, {"holder": item["holder"], "fuel": key[1], **_zero()})
            _add(group, {key: Decimal(item[key]) for key in AMOUNTS})
    return _report(first["client"], first["weekly_kind"], first["supplier"], groups.values(), {
        "merged": True, "source_reports": reports, "contract_ids": [r.get("contract_id") for r in reports],
        "verified_period": list(next(iter(periods))), "operation_count": sum(r["audit"]["operation_count"] for r in reports),
    })


def render_weekly(report):
    supplier = str(report["supplier"]).replace("\n", " ").replace("\r", " ")
    if report["weekly_kind"] == "china":
        totals = report["totals"]
        return (f'### Китай\nПоставщик: "{supplier}"\n'
                f'на склад (Китай) {_ru(totals["litres"], 3)} л на сумму\n\n'
                f'- для поставщика {_ru(report["supplier_total"], 3)} рублей\n'
                f'- клиент нам {_ru(totals["customer_total"], 2)} рублей')
    lines = ['### ООО "НК АРТЭЛЬ"', f'Поставщик: "{supplier}"', 'на склад (Эльдар)']
    lines.extend(f'• {g["holder"]} - {g["fuel"]} {_ru(g["litres"], 2)} л | {_ru(g["customer_total"], 2)} руб'
                 for g in report["holders"])
    if not report["holders"]:
        lines.append("Операции полностью возвращены." if report["status"] == "fully_reversed" else "Нет операций за период.")
    return "\n".join(lines)
