"""Interactive first login and a private session for scheduled Yandex exports."""
from pathlib import Path
from threading import Lock
import json
import os
import subprocess
import sys
import tempfile
import time

from . import storage

URL = "https://business.taxi.yandex.ru/orders-v2/list/tanker"
COMPANY = "ООО НК АРТЭЛЬ"
_process = None
_lock = Lock()


def session_path():
    return storage.DATA / "yandex-session.json"


def status():
    pid = storage.setting("yandex_connection_pid")
    connecting = False
    if pid:
        try:
            os.kill(pid, 0)
            connecting = True
        except ProcessLookupError:
            pass
    return {"configured": session_path().is_file(),
            "verified": bool(session_path().is_file() and storage.setting("yandex_verified", False)),
            "connecting": connecting or _process is not None and _process.poll() is None,
            "company": COMPANY,
            "error": storage.setting("yandex_connection_error")}


def save_session(context):
    fd, name = tempfile.mkstemp(prefix="yandex-session-", suffix=".tmp", dir=storage.DATA)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(context.storage_state(), stream)
        os.replace(name, session_path())
    finally:
        Path(name).unlink(missing_ok=True)


def connect():
    global _process
    with _lock:
        if status()["connecting"]:
            return status()
        if any(r["status"] in ("queued", "running") for r in storage.runs()):
            raise ValueError("Сначала дождитесь завершения текущего запуска")
        storage.set_setting("yandex_connection_error", None)
        _process = subprocess.Popen([sys.executable, "-m", "operator_app.yandex_connection"],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    env={**os.environ, "OPERATOR_DATA_DIR": str(storage.DATA)},
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return status()


def login():
    from playwright.sync_api import sync_playwright
    storage.set_setting("yandex_connection_pid", os.getpid())
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow",
                                          storage_state=str(session_path()) if session_path().is_file() else None)
            page = context.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise ValueError("Окно входа закрыто до подтверждения подключения")
                if page.get_by_role("link", name=COMPANY, exact=True).is_visible():
                    if "/orders-v2/list/tanker" not in page.url:
                        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                    page.get_by_role("button", name="Все фильтры", exact=True).wait_for(timeout=60000)
                    page.wait_for_timeout(1000)
                    page.wait_for_load_state("networkidle", timeout=60000)
                    notice = page.get_by_role("dialog").filter(has_text="В Заправках появились зарядные станции")
                    if notice.is_visible():
                        storage.set_setting("yandex_connection_error", "В окне Яндекса открыта новая оферта. Ознакомьтесь с условиями; приложение не принимает их автоматически.")
                        page.wait_for_timeout(1000)
                        continue
                    save_session(context)
                    storage.set_setting("yandex_verified", True)
                    storage.set_setting("yandex_connection_error", None)
                    browser.close()
                    return
                page.wait_for_timeout(1000)
            raise ValueError("Вход не завершён за 15 минут. Откройте подключение снова.")
    except Exception:
        storage.set_setting("yandex_verified", False)
        storage.set_setting("yandex_connection_error", "Вход в Яндекс не завершён. Откройте подключение и войдите в кабинет ООО НК АРТЭЛЬ.")
    finally:
        storage.set_setting("yandex_connection_pid", None)


if __name__ == "__main__":
    login()
