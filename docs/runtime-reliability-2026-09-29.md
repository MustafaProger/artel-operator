# Проверка надёжности runtime — 29 сентября 2026

Проверка выполнена в `/Users/mustafa/Desktop/artel-operator`. Область этой работы: сеть, очередь/расписание, финализация запуска и запись Markdown-отчётов. Изменения GloPro/Яндекса и общий прогон приложения ведёт отдельный координатор; его результаты описаны в `fuel-integrations-reliability-2026-09-29.md`.

В runtime-разработке использованы временные SQLite, каталоги, заметки и локальные сетевые fixtures. Эта задача службу не перезапускала, `/api/run` не вызывала; новые кабинетные выгрузки, SEO-публикации и записи в настоящий Obsidian не выполнялись. Последующее применение координатором зафиксировано отдельно ниже. Ранее внесённые изменения в Desktop сохранены. Старый worktree не переносился. Commit/push не выполнялись.

## Исправленные дефекты

1. **Публикация заметки происходила до завершения локального результата.** Для GloPro и Яндекса сбой аудита, ZIP или метаданных оставлял `failed`, но уже менял заметку: исходные шесть случаев воспроизведены. Теперь встроенные processors возвращают внутренний одноразовый callback: engine сначала записывает результаты/аудит, закрывает ZIP, строит список файлов и SHA256, сохраняет checkpoint `running`, затем публикует заметку и сохраняет терминальный статус. Callback не сериализуется и не повторяется автоматически.
2. **Общий `.md.tmp` конфликтовал при конкурентных записях.** Восемь исходных конкурентных последовательностей теряли обновления либо падали с `FileNotFoundError`; чужой оставшийся временный файл также перезаписывался/удалялся. Новый `save_merged_sections` сериализует чтение, merge и replace внутри одного процесса, использует уникальный временный файл и убирает только свой файл. Режим новой заметки — `0600`, режим существующей сохраняется. Координатор подключил общий helper к Яндексу.
3. **Заголовки внутри ручных Markdown-примеров воспринимались как реальные компании.** Исправлен разбор fenced code blocks и отступов заголовков; ручные примеры сохраняются. Незакрытый блок кода в существующем или входящем тексте вызывает понятный `ValueError` до изменения заметки: шесть новых случаев сначала воспроизводили некорректную запись, затем прошли.
4. **Отказ удаления одного временного upload прерывал очистку остальных.** Шесть исходно красных случаев проверяли три результата × два положения недоступного файла. Теперь очистка пытается удалить каждый upload, сохраняет исход расчёта и добавляет обезличенное событие о количестве ошибок очистки.
5. **Сбой последней записи SQLite мог оставить неверный итог в памяти.** После `OSError`/`sqlite3.Error` при терминальном сохранении делается одна попытка записать `failed` с явным предупреждением о возможном сохранённом внешнем результате. Handler, processor и публикация не повторяются. Полный отказ SQLite по-прежнему требует восстановления при следующем старте.
6. **Сетевые дефекты:** ошибка запуска обслуживающего потока оставляла listener открытым; недопустимые DNS labels проходили до resolver; медленный CONNECT-заголовок обходил общий срок ожидания; TLS-данные, пришедшие вместе с заголовком, ошибочно учитывались в лимите заголовка. В исходном наборе было 14 красных случаев. Добавлены обязательная очистка, проверка DNS-имён/allowlist, общий срок заголовка 5 секунд и отдельный подсчёт его размера.
7. **Неполный обход файлов ошибочно считался успешным.** Независимое ревью воспроизвело на настоящих временных правах доступа: после `ZIP.close` и `root.chmod(0)` прежний `Path.rglob()` возвращал пустой обход, engine публиковал заметку и сохранял `completed` с `files=[]`. Теперь ZIP и inventory используют явные `iterdir/lstat/stat` и пробрасывают ошибки доступа/IO. Восемь исходно красных permission-сценариев теперь сохраняют `failed` и прежнюю заметку. Состав включаемых файлов, исключение самого ZIP и правило необхода directory symlinks сохранены.

## Матрица проверок

