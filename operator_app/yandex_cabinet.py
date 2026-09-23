"""Validated cabinet requests, bound to the user's existing browser session.

Endpoints and cursor semantics were observed in the cabinet on 2026-09-22.
Authentication headers are kept in memory only; never included in the audit.
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo
import json
import re
import uuid

from .yandex_reports import number, phone_hash, text, display_name

API = "https://b2b-api-lk.go.yandex.ru"
ORDERS = "/corp-cabinet/2.0/orders/tanker/list"
REPORTS = "/corp-cabinet/2.0/reports/"
ID = re.compile(r"[0-9a-f]{32}")
MOSCOW = ZoneInfo("Europe/Moscow")


class CabinetError(ValueError):
    pass


class Cabinet:
    def __init__(self, context, response, company):
        if response.status != 200:
            raise CabinetError("Не удалось подтвердить организацию Яндекса")
        body = response.json()
        if body.get("name") != company or not ID.fullmatch(str(body.get("id", ""))):
            raise CabinetError("В кабинете Яндекса другая организация")
        self.company_id = body["id"]
        self.context = context
        headers = response.request.all_headers()
        self.headers = {k: v for k, v in headers.items() if k in {
            "x-csrf-token", "x-yataxi-selected-corp-client-id", "x-application-version",
            "origin", "referer", "x-requested-uri", "user-agent"}}
        self.headers["content-type"] = "application/json"
        if not self.headers.get("x-csrf-token"):
            raise CabinetError("Не удалось подтвердить авторизованный запрос кабинета")
        selected = self.headers.get("x-yataxi-selected-corp-client-id")
        if selected and selected != self.company_id:
            raise CabinetError("Организация запроса не совпала с кабинетом")

    def request(self, path, payload=None, params=None):
        url = API + path + ("?" + urlencode(params) if params else "")
        headers = dict(self.headers)
        if path == REPORTS + "generate":
            headers["x-idempotency-token"] = str(uuid.uuid4())
        response = self.context.request.fetch(url, method="GET" if payload is None else "POST",
            headers=headers, data=json.dumps(payload) if payload is not None else None,
            timeout=60000, max_redirects=0)
        if response.status != 200:
            raise CabinetError("Ошибка загрузки данных Яндекса; отсутствие операций не подтверждено")
        try:
            result = response.json()
        except Exception:
            raise CabinetError("Яндекс вернул некорректные данные; требуется повторная проверка") from None
        if not isinstance(result, dict) or "error" in result:
            raise CabinetError("Ответ кабинета Яндекса не подтверждён")
        return result

    def orders_page(self, start, end, cursor, limit):
        # Include fractional seconds of the final day, which the UI's 23:59:59 omits.
        return self.request(ORDERS, {}, {
            "since_datetime": datetime.combine(start, time.min, MOSCOW).isoformat(),
            "till_datetime": datetime.combine(end, time.max, MOSCOW).isoformat(),
            "limit": limit, **({"cursor": cursor} if cursor else {})})


def normalize_order(order, company_id, start, end):
    try:
        if (not ID.fullmatch(order["id"]) or not ID.fullmatch(order["user_id"])
                or order["client_id"] != company_id):
            raise ValueError()
        timestamp = datetime.fromisoformat(order["created_at"])
        if timestamp.utcoffset() != timedelta(hours=3):
            raise ValueError()
        if not start <= timestamp.astimezone(MOSCOW).date() <= end:
            raise ValueError()
        name = text(order["user_info"]["fullname"])
        phone = re.sub(r"\D", "", order["user_info"]["phone"])
        if not name or not re.fullmatch(r"\d{10,15}", phone):
            raise ValueError()
        litres, amount = number(order["liters_filled"]), number(order["final_price"])
        if order.get("currency") != "RUB":
            raise ValueError()
        if (order["status"] not in {"Completed", "Cancelled"}
                or order["status"] == "Cancelled" and (litres != 0 or amount != 0)
                or litres < 0 or amount < 0 or litres == 0 and amount != 0):
            raise CabinetError("Неизвестный статус, возврат или несогласованные суммы Яндекса: нужна проверка")
        return {"id": order["id"], "user_id": order["user_id"], "source_name": name,
                "phone_sha256": phone_hash(phone), "date": timestamp.date().isoformat(),
                "created_at": timestamp.isoformat(), "status": order["status"],
                "litres": str(litres), "amount": str(amount),
                "active": order["status"] == "Completed" and litres > 0}
    except CabinetError:
        raise
    except (KeyError, TypeError, ValueError):
        raise CabinetError("Не удалось проверить ID, организацию, дату, UTC+3 или сотрудника заказа Яндекса") from None


def collect_orders(cabinet, start, end, *, limit=30):
    orders, ids, cursors, pages = [], set(), set(), []
    cursor = ""
    previous_time = None
    declared_total = None
    for _ in range(10000):
        body = cabinet.orders_page(start, end, cursor, limit)
        rows, next_cursor = body.get("orders"), body.get("cursor")
        if (not isinstance(rows, list) or not isinstance(next_cursor, str)
                or type(body.get("limit")) is not int or body["limit"] != limit
                or len(rows) > limit or body.get("sorting_order") != "desc"):
            raise CabinetError("Неполная выдача заказов Яндекса: схема или пагинация не подтверждены")
        if "total" in body:
            total = body["total"]
            if type(total) is not int or total < 0 or declared_total is not None and total != declared_total:
                raise CabinetError("Количество заказов изменилось при пагинации Яндекса")
            declared_total = total
        if next_cursor and (next_cursor in cursors or not rows):
            raise CabinetError("Пагинация Яндекса зациклилась или вернула пустую промежуточную страницу")
        if len(rows) == limit and not next_cursor:
            raise CabinetError("Полная страница Яндекса без курсора: окончание выдачи не подтверждено")
        for raw in rows:
            order = normalize_order(raw, cabinet.company_id, start, end)
            timestamp = datetime.fromisoformat(order["created_at"])
            if order["id"] in ids or previous_time is not None and timestamp > previous_time:
                raise CabinetError("Повтор ID или нарушенный порядок страниц Яндекса")
            previous_time = timestamp
            ids.add(order["id"])
            orders.append(order)
        pages.append({"number": len(pages) + 1, "count": len(rows), "has_next": bool(next_cursor)})
        if not next_cursor:
            if declared_total is not None and declared_total != len(orders):
                raise CabinetError("Число заказов не совпало с полной выдачей Яндекса")
            return {"company_id": cabinet.company_id, "period": [str(start), str(end)],
                    "timezone": "UTC+3", "complete": True, "order_count": len(orders),
                    "pages": pages, "orders": orders}
        cursors.add(next_cursor)
        cursor = next_cursor
    raise CabinetError("Превышен предел страниц Яндекса; итог не опубликован")


def discover_employees(manifest, aliases=()):
    employees = {}
    for order in manifest["orders"]:
        uid = order["user_id"]
        if uid not in employees:
            # Descending order: display the most recent source name for this week.
            employees[uid] = {"user_id": uid, "employee_id": uid,
                "source_name": order["source_name"], "phone_sha256": order["phone_sha256"],
                "name": display_name(order["source_name"], uid, order["phone_sha256"], aliases),
                "orders": [], "active": False}
        employees[uid]["orders"].append(order)
        employees[uid]["active"] |= order["active"]
    from .yandex_reports import label_reports
    return label_reports(list(employees.values()))
