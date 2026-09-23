from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from operator_app import config, engine, main, storage


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    data = tmp_path / "data"
    operators = tmp_path / "operators"
    operators.mkdir()
    for module in (config, storage, engine, main):
        monkeypatch.setattr(module, "DATA", data)
    for module in (config, main):
        monkeypatch.setattr(module, "OPERATORS", operators)
    monkeypatch.setattr(storage, "credentials", lambda: None)
    markdown = """---
id: glopro
name: GloPro
kind: glopro
enabled: false
schedule:
  days: [tue, fri]
  time: '09:00'
  timezone: Europe/Moscow
rules: {}
---
Инструкция.
"""
    (operators / "glopro.md").write_text(markdown, encoding="utf-8")
    storage.init()
    # No context manager: lifespan and its scheduler must not start in API tests.
    client = TestClient(main.app)
    yield client, data, operators
    client.close()


@pytest.mark.parametrize("headers", [{"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}, {"host": "evil.example"}, {"origin": "http://localhost:9999"}])
def test_cross_site_write_rejected_without_saving_credentials(isolated_api, headers):
    client, data, _ = isolated_api
    response = client.post("/api/connection", headers=headers, json={"username": "fake", "password": "not-a-real-secret"})
    assert response.status_code == 403
    assert not (data / "credentials.json").exists()


def test_same_origin_state_has_security_headers(isolated_api):
    client, _, _ = isolated_api
    response = client.get("/api/state", headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.json()["connection"]["configured"] is False


def test_download_only_recorded_file_inside_run_root(isolated_api, tmp_path):
    client, data, _ = isolated_api
    record = storage.create_run("glopro", date(2026, 9, 18), date(2026, 9, 15), date(2026, 9, 17), "manual")
    root = data / "runs" / record["id"]
    root.mkdir()
    (root / "allowed.txt").write_text("public artifact")
    (root / "unlisted.txt").write_text("internal file")
    secret = tmp_path / "outside.txt"
    secret.write_text("private outside data")
    (root / "linked.txt").symlink_to(secret)
    record["files"] = [{"name": "allowed.txt"}, {"name": "../outside.txt"}, {"name": "linked.txt"}]
    storage.save_run(record)
    base = f"/api/runs/{record['id']}/files/"
    assert client.get(base + "allowed.txt").text == "public artifact"
    for filename in ("unlisted.txt", "%2e%2e%2foutside.txt", "linked.txt", "%2Fetc%2Fpasswd"):
        response = client.get(base + filename)
        assert response.status_code == 404
        assert "private outside data" not in response.text


def test_operator_id_cannot_escape_config_directory(isolated_api):
    client, _, operators = isolated_api
    existing = (operators / "glopro.md").read_text()
    malicious = existing.replace("id: glopro", "id: ../outside")
    response = client.post("/api/operators", json={"id": "../outside", "markdown": malicious})
    assert response.status_code == 400
    assert not (operators.parent / "outside.md").exists()
    assert (operators / "glopro.md").read_text() == existing


def test_invalid_instruction_is_not_written(isolated_api):
    client, _, operators = isolated_api
    path = operators / "glopro.md"
    original = path.read_text()
    response = client.put("/api/operators/glopro", json={"markdown": original.replace("time: '09:00'", "time: '29:00'")})
    assert response.status_code == 400
    assert path.read_text() == original


def _xlsx_bytes():
    book = Workbook()
    book.active.append(["Synthetic upload"])
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_import_strips_path_and_passes_only_staged_file(isolated_api, monkeypatch):
    client, data, _ = isolated_api
    captured = {}
    def submit(*args, **kwargs):
        captured.update(kwargs)
        return {"id": "test", "status": "queued"}
    monkeypatch.setattr(engine, "submit", submit)
    response = client.post("/api/import", files=[("files", ("../../client.xlsx", _xlsx_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))], data={"run_date": "2026-09-18"})
    assert response.status_code == 202
    upload = captured["uploads"][0]
    assert upload["name"] == "client.xlsx"
    assert Path(upload["path"]).parent == data
    assert Path(upload["path"]).is_file()
    assert captured["trigger"] == "import"


@pytest.mark.parametrize("filename", ['ООО "УНИСТРОЙ".xlsx', '..%2F..%2FООО %22УНИСТРОЙ%22.xlsx'])
def test_import_preserves_company_quotes_and_strips_decoded_path(isolated_api, monkeypatch, filename):
    client, data, _ = isolated_api
    captured = {}
    def submit(*args, **kwargs):
        captured.update(kwargs)
        return {"id": "test", "status": "queued"}
    monkeypatch.setattr(engine, "submit", submit)
    response = client.post("/api/import", files=[("files", (filename, _xlsx_bytes()))], data={"run_date": "2026-09-18"})
    assert response.status_code == 202
    upload = captured["uploads"][0]
    assert upload["name"] == 'ООО "УНИСТРОЙ".xlsx'
    assert "%22" not in upload["name"]
    assert Path(upload["name"]).name == upload["name"]
    assert Path(upload["path"]).parent == data


def test_import_rejects_decoded_control_character(isolated_api, monkeypatch):
    client, data, _ = isolated_api
    def reject_submit(*args, **kwargs):
        pytest.fail("Control characters must be rejected before enqueueing")
    monkeypatch.setattr(engine, "submit", reject_submit)
    response = client.post("/api/import", files=[("files", ("company%00.xlsx", _xlsx_bytes()))])
    assert response.status_code == 400
    assert list(data.glob("*.xlsx")) == []


def test_failed_submit_cleans_temporary_upload(isolated_api, monkeypatch):
    client, data, _ = isolated_api
    def submit(*args, **kwargs):
        raise ValueError("Запуск уже выполняется")
    monkeypatch.setattr(engine, "submit", submit)
    response = client.post("/api/import", files=[("files", ("client.xlsx", _xlsx_bytes()))], data={"run_date": "2026-09-18"})
    assert response.status_code == 400
    assert list(data.glob("*.xlsx")) == []


def test_duplicate_upload_names_rejected_and_temporary_files_cleaned(isolated_api, monkeypatch):
    client, data, _ = isolated_api
    def reject_submit(*args, **kwargs):
        pytest.fail("Duplicate upload must be rejected before enqueueing")
    monkeypatch.setattr(engine, "submit", reject_submit)
    response = client.post("/api/import", files=[("files", ("same.xlsx", _xlsx_bytes())), ("files", ("same.xlsx", _xlsx_bytes()))])
    assert response.status_code == 400
    assert list(data.glob("*.xlsx")) == []


def test_spoofed_non_xlsx_is_rejected_without_enqueueing(isolated_api, monkeypatch):
    client, data, _ = isolated_api
    def reject_submit(*args, **kwargs):
        pytest.fail("Bad upload must be rejected before enqueueing")
    monkeypatch.setattr(engine, "submit", reject_submit)
    response = client.post("/api/import", files=[("files", ("bad.xlsx", b"not a ZIP workbook"))])
    assert response.status_code == 400
    assert list(data.glob("*.xlsx")) == []