| Риск | Сценарий и ожидаемый результат | Проверка |
| --- | --- | --- |
| Два запуска одновременно | 2/8/16 независимых SQL connections; manual/schedule/mixed — одна активная запись | `test_sql_reservation_race_across_connections` |
| Двойная отправка в executor | 2/12 конкурентных submit — один dispatch | `test_engine_manual_and_schedule_race_dispatches_once` |
| Перезапуск портит историю | Только queued/running переходят в failed; четыре терминальных статуса и остальные поля не меняются | `test_restart_only_interrupts_active_and_preserves_all_other_payloads` |
| Повтор расписания после ошибки | scheduled_once переживает каждый терминальный статус и restart; новый ручной запуск разрешён | `test_scheduled_once_survives_terminal_state_and_allows_explicit_manual_retry` |
| Гонки накопленных состояний | 4 seed × 160 переходов reserve/run/finish/restart, независимая модель ожидаемых состояний, максимум одна активная запись | `test_seeded_reservation_restart_state_machine` |
| Поздний wakeup/repeated tick | Семидневное окно, до/после времени запуска, 2 × 24 ticks и два restart, нет повторного failed schedule | `test_wakeup_window_and_repeated_tick_do_not_replay_terminal_schedule` |
| Неполная SQL-транзакция | Ошибки тела транзакции и настоящего commit wrapper откатывают резервирование, dispatch не вызывается | `test_failed_transaction_rolls_back_reservation_and_settings`, `test_reservation_commit_failure_rolls_back_and_never_dispatches` |
| DB недоступна | initial/progress/checkpoint/final transient failure; permanent failure до работы и после checkpoint; нет replay, безопасное восстановление | `test_transient_database_failure_is_terminal_without_replaying_side_effect`, два `test_permanent_*database_failure*` |
| Повтор побочного действия | Ошибка handler/processor/publisher, число вызовов ≤1; следующий явный запуск успешен | `test_stage_exception_runs_at_most_once_and_does_not_poison_next_run` |
| Поздний сбой портит заметку | outputs/audit/ZIP open/write/close/inventory/checkpoint × GloPro/Яндекс = 14 случаев; reached-fault проверен, старая заметка побайтно неизменна | `test_failed_finalization_preserves_previous_note` |
| Неудачная замена портит успешную историю | completed → failed при audit/ZIP/inventory; старая запись, все файлы и ручное дополнение сохранены | `test_failed_replacement_keeps_previous_success_history_and_artifacts` |
| Локальные артефакты/cleanup | Шесть этапов файлового отказа; шесть комбинаций cleanup, без replay и без повреждения предыдущего запуска | `test_local_artifact_fault_never_replays_handler_or_damages_previous_success`, `test_cleanup_attempts_every_upload_and_preserves_pipeline_outcome` |
| Обход скрывает недоступную папку | GloPro/Яндекс × ZIP/inventory × root/nested = 8 настоящих permission failures; failed, заметка неизменна, import очищен. Ещё четыре IO failures и сохранение правил включения файлов | `test_unreadable_artifact_directory_blocks_note_publication`, `test_traversal_propagates_io_errors`, `test_strict_traversal_preserves_existing_file_inclusion_rules` |
| Сбой атомарной записи | Ошибки чтения/выделения temp/записи/chmod/replace и последующее восстановление; старая note сохранена, собственные temp убраны | `test_atomic_write_failure_preserves_existing_note_and_cleans_own_temp`, `test_failure_before_replace_does_not_touch_existing_note`, `test_recovery_after_write_failure_keeps_manual_text_and_other_company` |
| Потеря конкурентных обновлений | 8 seed × 12 writers = 96 записей разных компаний; сохранены все | `test_concurrent_disjoint_updates_are_all_preserved` |
| Порча ручного Markdown | 12 seed × 50 semantic updates = 600 обновлений и 600 отдельных идемпотентных повторов; чужие компании/ручные границы сохранены | `test_seeded_update_sequences_preserve_manual_boundaries_and_other_values` |
| Незакрытый code fence | existing/incoming × три вида fence; шесть отказов до записи плюс шесть повторных попыток | `test_unclosed_fence_is_rejected_without_changing_note` |
| Лишний доступ сети | 96 детерминированных generated domain-boundary inputs; некорректные host/allowlist не доходят до DNS/socket | `test_seeded_domain_boundary_cases`, `test_invalid_allowlist_fails_before_socket`, `test_invalid_connect_never_opens_upstream` |
| Утечки сети и бесконечный CONNECT | thread.start failure, EOF/reset/timeout, заголовок сверх лимита, slow-drip deadline, shutdown активных sockets | `test_listener_closed_if_serve_thread_cannot_start`, `test_dripped_header_has_total_deadline`, `test_shutdown_closes_incomplete_headers_and_active_tunnel` |
| Неправильный failover | ≤4 IPv4 addresses и общий connect budget, bind fail без fallback, никаких повторов установленного потока | `test_connect_attempts_are_bounded_by_four_addresses`, `test_connect_deadline_is_shared_across_addresses`, `test_established_tunnel_errors_close_both_peers_without_replay` |
| Shutdown во время DNS | Клиенты/listener закрываются сразу; handler ждёт OS resolver, после ответа не создаёт позднее соединение | `test_shutdown_closes_sockets_while_dns_waits_for_os` |

