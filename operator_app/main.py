from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote
import asyncio
import os
import re
import secrets
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import engine, storage
from . import yandex_connection
from .config import DATA, OPERATORS, ROOT, get_operator, load_operators, next_run, parse_operator


async def scheduler():
    while True:
        try:
            await asyncio.to_thread(engine.tick)
            storage.set_setting("scheduler_error", None)
        except Exception:
            storage.set_setting("scheduler_error", "Не удалось прочитать настройки расписания. Проверьте Markdown.")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app):
    storage.init()
    OPERATORS.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Артель · Оператор", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_security(request: Request, call_next):
    # Bind locally; remote use is through an authenticated SSH tunnel.
    allowed_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
    host = request.url.hostname
    if host not in allowed_hosts:
        return JSONResponse({"detail": "Разрешён только локальный адрес"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        return JSONResponse({"detail": "Запрос с другого сайта запрещён"}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Запрос с другого сайта запрещён"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


class MarkdownBody(BaseModel):
    markdown: str = Field(max_length=100_000)


class NewOperator(MarkdownBody):
    id: str


class RunBody(BaseModel):
    operator_id: str = "glopro"
    run_date: str | None = None


class ConnectionBody(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


@app.get("/api/state")
def state():
    operators = []
    for conf in load_operators():
        planned = next_run(conf)
        if conf["kind"] == "seo":
            from .seo import next_due
            due = next_due(conf)
            planned = due.isoformat() if due else None
        operators.append({k: v for k, v in {**conf, "next_run": planned}.items() if k != "markdown"})
    return {"operators": operators, "runs": storage.runs(), "server_time": storage.now_iso(),
            "yandex": yandex_connection.status(),
            "connection": {"configured": bool(storage.credentials()), "verified": storage.setting("connection_verified", False)},
            "scheduler_error": storage.setting("scheduler_error"), "runtime": "Локальное приложение. Для расписания процесс должен работать."}


@app.post("/api/yandex/connect")
def yandex_connect():
    return yandex_connection.connect()


@app.get("/api/operators/{ident}")
def read_operator(ident: str):
    return {"markdown": get_operator(ident)["markdown"]}


def write_operator(ident, markdown):
    conf = parse_operator(markdown, ident)
    existing = OPERATORS / f"{ident}.md"
    was_enabled = get_operator(ident)["enabled"] if existing.exists() else False
    temporary = existing.with_suffix(".tmp")
    temporary.write_text(markdown, encoding="utf-8")
    os.replace(temporary, existing)
    if conf["enabled"] and not was_enabled:
        storage.set_setting(f"enabled_since:{ident}", storage.now_iso())
    if not conf["enabled"]:
        storage.set_setting(f"enabled_since:{ident}", None)
    return {"ok": True, "next_run": next_run(conf)}


@app.put("/api/operators/{ident}")
def update_operator(ident: str, body: MarkdownBody):
    get_operator(ident)
    return write_operator(ident, body.markdown)


@app.post("/api/operators")
def create_operator(body: NewOperator):
    parse_operator(body.markdown, body.id)
    if (OPERATORS / f"{body.id}.md").exists():
        raise HTTPException(409, "Оператор с таким ID уже существует")
    return write_operator(body.id, body.markdown)


@app.post("/api/run", status_code=202)
def run(body: RunBody):
    return engine.submit(body.operator_id, body.run_date)


@app.post("/api/import", status_code=202)
async def import_files(files: list[UploadFile] = File(...), run_date: str | None = Form(None), operator_id: str = Form("glopro")):
    if not files or len(files) > 200:
        raise ValueError("Выберите от 1 до 200 XLSX-файлов")
    uploads, names = [], set()
    try:
        for upload in files:
            # Multipart encoders escape literal quotes as %22 in filename headers.
            name = Path(unquote(Path(upload.filename or "").name)).name
            if any(ord(char) < 32 for char in name):
                raise ValueError("Недопустимые символы в имени файла")
            if not name.lower().endswith(".xlsx") or name in names or len(name) > 220:
                raise ValueError("Нужны XLSX-файлы с различными именами до 220 символов")
            names.add(name)
            data = await upload.read(30_000_001)
            if len(data) > 30_000_000 or not data.startswith(b"PK\x03\x04"):
                raise ValueError("Файл должен быть XLSX размером до 30 МБ")
            fd, path = tempfile.mkstemp(suffix=".xlsx", dir=DATA)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            uploads.append({"path": path, "name": name})
        return engine.submit(operator_id, run_date, trigger="import", uploads=uploads)
    except Exception:
        for upload in uploads:
            Path(upload["path"]).unlink(missing_ok=True)
        raise


@app.post("/api/connection")
def connection(body: ConnectionBody):
    storage.save_credentials(body.username.strip(), body.password)
    return {"configured": True, "verified": False}


@app.post("/api/connection/test")
def test_connection():
    from .glopro import GloProConnector, GloProError
    creds = storage.credentials()
    if not creds:
        raise ValueError("Сначала сохраните подключение")
    try:
        result = GloProConnector(*creds).check_connection()
        storage.set_setting("connection_verified", True)
        return {"verified": True, **result}
    except GloProError as exc:
        storage.set_setting("connection_verified", False)
        raise ValueError(str(exc)) from None


@app.get("/api/runs/{ident}")
def read_run(ident: str):
    return storage.get_run(ident)


@app.get("/api/runs/{ident}/files/{filename:path}")
def download(ident: str, filename: str):
    record = storage.get_run(ident)
    if filename not in {f["name"] for f in record["files"]}:
        raise HTTPException(404, "Файл не найден")
    root = (DATA / "runs" / record["id"]).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, filename=path.name)


@app.get("/")
def index():
    return FileResponse(ROOT / "operator_app/static/index.html")


app.mount("/static", StaticFiles(directory=ROOT / "operator_app/static"), name="static")
