from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json

import pytest

from operator_app import config, engine, seo, storage


def conf():
    return config.parse_operator((config.ROOT / "operators/rusplast-seo.md").read_text())


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for module in (config, storage, engine):
        monkeypatch.setattr(module, "DATA", tmp_path / "data")
    storage.init()
    return tmp_path


def content():
    article = {
        "slug": "zakaz-gofry-dlya-snabzheniya", "title": "Как подготовить заказ на гофротрубу для снабжения",
        "seoTitle": "Заказ гофротрубы: что указать снабженцу", "description": "Проверяем материал, исполнение, цвет, диаметр и длину бухты перед заказом гофротрубы. Пример заявки и сверки с каталогом производителя.",
        "category": "Закупки", "image": "pipe-gray", "imageAlt": "Серая гофротруба ПВХ с чёткими рёбрами",
        "intro": "Заявка на закупку гофротрубы должна однозначно определять нужное изделие. Для этого удобно сверять артикул с карточкой производителя и отдельно согласовывать количество.",
        "takeaway": "Сверьте артикул и размеры в карточке, затем согласуйте количество и единицу поставки.",
        "publishedAt": "2026-09-23", "author": "Редакция РУСПЛАСТЗАВОДА",
        "sections": [{"id": f"step-{n}", "title": f"Этап заявки {n}", "paragraphs": ["У позиции 2021022 наружный диаметр 16 мм. " + "При согласовании заявки разделяйте данные изделия и пожелания к партии. Уточните единицы измерения и проверьте, что обе стороны используют одинаковое обозначение материала и исполнения. " * 4], "list": [], "links": [{"label": "Каталог труб", "href": "/catalog"}, {"label": "Трубы ПВХ", "href": "/catalog/pvh"}, {"label": "Трубы ПНД", "href": "/catalog/pnd"}], "table": None} for n in range(5)],
        "sources": [{"label": "Каталог ПВХ", "href": "https://rusplast-zavod.ru/catalog/pvh"}], "related": ["legkaya-i-tyazhelaya-gofra", "diametr-gofrotruby-16-20-25-32"],
    }
    evidence = {"claim": "У позиции 2021022 наружный диаметр 16 мм.", "sourceUrl": "https://rusplast-zavod.ru/catalog/pvh", "sourceExcerpt": '"outerDiameter": 16'}
    draft = {"article": article, "primaryKeyword": "заявка на гофротрубу", "searchIntent": "Составить точный заказ", "uniqueValue": "Последовательность сверки заказа", "evidence": [evidence, deepcopy(evidence)]}
    context = {"articles": [{"slug": "legkaya-i-tyazhelaya-gofra", "title": "Лёгкая и тяжёлая гофра: отличия"}, {"slug": "diametr-gofrotruby-16-20-25-32", "title": "Диаметры гофротрубы"}], "approvedImages": [{"id": "pipe-gray"}],
               "sources": [{"url": "https://rusplast-zavod.ru/catalog/pvh", "text": '{"sku": "2021022", "outerDiameter": 16}'}], "history": []}
    return draft, context


def test_four_day_calendar_crosses_month_and_year():
    c = conf()
    c["enabled"] = True
    assert config.next_run(c, datetime(2026, 9, 23, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))) == "2026-09-27T12:00:00+03:00"
    assert config.next_run(c, datetime(2026, 9, 30, 23, 0, tzinfo=ZoneInfo("Europe/Moscow"))) == "2026-10-01T12:00:00+03:00"
    assert config.seo_slot(c, date(2026, 10, 3)) == date(2026, 10, 1)
    with pytest.raises(ValueError):
        config.seo_slot(c, date(2026, 9, 22))


def test_invalid_interval_and_missing_generator_rejected():
    markdown = (config.ROOT / "operators/rusplast-seo.md").read_text()
    for bad in (markdown.replace("every_days: 4", "every_days: 1"), markdown.replace("codex_path: /", "codex_path: relative/")):
        with pytest.raises(ValueError):
            config.parse_operator(bad)


