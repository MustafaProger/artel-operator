import pytest

from operator_app.publication import sort_report_sections, update_report_sections


@pytest.mark.parametrize("heading", ["# Заметки", "## Заметки"])
def test_replacing_company_preserves_following_manual_section(heading):
    manual = heading + "\nРучной текст без изменений.  \n\n"
    existing = '22.09.2026\n\n### ООО «А»\nСтарый расчёт\n\n' + manual
    incoming = '22.09.2026\n\n### ООО «А»\nНовый расчёт\n'

    updated = update_report_sections(existing, incoming)

    assert updated == '22.09.2026\n\n### ООО «А»\nНовый расчёт\n\n' + manual
    assert update_report_sections(updated, incoming) == updated


def test_sorting_and_replacement_keep_manual_boundaries_and_other_companies():
    prefix = '22.09.2026\n\n## Основные фирмы\nРучное вступление.\n\n'
    other = '### ООО «Б»\nРасчёт другой фирмы.  \n\n'
    manual = '## Отдельные фирмы\nРучное пояснение.  \n\n'
    separate = '### ООО «А»\nОтдельный расчёт.\n\n'
    footer = '# Личные заметки\nНе переносить между разделами.\n'
    existing = prefix + '### ООО «Я»\nСтарый расчёт\n\n' + other + manual + separate + footer
    incoming = '22.09.2026\n\n### ООО «Я»\nНовый расчёт\n'

    updated = update_report_sections(existing, incoming)

    assert updated == prefix + other + '### ООО «Я»\nНовый расчёт\n\n' + manual + separate + footer
    assert sort_report_sections(updated) == updated
    assert update_report_sections(updated, incoming) == updated


def test_nested_heading_remains_part_of_replaced_company():
    existing = '22.09.2026\n\n### Китай\nСтарый расчёт\n\n#### Детали\nСтарые детали\n\n'
    incoming = '22.09.2026\n\n### Китай\nНовый расчёт\n\n#### Детали\nНовые детали\n'

    updated = update_report_sections(existing, incoming)

    assert updated == incoming + '\n'
    assert update_report_sections(updated, incoming) == updated
