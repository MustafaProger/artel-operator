"""GloPro browser adapter. Credentials exist only in an ephemeral browser context.

The adapter uses the observed reports UI. It never changes card state or limits.
An incomplete client list or an unverified download is a failed run, not a report.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import hashlib
import html
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable
from urllib.parse import parse_qs, urljoin, urlparse
import zipfile

from .clients import client_is_excluded, validate_excluded_clients
from .calculator import weekly_kind
from .ordering import alphabet_key

ORIGIN = "https://lk.glopro.ru"
FORM = "#reports2"
MAX_CLIENT_PAGES = 250
Progress = Callable[[dict], None]


class GloProError(ValueError):
    """Actionable failure that never includes credentials or browser call logs."""


def activity_from_recent(payload, first, last):
    """The account endpoint exposes the latest ten rows, not full history.

    Empty is proven only when this window reaches before the requested period
    (or the portal explicitly reports no history). Never interpret more=False
    as complete historical coverage.
    """
    if payload == {"success": False, "data": False, "messages": []}:
        return {"has_operations": False, "evidence": "no_contract_transactions"}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise GloProError("GloPro не подтвердил наличие операций. Пустой отчёт не формируется.")
    rows = data["items"]
    if not rows:
        raise GloProError("Неожиданный пустой ответ транзакций; нужна проверка GloPro.")
    try:
        timestamps = [datetime.strptime(row["DATETIME_TRN"], "%Y-%m-%d %H:%M:%S") for row in rows]
    except (ValueError, KeyError, TypeError):
        raise GloProError("GloPro изменил формат дат транзакций") from None
    if timestamps != sorted(timestamps, reverse=True):
        raise GloProError("GloPro не подтвердил порядок последних транзакций")
    evidence = {"evidence": "latest_transaction_window", "newest": timestamps[0].isoformat(), "oldest": timestamps[-1].isoformat(), "checked_rows": len(rows)}
    if any(first <= stamp.date() <= last for stamp in timestamps):
        return {**evidence, "has_operations": True}
    if timestamps[-1].date() < first:
        return {**evidence, "has_operations": False}
    raise GloProError("Последние транзакции не покрывают выбранный период. Автоматическая выгрузка остановлена: нельзя подтвердить, есть ли заправки. Используйте импорт отчёта за этот период.")


def _browser_channel() -> str | None:
    """Use installed Chrome on macOS without accessing its existing profile."""
    override = os.environ.get("GLOPRO_BROWSER_CHANNEL", "").strip().lower()
    if override:
        if override not in {"chrome", "chromium"}:
            raise GloProError("GLOPRO_BROWSER_CHANNEL должен быть chrome или chromium.")
        return "chrome" if override == "chrome" else None
    if sys.platform == "darwin" and Path("/Applications/Google Chrome.app").is_dir():
        return "chrome"
    return None


def _transport_mode() -> str:
    mode = os.environ.get("GLOPRO_TRANSPORT", "api").strip().lower()
    if mode not in {"api", "direct"}:
        raise GloProError("GLOPRO_TRANSPORT должен быть api или direct.")
    return mode


def _route_request(route, transport: str) -> None:
    """Bridge only GloPro through Playwright's verified HTTPS API transport.

    Keep original requests intact, including POST bodies, without logging them.
    Redirects remain browser-managed and no network retry can duplicate a POST.
    """
    response = None
    try:
        parsed = urlparse(route.request.url)
        if route.request.resource_type in {"image", "font", "media"} or parsed.hostname in {
            "mc.yandex.ru", "mc.yandex.com", "api-maps.yandex.ru"
        }:
            route.abort()
        elif transport == "api" and parsed.scheme == "https" and parsed.netloc == "lk.glopro.ru":
            response = route.fetch(max_redirects=0, max_retries=0, timeout=20_000)
            route.fulfill(response=response)
        else:
            route.continue_()
    except Exception:
        # Playwright call logs may include form bodies or auth headers. Never
        # propagate an original transport exception from this event callback.
        try:
            route.abort(error_code="failed")
        except Exception:
            pass  # Context may already be closed; do not expose its call log.
    finally:
        if response is not None:
            try:
                response.dispose()
            except Exception:
                pass


def _text(value: str) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def _period(start: str, end: str) -> tuple[date, date]:
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or "") for v in (start, end)):
        raise GloProError("Даты периода должны иметь формат ГГГГ-ММ-ДД.")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        raise GloProError("Указана несуществующая дата периода.") from None
    if first > last:
        raise GloProError("Начало периода позже его окончания.")
    return first, last


def _matches_report_download(url: str, expected: dict[str, str]) -> bool:
    """Accept only the native attachment for this exact Generate request.

    The portal opens /reports/generate/ in a new window and returns an XLSX
    attachment. Its asynchronously loaded report history is unrelated evidence.
    """
    if not GloProConnector._origin_checked(url):
        return False
    parsed = urlparse(url)
    if parsed.path.rstrip("/") != "/reports/generate":
        return False
    fields = parse_qs(parsed.query, keep_blank_values=True)
    for name in ("build", "report_id", "period_start", "period_end", "format"):
        if fields.get(name) != [expected[name]]:
            return False
    scopes = []
    for key, names in fields.items():
        match = re.fullmatch(r"additional\[(\d+)\]\[name\]", key)
        if match and any(name.startswith(("client_choose_single", "contract_choose_single")) for name in names):
            if names != [expected["contract_field"]]:
                return False
            scopes.append(f"additional[{match[1]}]")
    if len(scopes) != 1:
        return False
    scope = scopes[0]
    # GloPro serializes the firm as the parent of the selected contract, whose
    # field name includes the template/widget ID (e.g. contract_choose_single-19_0).
    for suffix, value in {
        "[value]": expected["contract_choose_single"],
        "[data][parent][name]": "client_choose_single",
        "[data][parent][value]": expected["client_choose_single"],
    }.items():
        if fields.get(scope + suffix) != [value]:
            return False
    return True


def _filename(client: str, client_id: str, reserved: set[str]) -> str:
    # Keep quotes and company spelling: user sends files named ООО "НАЗВАНИЕ".
    stem = re.sub(r'[\x00-\x1f\x7f/\\]', "_", client).strip().strip(".")
    # Most filesystems cap a filename at 255 bytes, not 255 Unicode characters.
    stem = (stem or "Клиент").encode("utf-8")[:190].decode("utf-8", errors="ignore")
    candidate = stem + ".xlsx"
    if candidate.casefold() in reserved:
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(client_id))[:40] or "id"
        candidate = f"{stem} ({safe_id}).xlsx"
        serial = 2
        while candidate.casefold() in reserved:
            candidate = f"{stem} ({safe_id}-{serial}).xlsx"
            serial += 1
    reserved.add(candidate.casefold())
    return candidate


def _validate_workbook(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size == 0:
        raise GloProError("GloPro вернул пустой файл отчёта.")
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"PK\x03\x04":
                raise GloProError("Вместо XLSX получен другой файл; отчёт не опубликован.")
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml"}.issubset(names):
                raise GloProError("Полученный ZIP не является книгой Excel XLSX.")
            if not any(n.startswith("xl/worksheets/") and n.endswith(".xml") for n in names):
                raise GloProError("В полученной книге Excel нет листов.")
            if archive.testzip() is not None:
                raise GloProError("Полученная книга Excel повреждена.")
    except (zipfile.BadZipFile, OSError):
        raise GloProError("Не удалось проверить скачанную книгу Excel.") from None
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _select_requested(all_clients: list[dict], requested: Iterable[dict | str] | None) -> list[dict]:
    if not requested:
        return list(all_clients)
    by_id = {str(client["id"]): client for client in all_clients}
    chosen = []
    seen = set()
    for request in requested:
        if isinstance(request, str):
            name, client_id = _text(request), None
        elif isinstance(request, dict):
            name = _text(request.get("name") or request.get("client") or "")
            client_id = request.get("id", request.get("client_id"))
        else:
            raise GloProError("Клиент должен быть названием или объектом с id/name.")
        if client_id is not None:
            match = by_id.get(str(client_id))
            if match is None:
                raise GloProError(f"Клиент с ID {client_id} отсутствует в доступном списке GloPro.")
            if name and _text(match["name"]) != name:
                raise GloProError(f"Название клиента с ID {client_id} отличается от настройки оператора.")
        else:
            matches = [client for client in all_clients if _text(client["name"]) == name]
            if len(matches) != 1:
                raise GloProError(f"Клиент «{name}» не найден однозначно; укажите его ID.")
            match = matches[0]
        if match["id"] not in seen:
            chosen.append(match)
            seen.add(match["id"])
    return chosen


class _PaginationEvidence:
    """Evidence from the exact JSON contract used by GloPro's site.js."""
    def __init__(self):
        self.pages = {}
        self.error = None
        self.changed_at = time.monotonic()

    def add(self, offset, payload):
        if type(offset) is not int or offset < 0 or not isinstance(payload, dict) or payload.get("success") not in (True, 1):
            raise GloProError("GloPro не подтвердил успешную загрузку страницы фирм.")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("more") not in (True, False, 0, 1):
            raise GloProError("Изменилась схема пагинации GloPro; полнота списка не подтверждена.")
        items = data.get("items")
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            raise GloProError("Ответ списка фирм GloPro не содержит ожидаемых записей.")
        advance = data.get("count", len(items))
        if isinstance(advance, str) and advance.isdigit():
            advance = int(advance)
        if type(advance) is not int or advance < 0 or (data["more"] and advance == 0):
            raise GloProError("GloPro не подтвердил смещение следующей страницы фирм.")
        ids = {}
        for item in items:
            if not isinstance(item, dict) or not str(item.get("CLIENT_ID", "")).isdigit() or not _text(item.get("CLIENT_NAME", "")):
                raise GloProError("Ответ списка фирм GloPro содержит запись без ID или названия.")
            ident, name = str(item["CLIENT_ID"]), _text(html.unescape(item["CLIENT_NAME"]))
            if ident in ids and ids[ident] != name:
                raise GloProError("Названия фирмы в ответе GloPro неоднозначны.")
            ids[ident] = name
        entry = {"advance": advance, "more": bool(data["more"]), "ids": ids}
        if offset in self.pages and self.pages[offset] != entry:
            raise GloProError("Список фирм изменился во время пагинации; требуется повторить выгрузку.")
        self.pages[offset] = entry
        if len(self.pages) > MAX_CLIENT_PAGES:
            raise GloProError("Превышен предел страниц фирм GloPro.")
        self.changed_at = time.monotonic()

    def complete_ids(self):
        offset, ids = 0, {}
        visited = set()
        while offset in self.pages:
            if offset in visited:
                raise GloProError("Пагинация GloPro зациклилась.")
            visited.add(offset)
            entry = self.pages[offset]
            for ident, name in entry["ids"].items():
                if ident in ids and ids[ident] != name:
                    raise GloProError("Название фирмы изменилось во время выгрузки.")
                ids[ident] = name
            if not entry["more"]:
                return ids
            offset += entry["advance"]
        return None