def test_scheduler_catches_current_edition_only(isolated, monkeypatch):
    c = conf()
    c["enabled"] = True
    monkeypatch.setattr(engine, "load_operators", lambda: [c])
    storage.set_setting("enabled_since:rusplast-seo", "2026-09-23T09:00:00+03:00")
    calls = []
    monkeypatch.setattr(engine, "submit", lambda *args, **kwargs: calls.append((args, kwargs)))
    engine.tick(datetime(2026, 10, 9, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")))
    assert calls == [(("rusplast-seo", "2026-10-09"), {"trigger": "schedule"})]


def test_sleep_does_not_compress_publication_interval(isolated, monkeypatch):
    c = conf()
    c["enabled"] = True
    seo._save(c["id"], "2026-09-23", {"status": "published", "published_at": "2026-09-26T10:00:00+03:00"})
    assert seo.next_due(c).isoformat() == "2026-09-30T12:00:00+03:00"
    monkeypatch.setattr(engine, "load_operators", lambda: [c])
    storage.set_setting("enabled_since:rusplast-seo", "2026-09-23T09:00:00+03:00")
    monkeypatch.setattr(engine, "submit", lambda *args, **kwargs: pytest.fail("Must not create a second article one day after a delayed publication"))
    engine.tick(datetime(2026, 9, 27, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")))


def test_public_fetch_retries_transient_failure_and_rejects_redirect(monkeypatch):
    attempts = []
    class Response:
        status = 200
        url = "https://rusplast-zavod.ru/catalog"
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): return b"catalog"
    def fetch(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1: raise TimeoutError()
        return Response()
    monkeypatch.setattr(seo, "urlopen", fetch)
    monkeypatch.setattr(seo.clock, "sleep", lambda seconds: None)
    assert seo._public_html("https://rusplast-zavod.ru/catalog") == "catalog"
    assert len(attempts) == 2
    Response.url = "https://unexpected.example/catalog"
    with pytest.raises(ValueError, match="трёх попыток"):
        seo._public_html("https://rusplast-zavod.ru/catalog")


@pytest.mark.parametrize("mutation,expected", [
    (lambda d, c: d["article"].update(image="invented-ai-image"), "Изображение не утверждено"),
    (lambda d, c: d["evidence"][0].update(sourceExcerpt="750 Н invented"), "Цитата источника не найдена"),
    (lambda d, c: d["article"]["sources"][0].update(href="https://unverified.example"), "Источник отсутствует"),
    (lambda d, c: d["article"].update(slug=c["articles"][0]["slug"]), "Такой slug"),
    (lambda d, c: c["history"].append({"primaryKeyword": d["primaryKeyword"]}), "Основной запрос"),
    (lambda d, c: d["article"]["sections"][0]["links"][0].update(href="javascript:alert(1)"), "Недопустимая внутренняя ссылка"),
])
def test_editorial_guards(mutation, expected):
    draft, context = content()
    assert seo.validate_draft(draft, context, "2026-09-23") == []
    mutation(draft, context)
    assert any(expected in error for error in seo.validate_draft(draft, context, "2026-09-23"))


def test_prepared_edition_is_reused_after_restart_without_generation(isolated, monkeypatch):
    draft, context = content()
    review = {"approved": True, "sourceGrounded": True, "distinctIntent": True, "usefulForBuyer": True, "issues": []}
    seo._save("rusplast-seo", "2026-09-23", {"status": "prepared", "draft": draft, "context": context, "review": review})
    monkeypatch.setattr(seo, "_generate", lambda *a: pytest.fail("A retry must not create another article"))
    directory = isolated / "retry"
    directory.mkdir()
    sources = seo.prepare_article(conf(), {"run_date": "2026-09-23"}, directory, lambda event: None)
    assert json.loads(Path(sources[0]["path"]).read_text()) == draft["article"]
    assert seo._get("rusplast-seo", "2026-09-23")["status"] == "prepared"


def test_wrong_publication_receipt_cannot_mark_success(isolated, monkeypatch):
    draft, context = content()
    seo._save("rusplast-seo", "2026-09-23", {"status": "prepared", "draft": draft, "context": context, "review": {}})
    path = isolated / "article.json"
    path.write_text(json.dumps(draft["article"]))
    monkeypatch.setattr(seo, "_verify_links", lambda *args: [])
    monkeypatch.setattr(seo, "_run_json", lambda *args: {"url": "https://rusplast-zavod.ru/blog/" + draft["article"]["slug"], "slug": draft["article"]["slug"], "sha256": "0" * 64, "release": "fake"})
    with pytest.raises(ValueError, match="Publisher не подтвердил"):
        seo.publish_article(conf(), {"run_date": "2026-09-23"}, [{"path": str(path)}], isolated, lambda event: None)
    assert seo._get("rusplast-seo", "2026-09-23")["status"] == "prepared"


def test_unapproved_review_never_saves_publishable_edition(isolated, monkeypatch):
    draft, context = content()
    draft["article"]["publishedAt"] = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    monkeypatch.setattr(seo, "collect_context", lambda c: context)
    responses = iter([draft, {"approved": False, "sourceGrounded": False, "distinctIntent": True, "usefulForBuyer": True, "issues": ["Недоказанный факт"]}])
    monkeypatch.setattr(seo, "_generate", lambda *args: next(responses))
    with pytest.raises(ValueError, match="Недоказанный факт"):
        seo.prepare_article(conf(), {"run_date": "2026-09-23"}, isolated, lambda event: None)
    assert seo._get("rusplast-seo", "2026-09-23") is None
