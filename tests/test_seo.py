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
        "category": "Закупки", "image": "generated-cover", "imageAlt": "Серая гофротруба ПВХ с чёткими рёбрами",
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
    (lambda d, c: d["article"].update(image="invented-ai-image"), "новая сгенерированная обложка"),
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
    seo._save("rusplast-seo", "2026-09-23", {"status": "prepared", "draft": draft, "context": context, "review": review, "image": {"path": str(isolated / "cover.png"), "sha256": "1" * 64}})
    monkeypatch.setattr(seo, "inspect_cover", lambda path: {"sha256": "1" * 64})
    monkeypatch.setattr(seo, "_run_json", lambda *args: {"valid": True, "slug": draft["article"]["slug"]})
    monkeypatch.setattr(seo, "_generate", lambda *a: pytest.fail("A retry must not create another article"))
    directory = isolated / "retry"
    directory.mkdir()
    sources = seo.prepare_article(conf(), {"run_date": "2026-09-23"}, directory, lambda event: None)
    assert json.loads(Path(sources[0]["path"]).read_text()) == draft["article"]
    assert seo._get("rusplast-seo", "2026-09-23")["status"] == "prepared"


def test_wrong_publication_receipt_cannot_mark_success(isolated, monkeypatch):
    draft, context = content()
    seo._save("rusplast-seo", "2026-09-23", {"status": "prepared", "draft": draft, "context": context, "review": {}, "image": {"path": str(isolated / "cover.png"), "sha256": "1" * 64}})
    monkeypatch.setattr(seo, "inspect_cover", lambda path: {"sha256": "1" * 64})
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


def test_cover_failure_preserves_reviewed_draft_for_retry(isolated, monkeypatch):
    draft, context = content()
    draft["article"]["publishedAt"] = datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()
    review = {"approved": True, "sourceGrounded": True, "distinctIntent": True, "usefulForBuyer": True, "issues": []}
    monkeypatch.setattr(seo, "collect_context", lambda c: context)
    responses = iter([draft, review])
    monkeypatch.setattr(seo, "_generate", lambda *args: next(responses))
    def image_failure(*args):
        raise ValueError("Генерация изображения временно недоступна")
    monkeypatch.setattr(seo, "_generate_cover", image_failure)
    with pytest.raises(ValueError, match="изображения временно"):
        seo.prepare_article(conf(), {"run_date": "2026-09-23"}, isolated, lambda event: None)
    edition = seo._get("rusplast-seo", "2026-09-23")
    assert edition["status"] == "reviewed"
    assert edition["draft"]["article"]["slug"] == draft["article"]["slug"]
    # Retry reuses reviewed prose. It must never regenerate the topic or publish stock imagery.
    monkeypatch.setattr(seo, "_generate", lambda *args: pytest.fail("Do not regenerate reviewed prose"))
    with pytest.raises(ValueError, match="изображения временно"):
        seo.prepare_article(conf(), {"run_date": "2026-09-23"}, isolated, lambda event: None)


def test_cover_rejects_missing_or_unrelated_file(isolated, monkeypatch):
    draft, _ = content()
    monkeypatch.setattr(seo, "_generate", lambda *args, **kwargs: {"imagePath": str(isolated / "unrelated.png")})
    with pytest.raises(ValueError, match="новую обложку"):
        seo._generate_cover(conf(), isolated, draft["article"], lambda event: None)
    with pytest.raises(ValueError, match="отсутствует"):
        seo.inspect_cover(isolated / "missing.png")


def test_context_refuses_legacy_static_editorial_data(isolated, monkeypatch):
    monkeypatch.setattr(seo, "_run_json", lambda *args: {"articles": [], "products": [], "approvedImages": ["pipe-gray"]})
    with pytest.raises(ValueError, match="Strapi CMS"):
        seo.collect_context(conf())


@pytest.mark.parametrize("href", ["/contacts", "/documents", "/product/fake-product", "/blog/missing-article"])
def test_editorial_rejects_nonexistent_routes(href):
    draft, context = content()
    draft["article"]["sections"][0]["links"][0]["href"] = href
    assert any("Недопустимая внутренняя ссылка" in error for error in seo.validate_draft(draft, context, "2026-09-23"))


@pytest.mark.parametrize("href", ["/#contacts", "/#certificates", "/#about", "/#delivery", "/#request", "/catalog/aksessuary"])
def test_editorial_accepts_actual_home_anchors_and_accessories(href):
    draft, context = content()
    draft["article"]["sections"][0]["links"][0]["href"] = href
    assert seo.validate_draft(draft, context, "2026-09-23") == []


def existing_publication_fixture(monkeypatch):
    from html import escape
    import hashlib
    draft, context = content()
    draft['article']['image'] = 'pipe-gray'  # Pre-CMS published edition has no generated PNG.
    edition = {'status': 'published', 'published_at': '2026-09-23T09:16:19+00:00',
               'draft': draft, 'context': context, 'review': {}}
    original = draft['article']
    normalized = {**original, 'image': 'https://cms.rusplast-zavod.ru/uploads/legacy_cover.webp'}
    entry = {key: value for key, value in original.items() if key not in ('image', 'publishedAt')}
    entry.update(documentId='cms-doc-1', publishedOn=original['publishedAt'],
                 image={'id': 49, 'url': '/uploads/legacy_cover.webp'})
    manifest = {'release': 'cms-test', 'articles': {original['slug']: {
        'sha256': hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}}}
    public = {'entry': entry, 'manifest': manifest,
              'html': '<h1>' + escape(original['title']) + '</h1><img src="' + normalized['image'] + '">',
              'sitemap': '<loc>https://rusplast-zavod.ru/blog/' + original['slug'] + '</loc>'}
    monkeypatch.setattr(seo, '_public_json', lambda url: {'data': [public['entry']]} if '/api/articles?' in url else public['manifest'])
    monkeypatch.setattr(seo, '_public_html', lambda url: public['sitemap'] if url.endswith('/sitemap.xml') else public['html'])
    monkeypatch.setattr(seo, '_generate', lambda *a, **k: pytest.fail('Published retry must not generate'))
    monkeypatch.setattr(seo, '_run_json', lambda *a, **k: pytest.fail('Published retry must not execute publisher'))
    return edition, public, normalized


