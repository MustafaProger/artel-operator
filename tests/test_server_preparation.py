import asyncio
import hashlib
from pathlib import Path

import pytest

from operator_app import knowledge, main


def test_knowledge_snapshot_and_explicit_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_KNOWLEDGE_DIR", str(tmp_path))
    raw = "# Правила\nПисать понятно.".encode()
    (tmp_path / "rules.md").write_bytes(raw)
    (tmp_path / "private.md").write_text("not selected")
    assert knowledge.load_knowledge({}) == []
    assert knowledge.load_knowledge({"knowledge_files": ["rules.md"]}) == [
        {"file": "rules.md", "sha256": hashlib.sha256(raw).hexdigest(), "text": raw.decode()}]


@pytest.mark.parametrize("name", ["../outside.md", "/outside.md", "file.txt", "missing.md"])
def test_invalid_knowledge_stops_run(tmp_path, monkeypatch, name):
    monkeypatch.setenv("OPERATOR_KNOWLEDGE_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        knowledge.load_knowledge({"knowledge_files": [name]})


def test_knowledge_rejects_escape_and_size(tmp_path, monkeypatch):
    root = tmp_path / "notes"
    root.mkdir()
    monkeypatch.setenv("OPERATOR_KNOWLEDGE_DIR", str(root))
    (tmp_path / "outside.md").write_text("private")
    (root / "link.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ValueError):
        knowledge.load_knowledge({"knowledge_files": ["link.md"]})
    (root / "large.md").write_bytes(b"x" * (knowledge.MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError):
        knowledge.load_knowledge({"knowledge_files": ["large.md"]})


def test_disabled_scheduler_never_ticks(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_SCHEDULER_ENABLED", "false")
    monkeypatch.setattr(main.storage, "init", lambda: None)
    monkeypatch.setattr(main, "OPERATORS", tmp_path)
    async def forbidden():
        pytest.fail("Scheduler started during server preparation")
    monkeypatch.setattr(main, "scheduler", forbidden)
    async def run():
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
    asyncio.run(run())


def test_scheduler_default_and_invalid_flag(monkeypatch):
    monkeypatch.delenv("OPERATOR_SCHEDULER_ENABLED", raising=False)
    assert main.scheduler_enabled()
    monkeypatch.setenv("OPERATOR_SCHEDULER_ENABLED", "typo")
    with pytest.raises(ValueError):
        main.scheduler_enabled()


def test_article_prompt_and_archive_include_selected_notes(tmp_path, monkeypatch):
    import json
    from operator_app import config, seo
    conf = config.parse_operator((config.ROOT / "operators/rusplast-seo.md").read_text())
    conf["knowledge_files"] = ["rules.md"]
    monkeypatch.setenv("OPERATOR_KNOWLEDGE_DIR", str(tmp_path))
    (tmp_path / "rules.md").write_text("Уникальное редакционное правило", encoding="utf-8")
    monkeypatch.setattr(seo, "_get", lambda *args: None)
    monkeypatch.setattr(seo, "collect_context", lambda *args: {"sources": []})
    class StopBeforeGeneration(Exception):
        pass
    def capture(conf, directory, prompt, *args, **kwargs):
        assert "Уникальное редакционное правило" in prompt
        raise StopBeforeGeneration()
    monkeypatch.setattr(seo, "_generate", capture)
    with pytest.raises(StopBeforeGeneration):
        seo.prepare_article(conf, {"run_date": "2026-10-10"}, tmp_path, lambda event: None)
    saved = json.loads((tmp_path / "context.json").read_text())
    assert saved["knowledge"][0]["text"] == "Уникальное редакционное правило"
    assert len(saved["knowledge"][0]["sha256"]) == 64
