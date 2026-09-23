"""Yandex corporate fuel exports through the same verified cabinet UI."""
from datetime import date
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from . import storage, yandex_connection
from .yandex_reports import read_report, period_for
from .yandex_cabinet import Cabinet, CabinetError, collect_orders, discover_employees, API, REPORTS, ID


class YandexError(ValueError):
    pass


def wait_ready(page):
    # Only wait for report controls; global networkidle is blocked by analytics.
    # Order completeness is established independently through authenticated API pages.
    page.wait_for_timeout(1200)
    page.locator('[id*="-skeleton_row_"]').first.wait_for(state="hidden", timeout=60000)


def verify_page(page, company):
    page.get_by_role("button", name="Все фильтры", exact=True).wait_for(timeout=30000)
    if not page.get_by_role("link", name=company, exact=True).is_visible():
        storage.set_setting("yandex_verified", False)
        raise YandexError("Войдите в Яндекс на странице «Подключения»: нужен кабинет " + company)
    notice = page.get_by_role("dialog").filter(has_text="В Заправках появились зарядные станции")
    if notice.is_visible():
        # Dismissal is fine; accepting a supplier's offer is a user's decision.
        page.keyboard.press("Escape")
        if notice.is_visible():
            raise YandexError("Яндекс просит принять новую оферту. Откройте «Подключить Яндекс» и самостоятельно ознакомьтесь с условиями в кабинете.")


def open_reports(page):
    page.get_by_role("button", name="Отчёты", exact=True).click(timeout=60000)
    page.get_by_role("button", name="Все отчёты", exact=True).wait_for(timeout=60000)
    wait_ready(page)


def download_employee(cabinet, page, directory, employee, start, end, company, aliases, progress):
    if not employee["active"]:
        progress({"stage": "skipped", "reason": "no_operations", "client": employee["name"],
                  "user_id": employee["user_id"], "period": [str(start), str(end)]})
        return None
    history = cabinet.request(REPORTS + "history")
    if not isinstance(history.get("reports"), list):
        raise YandexError("Не удалось проверить список ранее созданных отчётов")
    before = {r["task_id"] for r in history["reports"]}
    tabs = cabinet.request(REPORTS + "tab-columns").get("tanker")
    if not isinstance(tabs, list) or len(tabs) != 1 or tabs[0].get("tab") != "report.report":
        raise YandexError("Изменился формат отчётов Яндекса")
    columns = [column["id"] for column in tabs[0]["columns"]]
    required = {"due_data", "user_fullname", "user_phone", "order_id", "fuel_type", "fuel_filled", "status", "price"}
    if not required.issubset(columns):
        raise YandexError("В отчёте Яндекса недостаёт столбцов для проверки")
    created = cabinet.request(REPORTS + "generate", {
        "service": "tanker", "since_date": str(start), "till_date": str(end),
        "time_zone": "+03:00", "user_ids": [employee["user_id"]],
        "columns_by_tabs": [{"tab": "report.report", "columns": columns}]})
    identity = created.get("task_id")
    if not isinstance(identity, str) or not ID.fullmatch(identity) or identity in before:
        raise YandexError("Яндекс не подтвердил ID нового отчёта")
    progress({"stage": "checking", "client": employee["name"], "user_id": employee["user_id"],
              "report_id": identity, "message": "Создан новый отчёт, ожидаем файл"})
    deadline = time.monotonic() + 180
    while True:
        state = cabinet.request(REPORTS + "status", {"task_id": identity})
        if state.get("task_id") != identity:
            raise YandexError("Яндекс вернул состояние другого отчёта")
        if state.get("status") == "complete":
            break
        if state.get("status") not in {"pending", "processing", "queued", "created", "in_progress", "new", "running"}:
            raise YandexError("Создание отчёта Яндекса завершилось неизвестным состоянием или ошибкой")
        if time.monotonic() >= deadline:
            raise YandexError("Яндекс не завершил создание нового отчёта")
        page.wait_for_timeout(1500)
    # Use the cabinet's own download link; signed URLs and auth stay out of logs.
    page.reload(wait_until="domcontentloaded", timeout=60000)
    verify_page(page, company)
    open_reports(page)
    option = page.locator(f'[id$="-option-{identity}"]')
    option.wait_for(timeout=60000)
    temporary = directory / f".{employee['user_id']}.xlsx"
    destination = directory / f"Яндекс. {employee['label']}.xlsx"
    try:
        with page.expect_download(timeout=90000) as event:
            option.get_by_role("button", name="Скачать", exact=True).click()
        event.value.save_as(temporary)
        report = read_report(temporary, start, end, company, aliases=aliases,
                             expected_orders=employee["orders"])
        if report["user_id"] != employee["user_id"] or not report["active"]:
            raise YandexError("Excel не подтвердил заправки выбранного сотрудника")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    progress({"stage": "downloaded", "client": employee["name"], "user_id": employee["user_id"],
              "report_id": identity, "order_count": len(employee["orders"]), "period": [str(start), str(end)]})
    return {"path": str(destination), "user_id": employee["user_id"], "report_id": identity}