class GloProConnector:
    def __init__(self, username: str, password: str, *, headless: bool = True, timeout: int = 120):
        if not username or not password:
            raise GloProError("Введите логин и пароль GloPro в настройках подключения.")
        self._username = username
        self._password = password
        self._headless = headless
        self._timeout = timeout

    def __repr__(self) -> str:
        return "GloProConnector(credentials=<hidden>)"

    @contextmanager
    def _browser_page(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise GloProError("Не установлен Playwright. Установите зависимости приложения и Chromium.") from None
        channel, transport = _browser_channel(), _transport_mode()
        with sync_playwright() as playwright:
            try:
                launch_options = {"headless": self._headless}
                if channel:
                    launch_options["channel"] = channel
                browser = playwright.chromium.launch(**launch_options)
            except Exception:
                if channel == "chrome":
                    raise GloProError("Не удалось запустить Google Chrome. Проверьте установку Chrome или выберите GLOPRO_BROWSER_CHANNEL=chromium.") from None
                raise GloProError("Не удалось запустить Chromium. Выполните: python -m playwright install chromium") from None
            try:
                context = browser.new_context(accept_downloads=True, locale="ru-RU", timezone_id="Europe/Moscow")
                context.route("**/*", lambda route: _route_request(route, transport))
                context.set_default_timeout(30_000)
                context.set_default_navigation_timeout(30_000)
                yield context.new_page()
            finally:
                browser.close()

    @staticmethod
    def _origin_checked(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.netloc == "lk.glopro.ru"

    def _goto(self, page, url):
        if not self._origin_checked(url):
            raise GloProError("Переход за пределы lk.glopro.ru запрещён.")
        route_name = re.sub(r"[\x00-\x1f\x7f]", "", urlparse(url).path or "/")[:120]
        for attempt in range(3):
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                if response and response.status >= 500:
                    raise GloProError("GloPro временно не отвечает.")
                if not self._origin_checked(page.url):
                    raise GloProError("GloPro перенаправил запрос за пределы своего личного кабинета.")
                return
            except Exception as error:
                transient = type(error).__name__ == "TimeoutError" or "net::ERR_" in str(error) or (isinstance(error, GloProError) and "временно" in str(error))
                if not transient or attempt == 2:
                    raise GloProError(f"Страница GloPro «{route_name}» не загрузилась. Проверьте доступность сервиса и повторите запуск.") from None
                page.wait_for_timeout(700 * (attempt + 1))

    def _login(self, page):
        self._goto(page, ORIGIN)
        if not self._origin_checked(page.url):
            raise GloProError("Страница входа перенаправлена за пределы lk.glopro.ru.")
        login = page.locator("form#login")
        if login.count():
            password = login.locator('input[name="password"]')
            # Do not assume whether this deployment names the field login or username.
            username = login.locator('input:not([type="hidden"]):not([name="password"]):not([type="submit"]):not([type="button"]):not([type="checkbox"])')
            if username.count() != 1 or password.count() != 1:
                raise GloProError("Форма входа GloPro изменилась; требуется обновить адаптер.")
            username.fill(self._username)
            password.fill(self._password)
            password.press("Enter")
            try:
                login.wait_for(state="hidden", timeout=30_000)
            except Exception:
                raise GloProError("Вход GloPro не завершён. Проверьте учётные данные и доступность сервиса.") from None
        if not self._origin_checked(page.url):
            raise GloProError("GloPro перенаправил запрос за пределы своего личного кабинета.")
        self._ensure_clients_list(page)

    def _ensure_clients_list(self, page):
        if urlparse(page.url).path.rstrip("/") != "/clients":
            self._goto(page, ORIGIN + "/clients")
        try:
            page.locator(".ajax_block_clients_out").wait_for(state="attached")
        except Exception:
            raise GloProError("В аккаунте недоступен ожидаемый список фирм GloPro.") from None

    def _items(self, page) -> list[dict]:
        raw = page.locator(".card.client").evaluate_all("""els => els.map(e => ({
          name: e.querySelector('h3 a')?.innerText || '',
          url: e.querySelector('h3 a')?.href || '',
          contracts: [...e.querySelectorAll('a[href*="contract_id="]')].map(a => a.href)
        }))""")
        result = []
        for item in raw:
            parsed = urlparse(item["url"])
            match = re.fullmatch(r"/clients/client/(\d+)", parsed.path)
            if not self._origin_checked(item["url"]) or not match:
                raise GloProError("Ссылка фирмы в GloPro изменилась; полнота выгрузки не подтверждена.")
            contracts = {}
            for url in item["contracts"]:
                contract_url = urlparse(url)
                ids = parse_qs(contract_url.query).get("contract_id", [])
                if not self._origin_checked(url) or contract_url.path != parsed.path or len(ids) != 1 or not ids[0].isdigit():
                    raise GloProError("Некорректная ссылка договора в списке GloPro.")
                contracts.setdefault(ids[0], {"id": ids[0], "url": url})
            result.append({"id": match[1], "name": item["name"], "url": item["url"], "contracts": list(contracts.values())})
        return result

    @staticmethod
    def _more(page):
        return page.locator(".ajax_block_clients_out .ajax_block_load_all:visible")

    @staticmethod
    def _loading(page):
        return bool(page.locator(".ajax_block_clients_out.loading:visible, .ajax_block_clients_out .loading:visible, .ajax_block_clients_out .spinner:visible, .ajax_block_clients_out .fa-circle-notch:visible").count())

    @staticmethod
    def _merge_items(collected: dict, items: list[dict]):
        for item in items:
            if not item.get("id") or not _text(item.get("name", "")):
                raise GloProError("В списке GloPro появился клиент без ID или названия; выгрузка остановлена.")
            client = {**item, "id": str(item["id"]), "name": _text(item["name"])}
            previous = collected.get(client["id"])
            if previous and previous["name"] != client["name"]:
                raise GloProError("GloPro вернул разные названия для одного ID клиента.")
            collected[client["id"]] = client

    def _start_client_listing(self, page):
        # Reload only after attaching the response observer. The login redirect's
        # first AJAX response may already have finished by the time this method runs.
        self._goto(page, ORIGIN + "/clients")
        self._ensure_clients_list(page)

    def _list_clients(self, page) -> list[dict]:
        evidence = _PaginationEvidence()

        def observe(response):
            if not self._origin_checked(response.url) or urlparse(response.url).path != "/clients/clients-list":
                return
            try:
                fields = parse_qs(response.request.post_data or "")
                offsets = fields.get("offset", [])
                if len(offsets) != 1 or not offsets[0].isdigit():
                    raise GloProError("Не удалось подтвердить смещение страницы фирм GloPro.")
                evidence.add(int(offsets[0]), response.json())
            except GloProError as error:
                evidence.error = str(error)
            except Exception:
                evidence.error = "Не удалось прочитать ответ списка фирм GloPro; частичная выгрузка запрещена."

        page.on("response", observe)
        try:
            self._start_client_listing(page)
            deadline = time.monotonic() + max(90, self._timeout)
            last_click = 0
            previous_complete = None
            stable_since = None
            while time.monotonic() < deadline:
                if evidence.error:
                    raise GloProError(evidence.error)
                expected = evidence.complete_ids()
                current = {}
                self._merge_items(current, self._items(page))
                actual = {ident: client["name"] for ident, client in current.items()}
                if expected is not None and actual == expected and not self._loading(page):
                    if previous_complete == actual and stable_since is not None and time.monotonic() - stable_since >= 0.5:
                        return list(current.values())
                    if previous_complete != actual:
                        stable_since = time.monotonic()
                        previous_complete = actual
                else:
                    previous_complete, stable_since = None, None
                # Let GloPro's own 1000 ms auto-load-all timer run first. A click
                # only starts loading if the UI is idle; its JS drives all later pages.
                now = time.monotonic()
                if expected is None and evidence.pages and now - evidence.changed_at >= 1.5 and now - last_click >= 2 and not self._loading(page):
                    more = self._more(page)
                    if more.count():
                        more.first.click()
                        last_click = now
                page.wait_for_timeout(150)
            raise GloProError("GloPro не подтвердил конец списка фирм и совпадение всех ID с экраном. Частичная выгрузка запрещена.")
        finally:
            page.remove_listener("response", observe)

    def _read_account_preview(self, page):
        panel = page.locator('.ajax_contract_block .tab-content')
        panel.wait_for(state="visible")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            text = panel.inner_text()
            marker = re.search(r"Последние\s+транзакции|Баланс\s+р/с", text, re.I)
            loading = panel.locator('.loading:visible, .spinner:visible, .fa-circle-notch:visible').count()
            if marker and not loading:
                dates = []
                for value in re.findall(r"\b\d{2}\.\d{2}\.\d{4}\b", text):
                    try:
                        day, month, year = map(int, value.split("."))
                        dates.append(date(year, month, day).isoformat())
                    except ValueError:
                        continue
                return {"checked": True, "section": marker.group(0), "visible_date_count": len(dates), "visible_dates": sorted(set(dates)), "scope": "visible_account_preview_only"}
            page.wait_for_timeout(150)
        raise GloProError("Вкладка «Счет» не загрузила данные; проверка счёта не подтверждена.")

    def _contracts(self, page, client):
        # Read the complete selector on the firm itself, including contracts that
        # could be absent from the overview's current filter.
        self._goto(page, client["url"])
        selector = page.locator('select[name="contracts_list"]')
        selector.wait_for(state="attached")
        ids = selector.locator("option").evaluate_all("els => els.map(e => e.value).filter(Boolean)")
        contracts = []
        observed = {c["id"]: c["url"] for c in client.get("contracts", [])}
        for ident in ids:
            if not str(ident).isdigit():
                raise GloProError("В списке договоров GloPro появился неизвестный идентификатор.")
            # URL shape and parameter are verified from the portal's own contract links.
            contracts.append({"id": str(ident), "url": observed.get(str(ident), client["url"] + "?contract_id=" + str(ident))})
        return contracts

    @staticmethod
    def _fill_date(locator, value: date):
        current = locator.input_value()
        date_type = locator.get_attribute("type")
        rendered = value.strftime("%d.%m.%Y") if date_type != "date" and re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", current) else value.isoformat()
        locator.fill(rendered)
        locator.press("Tab")
        if locator.input_value() != rendered:
            raise GloProError("GloPro не принял даты отчётного периода.")

    def _check_activity(self, page, contract, first, last):
        response = None
        try:
            response = page.context.request.post(ORIGIN + "/clients/contract-transactions", form={"contract_id": contract["id"], "offset": "0"},
                                                 headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20_000, max_redirects=0)
            if response.status != 200:
                raise GloProError("GloPro не подтвердил проверку операций за период")
            return activity_from_recent(response.json(), first, last)
        except GloProError:
            raise
        except Exception:
            raise GloProError("Не удалось проверить операции GloPro до скачивания отчёта") from None
        finally:
            if response is not None:
                response.dispose()

    def _prepare_report(self, page, client: dict, contract: dict, first: date, last: date):
        selector = page.locator('select[name="contracts_list"]')
        already_selected = urlparse(page.url).path == urlparse(client["url"]).path and selector.count() == 1 and selector.input_value() == contract["id"]
        if not already_selected:
            self._goto(page, contract["url"])
        account = page.locator('.ajax_contract_block a[href="#account"][ajax_tab]')
        account.wait_for(state="visible")
        account.click()
        page.locator('.ajax_contract_block a[href="#account"].active').wait_for(state="visible")
        account_preview = self._read_account_preview(page)
        activity = self._check_activity(page, contract, first, last)
        account_preview["activity"] = activity
        if not activity["has_operations"]:
            return account_preview
        page.locator('.ajax_contract_block a[href="#reports"][ajax_tab]').click()
        select = page.locator(FORM + " select.report_select")
        select.wait_for(state="visible")
        selected = select.select_option(label="Транзакционный отчет со скидкой")
        if len(selected) != 1:
            raise GloProError("Не найден транзакционный отчёт со скидкой.")
        form = page.locator(FORM + f' .report_template_block[report="{selected[0]}"]')
        form.locator('input[name="period_start"]').wait_for(state="visible")
        expected = [("client_choose_single", client["id"]), ("contract_choose_single", contract["id"])]
        for field, ident in expected:
            hidden = form.locator(f'.combobox_outer:has(input[name^="{field}"]) input[name="combobox_value"]')
            deadline = time.monotonic() + 5
            while hidden.count() == 1 and hidden.input_value() != ident and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            if hidden.count() != 1 or hidden.input_value() != ident:
                raise GloProError("Форма отчёта GloPro выбрала другую фирму или договор.")
        self._fill_date(form.locator('input[name="period_start"]'), first)
        self._fill_date(form.locator('input[name="period_end"]'), last)
        # GloPro styles the format as a clickable label; its native radio can
        # be hidden. Click the visible control, then verify the actual value.
        xlsx = form.locator('[format="xlsx"]')
        xlsx.click()
        if not xlsx.locator('input[type="radio"]').is_checked():
            raise GloProError("GloPro не подтвердил выбор формата XLSX.")
        return account_preview

    @staticmethod
    def _download_parameters(page) -> dict[str, str]:
        form = page.locator(FORM + " .report_template_block:visible")
        if form.count() != 1 or form.locator('[format="xlsx"] input[type="radio"]:checked').count() != 1:
            raise GloProError("Форма отчёта GloPro не подтверждает выбранный формат XLSX.")
        expected = {"build": "1", "format": "xlsx", "report_id": form.get_attribute("report")}
        for field in ("period_start", "period_end"):
            expected[field] = form.locator(f'input[name="{field}"]').input_value()
        for field in ("client_choose_single", "contract_choose_single"):
            widget = form.locator(f'input[name^="{field}"]')
            hidden = form.locator(f'.combobox_outer:has(input[name^="{field}"]) input[name="combobox_value"]')
            if widget.count() != 1 or hidden.count() != 1:
                raise GloProError("Форма отчёта GloPro не подтверждает выбранную фирму или договор.")
            expected[field] = hidden.input_value()
            if field == "contract_choose_single":
                expected["contract_field"] = widget.get_attribute("name")
        if any(not str(expected[field] or "").isdigit() for field in ("report_id", "client_choose_single", "contract_choose_single")):
            raise GloProError("Форма отчёта GloPro содержит неизвестный шаблон, фирму или договор.")
        return expected

    def _download(self, page, destination: Path):
        expected = self._download_parameters(page)
        downloaded = []

        def listener(download):
            if _matches_report_download(download.url, expected):
                downloaded.append(download)

        # A new window can emit its attachment on the opener or on the popup.
        # Listen at context level before clicking, and bind it to the form scope.
        page.context.on("download", listener)
        try:
            # Only click Generate once. Retrying this operation could create duplicates.
            page.locator(FORM + ' .report_template_block:visible span[onclick="generateReport($(this))"]').click()
            deadline = time.monotonic() + self._timeout
            while not downloaded and time.monotonic() < deadline:
                page.wait_for_timeout(200)
            if len(downloaded) != 1:
                raise GloProError("GloPro не выдал файл для выбранной фирмы, договора и периода вовремя. Старые файлы из очереди не используются.")
            download = downloaded[0]
            if download.failure():
                raise GloProError("Скачивание отчёта GloPro прервано.")
            temporary = destination.with_name("." + destination.name + ".partial")
            try:
                download.save_as(str(temporary))
                metadata = _validate_workbook(temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            return metadata
        except GloProError:
            raise
        except Exception:
            raise GloProError("Скачивание нового отчёта GloPro прервано. Проверьте доступность сервиса и повторите запуск.") from None
        finally:
            page.context.remove_listener("download", listener)

    def check_connection(self) -> dict:
        try:
            with self._browser_page() as page:
                self._login(page)
                clients = self._list_clients(page)
                return {"ok": True, "origin": ORIGIN, "client_count": len(clients), "clients": clients}
        except GloProError:
            raise
        except Exception:
            raise GloProError("Не удалось проверить подключение GloPro. Сервис недоступен или его интерфейс изменился.") from None

    def download_reports(self, start: str, end: str, output_dir: Path, clients: list[dict | str] | None = None, progress: Progress | None = None, excluded_clients: list[dict] | None = None, *, run_date: str | None = None) -> list[dict]:
        from .config import client_period_for
        first, last = _period(start, end)
        scheduled_date = date.fromisoformat(run_date) if run_date else last + timedelta(days=1)
        exclusions = [] if excluded_clients is None else excluded_clients
        try:
            validate_excluded_clients(exclusions)
        except ValueError as exc:
            raise GloProError(str(exc)) from None
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results = []
        notify = progress or (lambda event: None)
        reserved_by_dir = {}
        try:
            with self._browser_page() as page:
                notify({"stage": "connecting", "message": "Вход в GloPro"})
                self._login(page)
                available = self._list_clients(page)
                selected = _select_requested(available, clients)
                if not selected:
                    raise GloProError("В GloPro не найдено ни одного доступного клиента.")
                eligible = []
                periods = {}
                for client in selected:
                    if client_is_excluded(client, exclusions):
                        notify({"stage": "skipped", "message": f"Временно пропущен по настройке: {client['name']}", "client": client["name"], "client_id": client["id"], "reason": "excluded_client"})
                    else:
                        period = client_period_for(scheduled_date, client["name"], client["id"])
                        if period is None:
                            notify({"stage": "skipped", "message": f"Только вторничный запуск: {client['name']}", "client": client["name"], "client_id": client["id"], "reason": "tuesday_only"})
                            continue
                        periods[client["id"]] = period if run_date or weekly_kind(client["name"], client["id"]) else (first, last)
                        eligible.append(client)
                selected = sorted(eligible, key=lambda client: alphabet_key(client["name"]))
                if not selected:
                    raise GloProError("Все выбранные клиенты временно исключены; нет клиентов для отчёта.")
                notify({"stage": "clients", "message": f"Найдено клиентов: {len(selected)}", "total": len(selected)})
                for index, client in enumerate(selected, 1):
                    client_first, client_last = periods[client["id"]]
                    notify({"stage": "downloading", "message": f"Выгрузка: {client['name']}", "client": client["name"], "current": index, "total": len(selected)})
                    contracts = self._contracts(page, client)
                    if not contracts:
                        notify({"stage": "no_contract", "message": f"Нет договоров: {client['name']}", "client": client["name"]})
                        continue
                    for contract in contracts:
                        account_preview = self._prepare_report(page, client, contract, client_first, client_last)
                        if account_preview.get("activity", {}).get("has_operations") is False:
                            notify({"stage": "skipped", "reason": "no_operations", "client": client["name"], "client_id": client["id"],
                                    "contract_id": contract["id"], "period": [client_first.isoformat(), client_last.isoformat()],
                                    "evidence": account_preview["activity"], "message": f"Нет заправок, XLSX не запрашивается: {client['name']}"})
                            continue
                        folder = output_dir / ("Договор " + contract["id"]) if len(contracts) > 1 else output_dir
                        folder.mkdir(parents=True, exist_ok=True)
                        reserved = reserved_by_dir.setdefault(str(folder), {p.name.casefold() for p in folder.iterdir()})
                        path = folder / _filename(client["name"], client["id"], reserved)
                        metadata = self._download(page, path)
                        results.append({"client": client["name"], "client_id": client["id"], "contract_id": contract["id"], "account_checked": account_preview["checked"], "account_preview": account_preview, "template": "Транзакционный отчет со скидкой", "path": str(path), "start": client_first.isoformat(), "end": client_last.isoformat(), **metadata})
                notify({"stage": "downloaded", "message": "Проверены все фирмы; скачаны отчёты с операциями", "total": len(results), "activity_check_complete": True})
                return results
        except GloProError:
            raise
        except Exception:
            raise GloProError("Выгрузка GloPro прервана: сеть недоступна или форма изменилась. Неполный набор не считается готовым.") from None
