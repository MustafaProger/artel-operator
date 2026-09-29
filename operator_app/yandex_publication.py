"""Yandex note updates keyed by employee identity, preserving unrelated sections."""
from pathlib import Path
import re

from .ordering import alphabet_key
from .yandex_reports import safe_label
from .publication import save_merged_sections

MARKER = re.compile(r"<!-- yandex-employee:([a-z0-9-]+) phone:([a-f0-9]{64})(?: orders:([a-f0-9]{64}))? -->")


def sections(text):
    # Examples inside Markdown fences belong to the user, even if they contain
    # a complete report with our identity marker. Keep original match offsets.
    fences = []
    position, opening_at, fence = 0, None, None
    for line in text.splitlines(keepends=True):
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*(?:\r?\n)?", line):
                fences.append((opening_at, position + len(line)))
                fence = None
        else:
            opening = re.match(r" {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|$)", line)
            if opening and not (opening[1][0] == "`" and "`" in opening[2]):
                fence, opening_at = opening[1], position
        position += len(line)
    if fence is not None:
        raise ValueError("В заметке Яндекса незакрытый блок кода; автоматическое обновление остановлено")
    headings = [heading for heading in re.finditer(r"(?m)^#{1,6} +[^\n]+(?:\n|$)", text)
                if not any(start <= heading.start() < end for start, end in fences)]
    found = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        block = text[heading.start():end]
        if not block.startswith("### ") or "\nЯндекс заправки\nна склад (" not in block:
            continue
        marker = MARKER.search(block)
        name = re.search(r"(?m)^на склад \((.*)\) .* л на сумму закупки ", block)
        if not name:
            raise ValueError("Не удалось проверить раздел Яндекса в заметке")
        found.append({"id": marker[1] if marker else None, "phone": marker[2] if marker else None,
                      "orders": marker[3] if marker else None, "name": name[1], "block": block.rstrip(), "start": heading.start(), "end": end})
    return found


def merge_yandex_sections(existing, incoming):
    old, new = sections(existing), sections(incoming)
    if not new:
        raise ValueError("Нет проверенных разделов Яндекса для публикации")
    # Pre-identity notes can be migrated only when their caption is identical.
    for item in new:
        candidates = [o for o in old if o["phone"] == item["phone"] and
                      (str(o["id"]).startswith("phone-") or item["id"].startswith("phone-"))]
        for previous in candidates:
            # A phone alone cannot connect an offline XLSX to a cabinet user:
            # require the exact same order IDs, even when the names coincide.
            if previous["id"] != item["id"] and (not item["orders"] or previous["orders"] != item["orders"]):
                raise ValueError("Нельзя связать импорт с ID сотрудника только по телефону; нужны совпадающие заказы или полная выгрузка")
            if (previous["id"] == item["id"] and item["id"].startswith("phone-")
                    and previous["name"] != item["name"] and previous["orders"] != item["orders"]):
                raise ValueError("Телефон в импорте связан с другим именем и заказами; автоматическое объединение запрещено")
        matches = [o for o in old if o["id"] == item["id"] or o in candidates]
        legacy = [o for o in old if o["id"] is None and o["name"] == item["name"]]
        if legacy:
            comparable = MARKER.sub("", item["block"]).replace("\n\n", "\n")
            if len(legacy) != 1 or legacy[0]["block"].replace("\n\n", "\n") != comparable:
                raise ValueError("Старый раздел Яндекса без ID неоднозначен; заметка сохранена без изменений")
            matches += legacy
        if len(matches) > 1:
            raise ValueError("Повтор или неоднозначная идентичность сотрудника в заметке Яндекса")
        if matches:
            if item["id"].startswith("phone-") and matches[0]["id"] and not matches[0]["id"].startswith("phone-"):
                item = dict(item, id=matches[0]["id"], block=item["block"].replace(
                    "yandex-employee:" + item["id"], "yandex-employee:" + matches[0]["id"]))
            old.remove(matches[0])
        old.append(item)
    # Remove only managed blocks; arbitrary text and other heading levels survive.
    remainder = existing
    for item in reversed(sections(existing)):
        remainder = remainder[:item["start"]] + remainder[item["end"]:]
    title = incoming.splitlines()[0]
    if not remainder.strip():
        remainder = title + "\n\n"
    elif title not in remainder.splitlines():
        remainder = title + "\n\n" + remainder
    result = []
    for item in sorted(old, key=lambda x: (alphabet_key(x["name"]), x["id"] or "")):
        label = safe_label(item["name"])
        if sum(safe_label(o["name"]).casefold() == label.casefold() for o in old) > 1:
            if item["id"] is None:
                raise ValueError("Одноимённый старый раздел Яндекса не имеет идентификатора")
            label += f" [{item['id']}]"
        result.append("### " + label + "\n" + item["block"].split("\n", 1)[1])
    return remainder.rstrip() + "\n\n" + "\n\n".join(result) + "\n"


def save_yandex_sections(destination: Path, incoming: str):
    save_merged_sections(destination, incoming, merge_yandex_sections)
