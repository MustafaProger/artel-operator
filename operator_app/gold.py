"""Dated gold-station rules and card evidence from verified source workbooks."""
from functools import lru_cache
from datetime import date
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import re

from openpyxl import load_workbook

START = date(2026, 10, 27)
MULTIPLIERS = {"gold_virtual": Decimal("0.925"), "gold_plastic": Decimal("0.93")}


def card_rows(rows, columns, start):
    card = None
    for number, row in enumerate(rows[start:], start + 1):
        for value in row:
            if isinstance(value, str):
                match = re.fullmatch(r"\s*Карта:\s*(\d+)\s*", value)
                if match:
                    card = match[1]
        if "card" in columns and columns["card"] < len(row):
            value = row[columns["card"]]
            if value is not None:
                card = str(value).strip()
        yield number, row, card


def evidence_from_workbook(workbook, source):
    from .calculator import _source_table, _decimal, _date, _fuel, CalculationError
    sheet, rows, header, columns = _source_table(workbook)
    result = {}
    if "discount" not in columns:
        return result
    for number, row, card in card_rows(rows, columns, header + 1):
        if not card:
            continue
        try:
            _date(row[columns["date"]], "дата")
            if _fuel(row[columns["service"]], number) != "ДТ ЭКТО":
                continue
            if not _decimal(row[columns["litres"]], "литры"):
                continue
            value = row[columns["discount"]]
            discount = _decimal(value, "скидка")
            if "%" in workbook[sheet].cell(number, columns["discount"] + 1).number_format and not isinstance(value, str):
                discount *= 100
            kind = "virtual" if abs(discount + 1) <= Decimal("0.20") else "plastic" if abs(discount + 2) <= Decimal("0.20") else None
            if kind:
                result.setdefault(card, {}).setdefault(kind, {"source": source, "row": number, "discount": str(discount)})
        except (CalculationError, IndexError):
            continue
    return result


def load_card_evidence(data):
    """Manifest pins historical inputs by hash; missing/changed evidence fails closed."""
    from .calculator import company_identity, workbook_client, CalculationError
    manifest = Path(data) / "glopro-card-evidence.json"
    result = {}
    if not manifest.exists():
        return result
    for item in json.loads(manifest.read_text()):
        path = Path(data) / item["path"]
        if not path.is_relative_to(Path(data)) or ".." in path.parts:
            raise CalculationError("Некорректный путь подтверждения типов карт")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise CalculationError("Изменился исходник подтверждения типов карт")
        identity = company_identity(workbook_client(path))
        book = load_workbook(path, data_only=True)
        try:
            for card, kinds in evidence_from_workbook(book, item).items():
                result.setdefault(identity, {}).setdefault(card, {}).update(kinds)
        finally:
            book.close()
    return result


@lru_cache(maxsize=1)
def _stations():
    from .calculator import _norm
    stations = json.loads(Path(__file__).with_name("gold_stations.json").read_text())
    return frozenset((_norm(s[0]), _norm(s[1])) for s in stations)


def station_matches(name, address):
    from .calculator import _norm
    address = re.sub(r"(?<!\w)хут\.(?= )", "х.", _norm(address))
    return (_norm(name), address) in _stations()