def download_reports(conf, record, directory, progress):
    start, end = period_for(date.fromisoformat(record["run_date"]))
    if not yandex_connection.session_path().is_file():
        raise YandexError("Подключите кабинет Яндекса на странице «Подключения»")
    aliases = conf.get("employee_aliases", conf.get("employees", []))
    results = []
    step = "открытие кабинета"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=str(yandex_connection.session_path()),
                locale="ru-RU", timezone_id="Europe/Moscow", accept_downloads=True)
            page = context.new_page()
            try:
                with page.expect_response(lambda r: r.url == API + "/corp-cabinet/1.0/clients", timeout=60000) as response:
                    page.goto(yandex_connection.URL, wait_until="domcontentloaded", timeout=60000)
                verify_page(page, conf["company"])
                cabinet = Cabinet(context, response.value, conf["company"])
                step = "полная выборка заказов недели"
                progress({"stage": "checking", "message": "Проверяем все страницы заказов и определяем сотрудников"})
                manifest = collect_orders(cabinet, start, end)
                employees = discover_employees(manifest, aliases)
                progress({"stage": "discovered", "employee_count": len(employees),
                          "manifest": manifest, "message": f"Заказов: {manifest['order_count']}, сотрудников: {len(employees)}"})
                for employee in employees:
                    step = "создание и сверка отчёта сотрудника"
                    progress({"stage": "checking", "client": employee["name"],
                              "user_id": employee["user_id"], "message": "Сверяем отдельный новый Excel с заказами"})
                    source = download_employee(cabinet, page, directory, employee, start, end,
                                               conf["company"], aliases, progress)
                    if source:
                        results.append(source)
                step = "повторная проверка полноты недели"
                final_manifest = collect_orders(cabinet, start, end)
                if manifest != final_manifest:
                    raise YandexError("Заказы Яндекса изменились во время выгрузки; итог не опубликован, повторите запуск")
                if not employees:
                    progress({"stage": "skipped", "reason": "no_operations", "client": conf["company"],
                              "period": [str(start), str(end)]})
                yandex_connection.save_session(context)
                storage.set_setting("yandex_verified", True)
                progress({"stage": "downloaded", "activity_check_complete": True,
                          "order_count": manifest["order_count"], "employee_ids": [e["user_id"] for e in employees]})
            finally:
                browser.close()
    except (YandexError, CabinetError):
        raise
    except ValueError as exc:
        raise YandexError(str(exc)) from None
    except PlaywrightTimeout:
        raise YandexError("Яндекс не ответил вовремя: " + step + ". Проверьте подключение; старые отчёты не использованы.") from None
    except Exception:
        raise YandexError("Выгрузка Яндекса не подтверждена: " + step + ". Проверьте вход и повторите запуск.") from None
    return results
