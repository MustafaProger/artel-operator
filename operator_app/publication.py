"""Replace only addressed company sections, preserving the rest of a daily note."""
import re
from pathlib import Path

from .calculator import company_identity
from .ordering import alphabet_key


def sort_report_sections(text):
    sections = _sections(text)
    if not sections:
        return text
    prefix = text[:sections[0][1]]
    blocks = [text[start:end] for _, start, end in sections]
    blocks.sort(key=lambda block: alphabet_key(block.splitlines()[0].lstrip("# ")))
    return prefix + "".join(block if block.endswith("\n\n") else block + ("\n" if block.endswith("\n") else "\n\n") for block in blocks)


def _sections(text):
    headings = list(re.finditer(r"(?m)^###[ \t]+([^\r\n]+)\r?\n", text))
    return [(company_identity(match[1]), match.start(), headings[index + 1].start() if index + 1 < len(headings) else len(text))
            for index, match in enumerate(headings)]


def update_report_sections(existing: str, incoming: str) -> str:
    replacements = {key: incoming[start:end].rstrip() for key, start, end in _sections(incoming)}
    if not existing:
        return sort_report_sections(incoming)
    blocks, used, position = [], set(), 0
    for key, start, end in _sections(existing):
        blocks.append(existing[position:start])
        if key in replacements:
            if key not in used:
                old = existing[start:end]
                blocks.append(replacements[key] + (old[len(old.rstrip()):] or "\n"))
                used.add(key)
        else:
            blocks.append(existing[start:end])
        position = end
    blocks.append(existing[position:])
    result = "".join(blocks)
    for key, block in replacements.items():
        if key not in used:
            result += ("" if result.endswith("\n\n") else "\n" if result.endswith("\n") else "\n\n") + block + "\n"
    if (not re.match(r"^\d{2}\.\d{2}\.\d{4}(?:\s|$)", result)
            and result.splitlines()[0] != incoming.splitlines()[0]):
        result = incoming.splitlines()[0] + "\n\n" + result
    return sort_report_sections(result)


def save_report_sections(destination: Path, report: str) -> None:
    existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
    updated = update_report_sections(existing, report)
    if updated != existing:
        temporary = destination.with_suffix(".md.tmp")
        try:
            temporary.write_text(updated, encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
