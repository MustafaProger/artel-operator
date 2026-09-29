"""Replace only addressed company sections, preserving the rest of a daily note."""
import os
import re
import tempfile
from pathlib import Path
from threading import Lock
from typing import Callable

from .calculator import company_identity
from .ordering import alphabet_key

# The supported runtime has one process. Serialize its note read/merge/replace
# operations; unique staging names also avoid touching a stale temporary file.
PUBLICATION_LOCK = Lock()


def sort_report_sections(text):
    sections = _sections(text)
    if not sections:
        return text
    result, position, first = [], 0, 0
    for index, (_, _, end) in enumerate(sections):
        if index + 1 < len(sections) and end == sections[index + 1][1]:
            continue
        # Sort only adjacent company sections. Higher-level headings and their
        # manual text keep their position instead of moving with a company.
        result.append(text[position:sections[first][1]])
        blocks = [text[start:stop] for _, start, stop in sections[first:index + 1]]
        blocks.sort(key=lambda block: alphabet_key(block.splitlines()[0].lstrip("# ")))
        result.extend(block if block.endswith("\n\n") else block + ("\n" if block.endswith("\n") else "\n\n") for block in blocks)
        position, first = end, index + 1
    result.append(text[position:])
    return "".join(result)


def _sections(text):
    # A company ends at the next peer or parent heading; deeper headings remain
    # part of its report and are replaced along with the company's content.
    headings = []
    position, fence = 0, None
    for line in text.splitlines(keepends=True):
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*(?:\r?\n)?", line):
                fence = None
        else:
            opening = re.match(r" {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r?\n|$)", line)
            if opening and not (opening[1][0] == "`" and "`" in opening[2]):
                fence = opening[1]
            else:
                heading = re.match(r" {0,3}(#{1,3})[ \t]+([^\r\n]+)(?:\r?\n|$)", line)
                if heading:
                    headings.append((heading[1], heading[2], position))
        position += len(line)
    if fence is not None:
        raise ValueError("Незакрытый блок кода в заметке; закройте его перед публикацией отчёта.")
    return [(company_identity(name), start, headings[index + 1][2] if index + 1 < len(headings) else len(text))
            for index, (level, name, start) in enumerate(headings) if level == "###"]


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


def save_merged_sections(destination: Path, incoming: str, merge: Callable[[str, str], str]) -> None:
    """Serialize a local note merge and replace it using a private staging file."""
    with PUBLICATION_LOCK:
        existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
        updated = merge(existing, incoming)
        if updated == existing:
            return
        descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.write_text(updated, encoding="utf-8")
            # Preserve an existing note's mode. New financial notes remain 0600.
            if destination.exists():
                temporary.chmod(destination.stat().st_mode & 0o777)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)


def save_report_sections(destination: Path, report: str) -> None:
    save_merged_sections(destination, report, update_report_sections)
