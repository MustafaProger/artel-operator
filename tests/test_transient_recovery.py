"""Transient outages recover without repeating writes or weakening validation."""
from unittest.mock import Mock

import pytest
from playwright.sync_api import TimeoutError, Error

from operator_app import seo, yandex_cabinet as yc
from test_seo import isolated, conf, content


def cabinet(monkeypatch, outcomes):
    monkeypatch.setattr(yc.clock, "sleep", lambda _: None)
    context = Mock()
    context.request.fetch.side_effect = outcomes
    initial = Mock(status=200)
    initial.json.return_value = {"id": "c" * 32, "name": "Fixture"}
    initial.request.all_headers.return_value = {"x-csrf-token": "fixture"}
    return yc.Cabinet(context, initial, "Fixture"), context.request.fetch


def response(status=200, data=None):
    result = Mock(status=status)
    result.json.return_value = data if data is not None else {"reports": []}
    return result


@pytest.mark.parametrize("path", [yc.ORDERS, yc.REPORTS + "history", yc.REPORTS + "status", yc.REPORTS + "tab-columns"])
@pytest.mark.parametrize("failure", [TimeoutError("secret"), Error("net::ERR_CONNECTION_RESET secret"), 429, 503])
def test_reads_recover_and_release_responses(monkeypatch, path, failure):
    failed = response(failure) if isinstance(failure, int) else failure
    success = response()
    api, fetch = cabinet(monkeypatch, [failed, success])
    assert api.request(path, {"task_id": "a" * 32}) == {"reports": []}
    assert fetch.call_count == 2
    assert fetch.call_args_list[0] == fetch.call_args_list[1]
    success.dispose.assert_called_once()
    if isinstance(failure, int):
        failed.dispose.assert_called_once()


@pytest.mark.parametrize("failure", [TimeoutError("secret"), 503])
def test_read_retries_are_bounded_and_do_not_invent_empty_data(monkeypatch, failure):
    failed = response(failure) if isinstance(failure, int) else failure
    api, fetch = cabinet(monkeypatch, [failed] * 3)
    with pytest.raises(yc.CabinetError) as error:
        api.request(yc.REPORTS + "status", {"task_id": "a" * 32})
    assert "secret" not in str(error.value)
    assert fetch.call_count == 3


@pytest.mark.parametrize("path", [yc.REPORTS + "generate", "/unknown-write"])
@pytest.mark.parametrize("failure", [TimeoutError("secret"), 503])
def test_write_never_retried(monkeypatch, path, failure):
    api, fetch = cabinet(monkeypatch, [response(failure) if isinstance(failure, int) else failure])
    with pytest.raises(yc.CabinetError):
        api.request(path, {"service": "tanker"})
    assert fetch.call_count == 1


@pytest.mark.parametrize("bad", [response(401), response(403), response(data={"error": "private"}), response(data=[1])])
def test_permanent_errors_not_retried(monkeypatch, bad):
    api, fetch = cabinet(monkeypatch, [bad])
    with pytest.raises(yc.CabinetError):
        api.request(yc.ORDERS, {})
    assert fetch.call_count == 1
    bad.dispose.assert_called_once()


def test_cover_empty_replies_recover_but_still_require_review(isolated, monkeypatch):
    home = isolated / "codex"
    generated = home / "generated_images/new.png"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"fixture")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(seo, "inspect_cover", lambda path: {"sha256": "new"})
    monkeypatch.setattr(seo, "_history", lambda _: [])
    approved = dict(approved=True, photorealistic=True, subjectAccurate=True, noFabricatedClaims=True, issues=[])
    generate = Mock(side_effect=[{"imagePath": ""}, {"imagePath": ""}, {"imagePath": str(generated)}, approved])
    monkeypatch.setattr(seo, "_generate", generate)
    events = []
    result = seo._generate_cover(conf(), isolated, content()[0]["article"], events.append)
    assert result["review"] == approved
    assert generate.call_count == 4
    assert [e["stage"] for e in events] == ["seo-image", "seo-image", "seo-image-review"]
    assert (isolated / "article-cover.png").read_bytes() == b"fixture"


def test_cover_empty_replies_exhaust_without_publication(isolated, monkeypatch):
    generate = Mock(return_value={"imagePath": ""})
    monkeypatch.setattr(seo, "_generate", generate)
    with pytest.raises(ValueError, match="после 3 попыток"):
        seo._generate_cover(conf(), isolated, content()[0]["article"], lambda _: None)
    assert generate.call_count == 3
    assert not (isolated / "article-cover.png").exists()
