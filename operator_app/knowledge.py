"""Explicit, bounded Markdown inputs for the SEO writer and reviewer."""
import hashlib
import os
from pathlib import Path

from .config import ROOT

MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 300_000


def load_knowledge(conf):
    names = conf.get("knowledge_files", [])
    if not isinstance(names, list) or len(names) > 20:
        raise ValueError("SEO: knowledge_files — список не более 20 Markdown-файлов")
    root = Path(os.environ.get("OPERATOR_KNOWLEDGE_DIR", str(ROOT / "knowledge"))).resolve()
    result, total = [], 0
    for name in names:
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts or Path(name).suffix.lower() != ".md":
            raise ValueError("SEO: knowledge_files должен содержать относительные пути .md внутри knowledge")
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("SEO: заметка выходит за пределы каталога knowledge")
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("SEO: превышен размер заметок (100 КБ на файл, 300 КБ всего)")
            content = raw.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"SEO: не удалось прочитать Markdown-заметку {name}") from exc
        if not content.strip():
            raise ValueError(f"SEO: пустая Markdown-заметка {name}")
        result.append({"file": name, "sha256": hashlib.sha256(raw).hexdigest(), "text": content})
    return result