def test_legacy_published_retry_checks_cms_and_same_cover_without_writing_edition(isolated, monkeypatch):
    edition, public, normalized = existing_publication_fixture(monkeypatch)
    seo._save('rusplast-seo', '2026-09-23', edition)
    before = deepcopy(edition)
    monkeypatch.setattr(seo, '_save', lambda *args: pytest.fail('Published edition/history must remain unchanged'))
    source = seo.prepare_article(conf(), {'run_date': '2026-09-23'}, isolated, lambda e: None)
    result = seo.publish_article(conf(), {'run_date': '2026-09-23'}, source, isolated, lambda e: None)
    assert result['status'] == 'completed'
    assert result['metrics']['verified_existing'] is True
    assert result['audit']['publication']['article'] == normalized
    assert result['audit']['publication']['imageId'] == 49
    assert seo._get('rusplast-seo', '2026-09-23') == before


def test_legacy_retry_preserves_manual_prose_edit(isolated, monkeypatch):
    edition, public, normalized = existing_publication_fixture(monkeypatch)
    public['entry']['intro'] = 'Изменено редактором после автоматической публикации.'
    with pytest.raises(ValueError, match='Статья изменена в CMS'):
        seo._verify_existing_publication(conf(), edition)


def test_new_published_retry_preserves_manual_cover_change(isolated, monkeypatch):
    edition, public, normalized = existing_publication_fixture(monkeypatch)
    edition['publication'] = {'article': normalized}
    public['entry']['image']['url'] = '/uploads/replacement.png'
    with pytest.raises(ValueError, match='Статья изменена в CMS'):
        seo._verify_existing_publication(conf(), edition)


def test_published_retry_requires_matching_manifest_and_actual_img_tag(isolated, monkeypatch):
    edition, public, normalized = existing_publication_fixture(monkeypatch)
    public['html'] = '<h1>' + normalized['title'] + '</h1><script>"' + normalized['image'] + '"</script><img src="wrong.png">'
    with pytest.raises(ValueError, match='заголовок и обложку'):
        seo._verify_existing_publication(conf(), edition)
    public['manifest']['articles'][normalized['slug']]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='manifest'):
        seo._verify_existing_publication(conf(), edition)


def test_missing_published_cms_entry_never_triggers_generation(isolated, monkeypatch):
    edition, _, _ = existing_publication_fixture(monkeypatch)
    monkeypatch.setattr(seo, '_public_json', lambda url: {'data': []})
    with pytest.raises(ValueError, match='не найдена'):
        seo._verify_existing_publication(conf(), edition)


def test_public_json_does_not_decode_html_entities(monkeypatch):
    url = 'https://cms.rusplast-zavod.ru/api/articles'
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): return b'{"literal": "&quot;text&quot; &amp;"}'
    Response.url = url
    monkeypatch.setattr(seo, 'urlopen', lambda *args, **kwargs: Response())
    assert seo._public_json(url)['literal'] == '&quot;text&quot; &amp;'


def test_text_review_knows_image_is_generated_only_after_its_approval(isolated, monkeypatch):
    draft, context = content()
    draft['article']['publishedAt'] = datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    context['imagePolicy'] = 'A generated image must be uploaded to CMS before publication.'
    monkeypatch.setattr(seo, 'collect_context', lambda conf: context)
    stages = []
    def generate(conf, directory, prompt, schema, name):
        stages.append(name)
        if name == 'draft':
            return draft
        assert name == 'review'
        assert 'стадия ДО генерации изображения' in prompt
        assert 'служебный маркер generated-cover' in prompt
        assert 'Файл изображения и URL Strapi сейчас ещё не должны существовать' in prompt
        assert 'проверь только imageAlt' in prompt
        return {'approved': True, 'sourceGrounded': True, 'distinctIntent': True, 'usefulForBuyer': True, 'issues': []}
    def cover(*args):
        stages.append('image')
        raise ValueError('Тест завершён на отдельном этапе изображения')
    monkeypatch.setattr(seo, '_generate', generate)
    monkeypatch.setattr(seo, '_generate_cover', cover)
    with pytest.raises(ValueError, match='отдельном этапе изображения'):
        seo.prepare_article(conf(), {'run_date': '2026-09-23'}, isolated, lambda e: None)
    assert stages == ['draft', 'review', 'image']
    assert seo._get('rusplast-seo', '2026-09-23')['status'] == 'reviewed'



def test_cms_runtime_manifest_without_static_release_can_verify_existing_article(isolated, monkeypatch):
    edition, public, normalized = existing_publication_fixture(monkeypatch)
    public['manifest'].pop('release')
    result = seo._verify_existing_publication(conf(), edition)
    assert result['release'] == 'verified-live'
    assert result['article'] == normalized
