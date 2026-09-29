"""Publication fault injection: temporary files/DB only; no portal or real vault."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from random import Random
from threading import Barrier

import pytest

from operator_app import engine, publication, storage
from test_engine import isolated_engine, source_file
from test_yandex import source as yandex_source, yandex_config


def report(company, value):
    return f'18.09.2026\n\n### {company}\nРасчёт {value}\n'


@pytest.mark.parametrize('kind', ['glopro', 'yandex'])
@pytest.mark.parametrize('failure', ['outputs', 'audit', 'zip', 'zip_write', 'zip_close', 'inventory', 'checkpoint'])
def test_failed_finalization_preserves_previous_note(isolated_engine, tmp_path, monkeypatch, kind, failure):
    data, operators = isolated_engine
    output = tmp_path / 'notes'
    output.mkdir()
    if kind == 'glopro':
        path = operators / 'glopro.md'
        path.write_text(path.read_text().replace('rules: {}', f"rules: {{}}\nobsidian_output: '{output}'"))
        date, title = '2026-09-18', 'Активация — 18.09.2026.md'
        upload = source_file(tmp_path / 'upload.xlsx')
    else:
        yandex_config(operators, output)
        monkeypatch.setitem(engine.HANDLERS, 'yandex', lambda *args: pytest.fail('network'))
        date, title = '2026-09-22', 'Яндекс Заправки — 22.09.2026.md'
        upload = yandex_source(tmp_path / 'upload.xlsx')
    note = output / title
    original = '# Ручные заметки\nСохранить предыдущий успешный результат.  \n'
    note.write_text(original)
    injected = []
    if failure == 'outputs':
        def fail_outputs(*args, **kwargs):
            injected.append(failure)
            raise OSError('simulated generated report failure')
        if kind == 'glopro':
            monkeypatch.setattr(engine, '_summary_xlsx', fail_outputs)
        else:
            from operator_app import yandex_reports
            monkeypatch.setattr(yandex_reports, 'write_outputs', fail_outputs)
    elif failure == 'audit':
        write = Path.write_text
        def fail_audit(path, *args, **kwargs):
            if path.name == 'Проверка расчётов.json':
                injected.append(failure)
                raise OSError('simulated audit write failure')
            return write(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'write_text', fail_audit)
    elif failure == 'zip':
        def fail_zip(*args, **kwargs):
            injected.append(failure)
            raise OSError('simulated archive failure')
        monkeypatch.setattr(engine.zipfile, 'ZipFile', fail_zip)
    elif failure in {'zip_write', 'zip_close'}:
        method = 'write' if failure == 'zip_write' else 'close'
        original_zip_method = getattr(engine.zipfile.ZipFile, method)
        def fail_zip_step(bundle, *args, **kwargs):
            target = str(bundle.filename).endswith('.zip') and bundle.fp is not None
            if method == 'close':
                result = original_zip_method(bundle, *args, **kwargs)
                if target:
                    injected.append(failure)
                    raise OSError('simulated archive close failure')
                return result
            if target:
                injected.append(failure)
                raise OSError('simulated archive member write failure')
            return original_zip_method(bundle, *args, **kwargs)
        monkeypatch.setattr(engine.zipfile.ZipFile, method, fail_zip_step)
    elif failure == 'checkpoint':
        save_run = storage.save_run
        def fail_checkpoint(record):
            if record['status'] == 'running' and record.get('files'):
                injected.append(failure)
                raise OSError('simulated durable checkpoint failure')
            return save_run(record)
        monkeypatch.setattr(storage, 'save_run', fail_checkpoint)
    else:
        def fail_inventory(*args, **kwargs):
            injected.append(failure)
            raise OSError('simulated metadata failure')
        monkeypatch.setattr(engine, '_file', fail_inventory)
    run = engine.submit(kind, date, 'import', [{'path': str(upload), 'name': upload.name}])
    assert injected, f'The intended {failure} fault was not reached'
    assert storage.get_run(run['id'])['status'] == 'failed'
    assert note.read_text() == original
    assert not upload.exists()
    assert len(storage.runs()) == 1


def test_stale_temporary_file_is_not_overwritten_or_removed(tmp_path):
    note = tmp_path / 'report.md'
    stale = note.with_suffix('.md.tmp')
    stale.write_text('Another writer owns this file')
    publication.save_report_sections(note, report('ООО «А»', 1))
    assert stale.read_text() == 'Another writer owns this file'
    assert note.read_text() == publication.update_report_sections('', report('ООО «А»', 1))


@pytest.mark.parametrize('failure', ['write', 'replace'])
def test_atomic_write_failure_preserves_existing_note_and_cleans_own_temp(tmp_path, monkeypatch, failure):
    note = tmp_path / 'report.md'
    original = report('ООО «А»', 'old')
    note.write_text(original)
    if failure == 'write':
        original_write = Path.write_text
        def fail_write(path, *args, **kwargs):
            if path != note:
                original_write(path, 'partial')
                raise OSError('simulated disk full')
            return original_write(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'write_text', fail_write)
    else:
        def fail_replace(path, *args, **kwargs):
            raise PermissionError('simulated replace permission failure')
        monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(OSError):
        publication.save_report_sections(note, report('ООО «А»', 'new'))
    assert note.read_text() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.md']


@pytest.mark.parametrize('seed', range(8))
def test_concurrent_disjoint_updates_are_all_preserved(tmp_path, seed):
    note = tmp_path / 'report.md'
    manual = '18.09.2026\n\n# Личные заметки\nНе менять.  \n\n'
    note.write_text(manual)
    names = [f'ООО «Фирма {n:02}»' for n in range(12)]
    Random(seed).shuffle(names)
    start = Barrier(len(names))
    def save(name):
        start.wait(timeout=10)
        publication.save_report_sections(note, report(name, name))
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        list(pool.map(save, names))
    actual = note.read_text()
    assert actual.startswith(manual)
    assert len(publication._sections(actual)) == len(names)
    for name in names:
        assert actual.count(f'### {name}\n') == 1
        assert f'Расчёт {name}\n' in actual
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.parametrize('seed', range(12))
def test_seeded_update_sequences_preserve_manual_boundaries_and_other_values(tmp_path, seed):
    random = Random(seed)
    note = tmp_path / 'report.md'
    prefix = '18.09.2026\n\n## Первая группа\nРучное вступление.  \n\n'
    boundary = '## Вторая группа\nРучное пояснение.  \n\n'
    footer = '# Примечания\nРучная итоговая запись.  \n'
    names = [f'ООО «Фирма {n:02}»' for n in range(8)]
    expected = {name: 'initial' for name in names}
    def block(name):
        return f'### {name}\nРасчёт {expected[name]}\n\n'
    note.write_text(prefix + ''.join(map(block, names[:4])) + boundary + ''.join(map(block, names[4:])) + footer)
    for step in range(50):
        name = random.choice(names)
        expected[name] = f'{seed}:{step}:{random.randrange(100000)}'
        publication.save_report_sections(note, report(name, expected[name]))
        actual = note.read_text()
        assert actual.startswith(prefix) and actual.endswith(footer)
        assert actual.count(boundary) == 1
        assert len(publication._sections(actual)) == len(names)
        for current in names:
            assert block(current) in actual
            assert (actual.index(f'### {current}\n') < actual.index(boundary)) == (current in names[:4])
        before = note.read_bytes()
        publication.save_report_sections(note, report(name, expected[name]))
        assert note.read_bytes() == before


@pytest.mark.parametrize('failure', ['read', 'allocate', 'mode'])
def test_failure_before_replace_does_not_touch_existing_note(tmp_path, monkeypatch, failure):
    note = tmp_path / 'report.md'
    original = report('ООО «А»', 'old')
    note.write_text(original)
    if failure == 'read':
        original_read = Path.read_text
        def fail_read(path, *args, **kwargs):
            if path == note:
                raise PermissionError('read denied')
            return original_read(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', fail_read)
    elif failure == 'allocate':
        def fail_allocate(*args, **kwargs):
            raise OSError('no space for temporary file')
        monkeypatch.setattr(publication.tempfile, 'mkstemp', fail_allocate)
    else:
        def fail_mode(*args, **kwargs):
            raise OSError('mode update failed')
        monkeypatch.setattr(Path, 'chmod', fail_mode)
    with pytest.raises(OSError):
        publication.save_report_sections(note, report('ООО «А»', 'new'))
    assert note.read_bytes() == original.encode()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.md']


def test_identical_update_does_not_require_writable_directory(tmp_path, monkeypatch):
    note = tmp_path / 'report.md'
    incoming = report('ООО «А»', 1)
    publication.save_report_sections(note, incoming)
    before = note.stat()
    def reject_write(*args, **kwargs):
        pytest.fail('An identical note must not be rewritten')
    monkeypatch.setattr(publication.tempfile, 'mkstemp', reject_write)
    publication.save_report_sections(note, incoming)
    assert (note.stat().st_ino, note.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize('mode', [0o600, 0o640, 0o644])
def test_replacing_existing_note_preserves_its_permissions(tmp_path, mode):
    note = tmp_path / 'report.md'
    note.write_text(report('ООО «А»', 0))
    note.chmod(mode)
    publication.save_report_sections(note, report('ООО «А»', 1))
    assert note.stat().st_mode & 0o777 == mode


def test_new_financial_note_and_temp_are_private(tmp_path, monkeypatch):
    note = tmp_path / 'report.md'
    replace = Path.replace
    seen = []
    def inspect_temp(path, destination):
        seen.append(path.stat().st_mode & 0o777)
        return replace(path, destination)
    monkeypatch.setattr(Path, 'replace', inspect_temp)
    publication.save_report_sections(note, report('ООО «А»', 1))
    assert seen == [0o600]
    assert note.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('fault', ['write', 'replace'])
def test_recovery_after_write_failure_keeps_manual_text_and_other_company(tmp_path, monkeypatch, fault):
    note = tmp_path / 'report.md'
    original = '18.09.2026\n\n# Комментарий\nПроверено вручную.  \n\n### ООО «Б»\nРасчёт сохранён\n\n'
    note.write_text(original)
    with monkeypatch.context() as faults:
        def fail(*args, **kwargs):
            raise OSError('transient fixture fault')
        faults.setattr(Path, 'write_text' if fault == 'write' else 'replace', fail)
        with pytest.raises(OSError):
            publication.save_report_sections(note, report('ООО «А»', 'new'))
    assert note.read_text() == original
    publication.save_report_sections(note, report('ООО «А»', 'new'))
    actual = note.read_text()
    assert '# Комментарий\nПроверено вручную.  \n\n' in actual
    assert '### ООО «Б»\nРасчёт сохранён\n\n' in actual
    assert actual.count('### ООО «А»') == 1
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('failure', ['audit', 'zip', 'inventory'])
def test_failed_replacement_keeps_previous_success_history_and_artifacts(isolated_engine, tmp_path, monkeypatch, failure):
    data, operators = isolated_engine
    output = tmp_path / 'notes'
    config = operators / 'glopro.md'
    config.write_text(config.read_text().replace('rules: {}', f"rules: {{}}\nobsidian_output: '{output}'"))
    first = source_file(tmp_path / 'first.xlsx')
    successful = engine.submit('glopro', '2026-09-18', 'import', [{'path': str(first), 'name': first.name}])
    assert successful['status'] == 'completed'
    old_record = storage.get_run(successful['id'])
    old_root = data / 'runs' / successful['id']
    old_artifacts = {p.relative_to(old_root).as_posix(): p.read_bytes() for p in old_root.rglob('*') if p.is_file()}
    note = output / 'Активация — 18.09.2026.md'
    note.write_text(note.read_text() + '\n# Ручная проверка\nСогласовано.  \n')
    old_note = note.read_bytes()
    second = source_file(tmp_path / 'second.xlsx', litres=200, customer=16600, supplier=16000)
    injected = []
    if failure == 'audit':
        write = Path.write_text
        def fail_audit(path, *args, **kwargs):
            if path.name == 'Проверка расчётов.json':
                injected.append(failure)
                raise OSError('simulated audit fault')
            return write(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'write_text', fail_audit)
    elif failure == 'zip':
        def fail_zip(*args, **kwargs):
            injected.append(failure)
            raise OSError('simulated ZIP fault')
        monkeypatch.setattr(engine.zipfile, 'ZipFile', fail_zip)
    else:
        def fail_inventory(*args, **kwargs):
            injected.append(failure)
            raise OSError('simulated metadata fault')
        monkeypatch.setattr(engine, '_file', fail_inventory)
    failed = engine.submit('glopro', '2026-09-18', 'import', [{'path': str(second), 'name': second.name}])
    assert injected, f'The intended {failure} fault was not reached'
    assert failed['status'] == 'failed'
    assert failed['id'] != successful['id']
    assert storage.get_run(successful['id']) == old_record
    assert len(storage.runs()) == 2
    assert note.read_bytes() == old_note
    assert {p.relative_to(old_root).as_posix(): p.read_bytes() for p in old_root.rglob('*') if p.is_file()} == old_artifacts


@pytest.mark.parametrize('fence', ['```', '~~~', '````'])
@pytest.mark.parametrize('malformed', ['existing', 'incoming'])
def test_unclosed_fence_is_rejected_without_changing_note(tmp_path, fence, malformed):
    note = tmp_path / 'report.md'
    existing = report('ООО «А»', 'previous')
    incoming = report('ООО «Б»', 'new')
    unclosed = '\n## Ручной пример\n' + fence + '\n### Пример\nСохранить буквально.\n'
    if malformed == 'existing':
        existing += unclosed
    else:
        incoming += unclosed
    note.write_text(existing)
    for attempt in range(2):
        with pytest.raises(ValueError, match='Незакрытый блок кода'):
            publication.save_report_sections(note, incoming)
        assert note.read_bytes() == existing.encode()
        assert list(tmp_path.iterdir()) == [note]