## Числа и воспроизведение

Число pytest cases включает параметризацию; это не число уникальных алгоритмов. Generated transitions/updates ниже являются шагами внутри тестов, а повторы запуска pytest не добавляют новых случаев.

- Сеть: 30 новых test functions, 222 новых collected cases; вместе с 27 существующими — **249 passed**. Новые cases: 96 generated domain boundaries, 122 статических/параметризованных случая, четыре последовательности. Последовательности: 2 seed × 40 concurrent socketpairs и 2 seed × 12 полных proxy contexts. Три отдельных процесса: 249 passed за 3.62/3.63/3.57 s.
- Очередь/жизненный цикл: 14 новых test functions, **50 cases**, включая 640 переходов модели состояния и 48 scheduler ticks. Три отдельных процесса: 50 passed за 0.95/0.78/0.87 s.
- Публикация и Markdown boundaries: 14 новых test functions, 67 новых cases; вместе с четырьмя прежними cases — **71 passed**. Три отдельных процесса: 71 passed за 0.61/0.60/0.66 s. Связанный набор с engine/weekly/Яндексом — 126 passed за 1.12 s.
- Строгий обход: три новые test functions, **13 cases**. Восемь permission-сценариев сначала упали на прежнем коде; после исправления все прошли. Связанный набор — 186 passed за 1.98 s; отдельный повтор — 13 passed.
- Всего добавлено **61 test function и 352 collected cases** в области runtime. Повторы и generated steps в эту сумму не включены повторно.
- Финальный объединённый runtime-набор: **508 passed, 2 warnings за 5.88 s**. Предупреждения — существующие deprecations `httpx`/Starlette и `anyio.abc.BlockingPortal`. `git diff --check` прошёл. Полный suite приложения этому числу не равен; его отдельно запускает координатор.

```sh
.venv/bin/python -m pytest -q tests/test_network.py tests/test_reliability_network.py
.venv/bin/python -m pytest -q tests/test_reliability_queue.py
.venv/bin/python -m pytest -q tests/test_reliability_publication.py tests/test_publication_boundaries.py tests/test_engine.py tests/test_weekly_engine.py tests/test_yandex.py
.venv/bin/python -m pytest -q tests/test_reliability_traversal.py

# Итоговый объединённый прогон, 508 cases
.venv/bin/python -m pytest -q tests/test_network.py tests/test_reliability_network.py tests/test_reliability_queue.py tests/test_reliability_publication.py tests/test_reliability_traversal.py tests/test_run_lifecycle.py tests/test_engine.py tests/test_weekly_engine.py tests/test_publication_boundaries.py tests/test_schedule.py tests/test_seo.py tests/test_api.py
```

## Проверка работающего приложения без изменений

Зафиксировано до передачи координатору на финальный прогон/перезапуск:

- Единственный listener `127.0.0.1:8790`, PID `5077`; SQLite открыт только в `mode=ro`, `integrity_check=ok`, 50 записей истории, активных заданий 0, `scheduler_error=null`.
- Все три расписания включены: GloPro — 02.10 07:00 МСК, SEO — 01.10 12:00 МСК, Яндекс — 06.10 07:00 МСК.
- Сохранённый GloPro run `25b6403482114bf5aec92caa2f8fce8f`: 18 файлов, несовпадений SHA256 — 0; ZIP по HTTP 200, 17 entries, CRC без ошибок, SHA256 HTTP-ответа совпадает с сохранённым.
- Сохранённый Яндекс run `5f9e88d6961649df9164b0ef2dc15408`: 9 файлов, несовпадений SHA256 — 0; ZIP по HTTP 200, 8 entries, CRC без ошибок, SHA256 HTTP-ответа совпадает с сохранённым.
- В браузере открыты обзор и детали обоих сохранённых результатов: статусы «Готово», список из 18/9 файлов и отчёты отображаются. В console нет warn/error. Временная вкладка закрыта. Новые запуски не создавались; прежние ошибки истории намеренно не удалялись.

Это проверка доступности и сохранности уже созданных результатов. Она не является новой кабинетной выгрузкой или независимым пересчётом финансовых сумм. Подробности утреннего восстановления — в `fuel-recovery-2026-09-29.md` и `yandex-fix-2026-09-29.md`.

## Границы гарантий

- SQLite и replace заметки не образуют общей транзакции. Crash или полный отказ последней DB-записи после записи заметки может оставить обновлённую заметку и checkpoint running; следующий старт пометит запуск failed. При доступной БД после временной ошибки сохраняется предупреждение о возможном внешнем результате. Автоматического replay нет.
- Поддерживается один процесс службы. Общий publication lock не синхронизирует запись с отдельным процессом Obsidian/iCloud. Защита от потери питания и fsync-транзакция нескольких файлов не добавлялись.
- `socket.getaddrinfo()` синхронно зависит от ОС и может ждать дольше сетевого connect budget. Shutdown закрывает listener/client, но не отменяет OS resolver; это явно проверено. Нельзя обещать абсолютный 15-секундный предел всей операции DNS+connect.
- В режиме auto, если подходящий физический интерфейс не найден, сохраняется обычная маршрутизация. Только явно неверный заданный интерфейс отклоняется; auto не заявляется как fail-closed.
- Неудачный scheduled run занимает свой `scheduled_once`; повторные ticks не повторяют его. Явный ручной повтор создаёт отдельную запись. Это сохранённый контракт, а не новый механизм автоматических попыток.
- Отложенная публикация здесь относится к топливным заметкам. Существующий SEO processor публикует до локального ZIP; его контракт этой работой не менялся. SEO regression использует mocks, реальная CMS не проверялась.
- Тесты с локальными sockets и временными данными не доказывают поведение настоящего VPN, доступность кабинетов или работу внешних сервисов при любом будущем сбое.
- Локальная проверка runtime и последующее применение координатором — разные этапы; подтверждение второго приведено ниже.

## Общий прогон и применение: подтверждение координатора

29.09.2026 чат `01a0e2b8-7789-7912-a7ec-bffe577c01ea` («Найти ошибку в Artel Operator») передал результаты общего прогона и применения. Ниже приведены его подтверждения; отдельный второй перезапуск эта задача не выполняла.

- `RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest -q`: **1666 passed, 54 subtests passed, 2 прежних warnings за 19.50 s**. Число включает весь suite, а не добавляется к 508 runtime cases. `git diff --check` прошёл.
- До применения: active=0, history=50; подтверждены единственный PID `5077`, рабочий каталог и LaunchAgent. Создана SQLite online backup `data/backups/reliability-20260929/110706/operator.sqlite3`, режим `0600`, `integrity_check=ok`.
- Координатор выполнил один graceful restart LaunchAgent `ru.artel.operator`: PID `5077` → `10021`, startup/shutdown без ошибок. После запуска: API 200, `scheduler_error=null`, active=0, один listener; **все 50 полных payload истории, три инструкции и настройки БД** совпали с baseline, 47 файлов snapshot не изменились.
- Оба сохранённых топливных результата доступны; **все 27 файлов проверены по HTTP**, контрольные суммы и ZIP без ошибок.

Таким образом, исправления применены к работающей службе. Это по-прежнему не новый live-прогон кабинетов и не обещание отсутствия любых будущих внешних сбоев.
