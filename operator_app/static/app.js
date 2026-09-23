(() => {
  'use strict';

  const icons = {
    dashboard: '<rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    history: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6"/><path d="M12 7v5l3 2"/>',
    document: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h6"/>',
    settings: '<path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"/><path d="m9 3 1-1h4l1 3 3 1 3-1 2 4-2 2v3l2 2-2 4-3-1-3 1-1 3h-4l-1-3-3-1-3 1-2-4 2-2v-3L1 9l2-4 3 1 3-1V3Z" transform="translate(1 0) scale(.92)"/>',
    play: '<path d="m8 5 10 7-10 7Z"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18M8 15h2M14 15h2"/>',
    'check-circle': '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    attention: '<path d="m10 4-8 14a2 2 0 0 0 2 3h16a2 2 0 0 0 2-3L14 4a2.3 2.3 0 0 0-4 0Z"/><path d="M12 9v4M12 17h.01"/>',
    'arrow-up-right': '<path d="M6 18 18 6M6 6h12v12"/>',
    'arrow-right': '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    download: '<path d="M12 3v13m-5-5 5 5 5-5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    search: '<circle cx="10.5" cy="10.5" r="7"/><path d="m16 16 5 5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    link: '<path d="m10 13 4-4M9 15l-2 2a3.5 3.5 0 0 1-5-5l5-5a3.5 3.5 0 0 1 5 0m0 2 2-2a3.5 3.5 0 1 1 5 5l-5 5a3.5 3.5 0 0 1-5 0"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    fuel: '<path d="M4 21V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v16M2 21h15M4 10h11M15 12h2a2 2 0 0 1 2 2v3a1.5 1.5 0 0 0 3 0V8l-4-4M20 6v3h2"/>',
    folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    edit: '<path d="m16 3 5 5M3 21l5-1L21 7a2 2 0 0 0 0-3l-1-1a2 2 0 0 0-3 0L4 16l-1 5Z"/>',
  };

  const $ = (id) => document.getElementById(id);
  const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.document}</svg>`;
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const state = { operators: [], runs: [], connection: {}, loaded: false, pending: false, currentView: 'overview', editorId: '', originalMarkdown: '', editorDirty: false, editorLoading: false, detailId: null, lastStateSignature: '', refreshing: false };
  const pageNames = { overview: 'Обзор', operators: 'Операторы', runs: 'История запусков', instructions: 'Инструкции', settings: 'Подключения' };
  const activeStatuses = new Set(['running', 'pending', 'queued', 'downloading', 'calculating', 'processing']);
  const successStatuses = new Set(['success', 'completed', 'complete', 'succeeded', 'ready']);
  const attentionStatuses = new Set(['failed', 'error', 'needs_review', 'blocked', 'partial', 'attention', 'needs_attention', 'cancelled']);
  const statusNames = { running: 'В работе', pending: 'В очереди', queued: 'В очереди', downloading: 'Загрузка', calculating: 'Расчёт', processing: 'Обработка', success: 'Готово', completed: 'Готово', complete: 'Готово', succeeded: 'Готово', ready: 'Готово', failed: 'Ошибка', error: 'Ошибка', needs_review: 'Нужна проверка', needs_attention: 'Нужна проверка', blocked: 'Нужно действие', partial: 'Неполный результат', attention: 'Нужна проверка', cancelled: 'Отменён', no_data: 'Нет операций' };
  const days = { tue: 'Вт', fri: 'Пт', mon: 'Пн', wed: 'Ср', thu: 'Чт', sat: 'Сб', sun: 'Вс', tuesday: 'Вт', friday: 'Пт', monday: 'Пн', wednesday: 'Ср', thursday: 'Чт', saturday: 'Сб', sunday: 'Вс', '0': 'Пн', '1': 'Вт', '2': 'Ср', '3': 'Чт', '4': 'Пт', '5': 'Сб', '6': 'Вс' };

  function hydrateIcons(root = document) {
    root.querySelectorAll('[data-icon]').forEach((element) => { element.innerHTML = svg(element.dataset.icon); });
  }

  function formatDate(value, options = {}) {
    if (!value) return '—';
    const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00+03:00` : value);
    if (!Number.isFinite(date.getTime())) return '—';
    return new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit', ...options }).format(date);
  }

  function formatTime(value) {
    if (!value) return '—:—';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '—:—';
    return new Intl.DateTimeFormat('ru-RU', { timeZone: 'Europe/Moscow', hour: '2-digit', minute: '2-digit' }).format(date);
  }

  function moscowDate() {
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date());
    const part = (type) => parts.find((item) => item.type === type).value;
    return `${part('year')}-${part('month')}-${part('day')}`;
  }

  function scheduledDate(kind = 'glopro') {
    if (kind === 'seo') return moscowDate();
    const date = new Date(`${moscowDate()}T12:00:00Z`);
    while (!(kind === 'yandex' ? [2] : [2, 5]).includes(date.getUTCDay())) date.setUTCDate(date.getUTCDate() - 1);
    return date.toISOString().slice(0, 10);
  }

  function periodForDate(value, kind = 'glopro') {
    const date = new Date(`${value}T12:00:00Z`);
    if (kind === 'seo') return Number.isFinite(date.getTime()) ? { start: value, end: value } : null;
    if (!Number.isFinite(date.getTime()) || !(kind === 'yandex' ? [2] : [2, 5]).includes(date.getUTCDay())) return null;
    const start = new Date(date);
    const end = new Date(date);
    start.setUTCDate(date.getUTCDate() - (kind === 'yandex' ? 7 : date.getUTCDay() === 2 ? 4 : 3));
    end.setUTCDate(date.getUTCDate() - 1);
    return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
  }

  const periodLabel = (start, end) => `${formatDate(start)} — ${formatDate(end, { year: 'numeric' })}`;
  const runPeriodLabel = (run) => Array.isArray(run.output_period) && run.output_period.length === 2 ? periodLabel(...run.output_period) : periodLabel(run.period_start, run.period_end);
  const operatorName = (id) => state.operators.find((operator) => operator.id === id)?.name || id || 'Оператор';
  const runStatus = (run) => String(run.status || '').toLowerCase();
  const isActive = () => state.pending || state.runs.some((run) => activeStatuses.has(runStatus(run)));

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), options.timeout || 45000);
    try {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options, signal: controller.signal, headers: options.body instanceof FormData ? { ...options.headers } : { 'Content-Type': 'application/json', ...options.headers } });
      const type = response.headers.get('content-type') || '';
      const payload = type.includes('application/json') ? await response.json() : { message: await response.text() };
      if (!response.ok) {
        const detail = payload.detail || payload.error || payload.message;
        throw new Error(typeof detail === 'string' && detail.length < 1800 ? detail : `Сервер вернул ошибку ${response.status}.`);
      }
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Сервер не ответил вовремя. Проверьте историю перед повторным запуском.');
      if (error instanceof TypeError) throw new Error('Не удалось связаться с приложением. Проверьте, запущен ли сервер.');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function toast(message, error = false) {
    const element = document.createElement('div');
    element.className = `toast${error ? ' error' : ''}`;
    element.textContent = message;
    $('toast-container').append(element);
    window.setTimeout(() => element.remove(), error ? 8500 : 5000);
  }

  function statusBadge(run) {
    const status = runStatus(run);
    const style = successStatuses.has(status) ? 'success' : activeStatuses.has(status) ? 'progress' : ['failed', 'error'].includes(status) ? 'danger' : attentionStatuses.has(status) ? 'attention' : 'neutral';
    return `<span class="badge badge-${style}">${escape(statusNames[status] || run.status || 'Неизвестно')}</span>`;
  }

  function safeFileUrl(url) {
    if (typeof url !== 'string' || !url) return '';
    try {
      const parsed = new URL(url, window.location.origin);
      if (parsed.origin !== window.location.origin || !['http:', 'https:'].includes(parsed.protocol)) return '';
      return parsed.href;
    } catch { return ''; }
  }

  function fileList(run) { return Array.isArray(run.files) ? run.files : []; }

  function zipFile(run) {
    return fileList(run).find((file) => /\.zip$/i.test(file.name || '') && safeFileUrl(file.url));
  }

  function emptyState(title, text, action = '') {
    return `<div class="empty-state"><span class="empty-icon">${svg('folder')}</span><h3>${escape(title)}</h3><p>${escape(text)}</p>${action}</div>`;
  }

  function scheduleText(operator) {
    const schedule = operator.schedule || {};
    const scheduledDays = schedule.every_days ? `Каждые ${schedule.every_days} дня` : Array.isArray(schedule.days) ? schedule.days.map((day) => days[String(day).toLowerCase()] || String(day)).join(' и ') : 'Расписание не задано';
    return `${scheduledDays}${schedule.time ? ` · ${schedule.time}` : ''} · МСК`;
  }

  function operatorCard(operator) {
    const unsupported = operator.kind && !['glopro', 'yandex', 'seo'].includes(operator.kind);
    const enabled = operator.enabled && !unsupported;
    const badge = unsupported ? '<span class="badge badge-attention">Нужен обработчик</span>' : `<span class="badge badge-${enabled ? 'success' : 'neutral'}">${enabled ? 'По расписанию' : 'Автозапуск выключен'}</span>`;
    const latest = state.runs.find((run) => run.operator_id === operator.id);
    return `<article class="operator-card"><div class="operator-main"><div class="operator-symbol">${svg(operator.kind === 'glopro' || operator.id === 'glopro' ? 'fuel' : 'layers')}</div><div class="operator-info"><div class="operator-title-line"><h3>${escape(operator.name || operator.id)}</h3>${badge}</div><p>${escape(operator.description || 'Выгрузки, расчёт топлива и пакет файлов за выбранный период.')}</p></div><div class="operator-actions"><button class="button button-secondary button-small" type="button" data-edit-operator="${escape(operator.id)}">${svg('edit')}Инструкция</button><button class="icon-button" type="button" data-run-operator="${escape(operator.id)}" aria-label="Запустить ${escape(operator.name || operator.id)}" ${isActive() || unsupported ? 'disabled' : ''}>${svg('play')}</button></div></div><div class="operator-meta"><span>${svg('calendar')}${escape(scheduleText(operator))}</span><span>${svg('document')}Правила в Markdown</span><span>${svg('clock')}${latest ? `Последний запуск ${escape(formatDate(latest.created_at))}` : 'Первый запуск впереди'}</span></div></article>`;
  }

  function renderOperators() {
    const html = state.operators.length ? state.operators.map(operatorCard).join('') : emptyState('Пока нет операторов', 'Добавьте Markdown-инструкцию, чтобы настроить первый процесс.');
    $('overview-operators').innerHTML = html;
    $('all-operators').innerHTML = html;
    $('operator-count').textContent = state.operators.length;
    $('operators-summary-count').textContent = state.operators.length;
    const select = $('editor-operator');
    const oldValue = select.value;
    select.innerHTML = state.operators.map((operator) => `<option value="${escape(operator.id)}">${escape(operator.name || operator.id)}</option>`).join('');
    if (state.operators.some((operator) => operator.id === oldValue)) select.value = oldValue;
    if (state.currentView === 'instructions' && !state.editorId && select.value) loadEditor(select.value);
  }

  function renderRuns() {
    function table(runs, emptyTitle, emptyDescription) {
      if (!runs.length) return emptyState(emptyTitle, emptyDescription);
      return `<table class="runs-table"><thead><tr><th>ОПЕРАТОР</th><th>ПЕРИОД</th><th>СТАТУС</th><th class="run-time-column">ЗАПУСК</th><th></th></tr></thead><tbody>${runs.map((run) => {
        const zip = zipFile(run);
        return `<tr><td><span class="run-name">${escape(operatorName(run.operator_id))}</span><span class="run-sub">${escape(({ scheduled: 'По расписанию', schedule: 'По расписанию', manual: 'Ручной запуск', import: 'Импорт файлов' })[run.trigger] || 'Запуск оператора')}</span></td><td><span class="run-period">${escape(runPeriodLabel(run))}</span></td><td>${statusBadge(run)}</td><td class="run-time-column"><span class="run-period">${escape(formatDate(run.created_at))}</span><span class="run-sub">${escape(formatTime(run.created_at))} МСК</span></td><td class="run-action-cell"><div class="table-actions">${zip ? `<a class="run-download" href="${escape(safeFileUrl(zip.url))}" download>${svg('download')}${successStatuses.has(runStatus(run)) ? 'Скачать ZIP' : runStatus(run) === 'no_data' ? 'Исходники ZIP' : 'ZIP проверки'}</a>` : ''}<button class="icon-button" type="button" data-run-details="${escape(run.id)}" aria-label="Открыть запуск за ${escape(runPeriodLabel(run))}">${svg('arrow-up-right')}</button></div></td></tr>`;
      }).join('')}</tbody></table>`;
    }
    $('recent-runs').innerHTML = table(state.runs.slice(0, 4), 'Первый отчёт скоро будет здесь', 'Подключите GloPro и запустите расчёт или импортируйте уже скачанные выгрузки.');
    const query = $('run-search').value.trim().toLocaleLowerCase('ru-RU');
    const filter = $('run-filter').value;
    const filtered = state.runs.filter((run) => {
      const status = runStatus(run);
      const matchesStatus = filter === 'all' || filter === 'success' && successStatuses.has(status) || filter === 'active' && activeStatuses.has(status) || filter === 'attention' && attentionStatuses.has(status);
      const searchable = `${operatorName(run.operator_id)} ${run.id} ${run.period_start} ${run.period_end} ${runPeriodLabel(run)}`.toLocaleLowerCase('ru-RU');
      return matchesStatus && (!query || searchable.includes(query));
    });
    $('all-runs').innerHTML = table(filtered, state.runs.length ? 'Нет подходящих запусков' : 'История пока пуста', state.runs.length ? 'Попробуйте изменить строку поиска или статус.' : 'Завершённые расчёты и их исходные файлы будут сохранены здесь.');
  }

  function renderSummary() {
    $('completed-count').textContent = state.runs.filter((run) => successStatuses.has(runStatus(run))).length;
    const attentionCount = state.runs.filter((run) => attentionStatuses.has(runStatus(run))).length;
    $('attention-count').textContent = attentionCount;
    $('attention-description').textContent = attentionCount ? 'Ошибки и результаты, которые нужно проверить' : 'В загруженной истории нет ошибок и результатов на проверку';
    const recentActivity = state.runs.slice(0, 18).reverse();
    const activityTrack = $('activity-track');
    activityTrack.innerHTML = recentActivity.map((run) => {
      const status = runStatus(run);
      const category = successStatuses.has(status) ? 'success' : activeStatuses.has(status) ? 'active' : attentionStatuses.has(status) ? 'attention' : 'neutral';
      return `<i class="${category}" aria-hidden="true" title="${escape(formatDate(run.created_at))} · ${escape(statusNames[status] || run.status)}"></i>`;
    }).join('');
    const recentReady = recentActivity.filter((run) => successStatuses.has(runStatus(run))).length;
    const recentAttention = recentActivity.filter((run) => attentionStatuses.has(runStatus(run))).length;
    activityTrack.setAttribute('aria-label', recentActivity.length ? `Последние запуски: ${recentActivity.length}. Готово: ${recentReady}. Требуют внимания: ${recentAttention}. Слева — более ранние.` : 'Запусков пока нет');
    activityTrack.title = recentActivity.length ? `Последние ${recentActivity.length} запусков. Слева — более ранние.` : 'Запусков пока нет';
    const next = state.operators.filter((operator) => operator.enabled && operator.next_run).sort((a, b) => new Date(a.next_run) - new Date(b.next_run))[0];
    if (next) {
      $('next-run-time').textContent = formatTime(next.next_run);
      $('next-run-date').textContent = formatDate(next.next_run, { weekday: 'long', day: 'numeric', month: 'long' });
      $('next-run-name').textContent = next.name || next.id;
      const localDate = new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(next.next_run));
      const period = periodForDate(localDate, next.kind);
      $('next-run-period').textContent = period ? `За ${periodLabel(period.start, period.end)}` : scheduleText(next);
    } else {
      const operator = state.operators[0];
      $('next-run-time').textContent = '—:—';
      $('next-run-date').textContent = 'Автозапуск выключен';
      $('next-run-name').textContent = operator?.name || 'Оператор не настроен';
      $('next-run-period').textContent = operator ? `Настроено: ${scheduleText(operator)}` : 'Добавьте первого оператора';
    }
    const configured = Boolean(state.connection.configured);
    const verified = Boolean(state.connection.verified);
    const badge = $('connection-badge');
    badge.classList.toggle('connected', verified);
    badge.innerHTML = `<span class="status-dot"></span>${verified ? 'Вход GloPro проверен' : configured ? 'GloPro · нужна проверка' : 'Подключить GloPro'}`;
    const settingsBadge = $('settings-connection-status');
    settingsBadge.textContent = verified ? 'Вход проверен' : configured ? 'Доступ сохранён' : 'Не подключено';
    settingsBadge.className = `badge badge-${verified ? 'success' : configured ? 'attention' : 'neutral'}`;
    $('connection-test').disabled = !configured;
    updateRunButtons();
  }

  function updateRunButtons() {
    const busy = isActive();
    document.querySelectorAll('[data-action="manual-run"]').forEach((button) => { button.disabled = busy || !state.operators.length; button.title = busy ? 'Дождитесь завершения текущего запуска' : ''; });
    document.querySelectorAll('[data-action="import"]').forEach((button) => { button.disabled = busy; });
    document.querySelectorAll('[data-run-operator]').forEach((button) => {
      const operator = state.operators.find((item) => item.id === button.dataset.runOperator);
      button.disabled = busy || Boolean(operator?.kind && !['glopro', 'yandex', 'seo'].includes(operator.kind));
    });
  }

  async function refreshState() {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const payload = await api('/api/state', { timeout: 15000 });
      state.operators = Array.isArray(payload.operators) ? payload.operators : [];
      state.runs = (Array.isArray(payload.runs) ? payload.runs : []).slice().sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
      state.connection = payload.connection || {};
      state.yandex = payload.yandex || {};
      state.loaded = true;
      const signature = JSON.stringify([state.operators, state.runs, state.connection, state.yandex]);
      if (signature !== state.lastStateSignature) {
        state.lastStateSignature = signature;
        renderOperators();
        renderRuns();
        renderSummary();
        renderYandex();
        if ($('details-dialog').open && state.detailId) renderDetails(state.detailId);
      }
      $('global-error').hidden = !payload.scheduler_error;
      if (payload.scheduler_error) $('global-error-text').textContent = payload.scheduler_error;
      $('server-dot').className = 'status-dot live';
      $('server-label').textContent = 'Приложение работает';
    } catch (error) {
      $('global-error-text').textContent = error.message;
      $('global-error').hidden = false;
      $('server-dot').className = 'status-dot error';
      $('server-label').textContent = 'Нет связи с сервером';
      if (!state.loaded) {
        const empty = emptyState('Не удалось загрузить данные', 'Проверьте соединение с приложением и нажмите «Повторить» выше.');
        ['overview-operators', 'all-operators', 'recent-runs', 'all-runs'].forEach((id) => { $(id).innerHTML = empty; });
        $('next-run-date').textContent = 'Расписание недоступно';
        $('connection-badge').innerHTML = '<span class="status-dot"></span>Нет данных о подключении';
      }
    } finally { state.refreshing = false; }
  }

  function navigate(view) {
    if (!pageNames[view]) view = 'overview';
    const changedView = state.currentView !== view;
    state.currentView = view;
    document.querySelectorAll('.page-section').forEach((section) => { section.hidden = section.id !== `view-${view}`; });
    document.querySelectorAll('.main-nav>a').forEach((link) => {
      link.classList.toggle('active', link.dataset.view === view);
      if (link.dataset.view === view) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    $('page-label').textContent = pageNames[view];
    document.title = `${pageNames[view]} · Артель Оператор`;
    if (changedView) window.scrollTo(0, 0);
    if (view === 'instructions' && state.operators.length && !state.editorId) loadEditor($('editor-operator').value || state.operators[0].id);
  }

  async function loadEditor(id) {
    if (!id || state.editorLoading) return;
    state.editorLoading = true;
    $('markdown-editor').disabled = true;
    $('editor-save').disabled = true;
    $('editor-operator').disabled = true;
    $('editor-status').textContent = 'Загрузка инструкции…';
    try {
      const payload = await api(`/api/operators/${encodeURIComponent(id)}`);
      state.editorId = id;
      state.originalMarkdown = payload.markdown || '';
      state.editorDirty = false;
      $('markdown-editor').value = state.originalMarkdown;
      $('editor-operator').value = id;
      $('editor-status').textContent = 'Нет несохранённых изменений';
    } catch (error) {
      $('editor-status').textContent = 'Не удалось загрузить инструкцию';
      toast(error.message, true);
      if (state.editorId) $('editor-operator').value = state.editorId;
    } finally {
      state.editorLoading = false;
      $('markdown-editor').disabled = false;
      $('editor-operator').disabled = false;
    }
  }

  function openManualRun(operatorId) {
    if (isActive()) return toast('Уже есть выполняющийся запуск. Дождитесь результата.', true);
    $('run-operator').innerHTML = state.operators.filter((operator) => !operator.kind || ['glopro', 'yandex', 'seo'].includes(operator.kind)).map((operator) => `<option value="${escape(operator.id)}">${escape(operator.name || operator.id)}</option>`).join('');
    if (!$('run-operator').options.length) return toast('Нет оператора с доступным обработчиком.', true);
    if (operatorId) $('run-operator').value = operatorId;
    $('run-date').value = scheduledDate(selectedKind('run-operator'));
    $('run-form-error').hidden = true;
    updatePeriodPreview();
    $('run-dialog').showModal();
  }

  function updatePeriodPreview() {
    const kind = selectedKind('run-operator');
    if (kind === 'seo') { $('run-period-preview').textContent = 'Одна новая статья в актуальном четырёхдневном выпуске. Повтор использует сохранённый текст и не создаёт дубликат.'; return; }
    const period = periodForDate($('run-date').value, kind);
    if (!period) {
      $('run-period-preview').textContent = kind === 'yandex' ? 'Выберите вторник: отчёт за предыдущие вторник–понедельник.' : 'Выберите вторник или пятницу. Отчёт охватит дни перед выбранной датой.';
      return;
    }
    if (kind === 'yandex') { $('run-period-preview').textContent = `Яндекс: ${periodLabel(period.start, period.end)}, полные сутки по Москве. Все сотрудники с заправками будут определены по заказам недели.`; return; }
    const planned = new Date(`${$('run-date').value}T00:00:00Z`);
    let weekly = 'Китай и НК АРТЭЛЬ в пятницу пропускаются.';
    if (planned.getUTCDay() === 2) {
      planned.setUTCDate(planned.getUTCDate() - 7);
      weekly = `Китай и НК АРТЭЛЬ: ${periodLabel(planned.toISOString().slice(0, 10), period.end)}.`;
    }
    $('run-period-preview').textContent = `Обычные фирмы: ${periodLabel(period.start, period.end)}. ${weekly} Все даты по Москве, сутки целиком.`;
  }

  function showFormError(id, message) { $(id).textContent = message; $(id).hidden = false; }

  function renderDetails(id) {
    const run = state.runs.find((item) => String(item.id) === String(id));
    if (!run) return;
    state.detailId = id;
    $('details-title').textContent = `${operatorName(run.operator_id)} · ${runPeriodLabel(run)}`;
    const files = fileList(run).filter((file) => safeFileUrl(file.url));
    const report = typeof run.report === 'string' ? run.report : run.report ? JSON.stringify(run.report, null, 2) : '';
    const trigger = ({ scheduled: 'По расписанию', schedule: 'По расписанию', manual: 'Ручной запуск', import: 'Ручной импорт Excel' })[run.trigger] || run.trigger || 'Не указан';
    const runKind = state.operators.find(o => o.id === run.operator_id)?.kind;
    const unit = runKind === 'seo' ? 'Статей' : runKind === 'yandex' ? 'Складов' : 'Фирм';
    const count = (value) => Number.isInteger(value) && value >= 0 ? String(value) : '—';
    const running = activeStatuses.has(runStatus(run));
    const events = running && Array.isArray(run.events) ? run.events.slice(-5) : [];
    const failures = runStatus(run) === 'needs_review' && Array.isArray(run.failures) ? run.failures : [];
    const excludedClients = Array.isArray(run.excluded_clients) ? run.excluded_clients.map((client) => typeof client?.client === 'string' ? client.client.trim() : '').filter(Boolean) : [];
    const scheduleSkipped = Array.isArray(run.schedule_skipped_clients) ? run.schedule_skipped_clients.map((client) => client.client).filter(Boolean) : [];
    const emptyClients = Array.isArray(run.empty_clients) ? [...new Set(run.empty_clients.map((client) => client.client).filter(Boolean))] : [];
    const companyPeriods = Array.isArray(run.company_periods) ? run.company_periods.filter((item) => Array.isArray(item.period) && item.period.length === 2 && (item.period[0] !== run.period_start || item.period[1] !== run.period_end)) : [];
    $('run-details').innerHTML = `
      <div class="detail-meta">${statusBadge(run)}<span>Создан ${escape(formatDate(run.created_at, { year: 'numeric' }))} в ${escape(formatTime(run.created_at))} МСК</span>${run.finished_at ? `<span>Завершён ${escape(formatTime(run.finished_at))} МСК</span>` : ''}</div>
      <p class="field-hint">${escape(trigger)} · ${runKind === 'seo' ? 'Источников' : 'Исходных файлов'}: ${count(run.source_count)} · ${unit}: ${count(runKind === 'seo' ? run.article_count : run.client_count)}</p>
      ${excludedClients.length ? `<p class="field-hint">Временно пропущены: ${escape(excludedClients.join(', '))}.</p>` : ''}
      ${scheduleSkipped.length ? `<p class="field-hint">Только во вторник: ${escape(scheduleSkipped.join(', '))}. В этом запуске пропущены.</p>` : ''}
      ${emptyClients.length ? `<p class="field-hint">Без заправок — XLSX не скачивались (${emptyClients.length}): ${escape(emptyClients.join(', '))}.</p>` : ''}
      ${companyPeriods.map((item) => `<p class="field-hint">${escape(item.client)}: ${escape(periodLabel(item.period[0], item.period[1]))}, полные сутки по Москве.</p>`).join('')}
      ${run.error ? `<div class="inline-message error">${escape(typeof run.error === 'string' ? run.error : JSON.stringify(run.error))}</div>` : ''}
      ${events.length ? `<h3 class="detail-section-title">Ход выполнения</h3><ul class="selected-files">${events.map((event) => {
        const progress = Number.isInteger(event.current) && Number.isInteger(event.total) ? ` (${event.current} из ${event.total})` : '';
        return `<li>${svg('clock')}<span>${escape(formatTime(event.at))} · ${escape(event.message || event.stage || 'Обработка')}${escape(progress)}</span></li>`;
      }).join('')}</ul>` : ''}
      ${failures.length ? `<h3 class="detail-section-title">Файлы, требующие проверки · ${failures.length}</h3>${failures.map((failure) => `<div class="inline-message error"><strong>${escape(failure.file || 'Исходный файл')}</strong><br>${escape(failure.error || 'Не прошёл проверку')}</div>`).join('')}` : ''}
      <h3 class="detail-section-title">Файлы запуска · ${files.length}</h3>
      ${files.length ? `<div class="detail-file-list">${files.map((file) => `<a class="detail-file" href="${escape(safeFileUrl(file.url))}" download>${svg(/\.zip$/i.test(file.name) ? 'folder' : 'document')}<span>${escape(file.name)}</span>${svg('download')}</a>`).join('')}</div>` : `<p class="field-hint">${running ? 'Файлы появятся после завершения обработки.' : 'В этом запуске нет файлов для скачивания.'}</p>`}
      ${report ? `<h3 class="detail-section-title">Отчёт</h3><pre class="detail-report">${escape(report)}</pre>` : ''}`;
  }

  function newOperatorTemplate() {
    return `---\nid: new-operator\nname: Новый оператор\nkind: custom\nenabled: false\nschedule:\n  days: [tue, fri]\n  time: "08:45"\n  timezone: Europe/Moscow\n---\n\n# Новый процесс\n\n## Задача\nОпишите, какой результат нужен.\n\n## Источники\nУкажите сервисы и исходные файлы.\n\n## Результат\nУкажите нужные документы, названия файлов и проверки.\n\nДля выполнения этого нового вида процесса требуется разработать и\nподключить обработчик. Произвольный текст не исполняет действия.\n`;
  }

  hydrateIcons();
  function selectedKind(id) { return state.operators.find(o => o.id === $(id).value)?.kind || 'glopro'; }
  function updateImportPeriod() {
    const kind = selectedKind('import-operator');
    const period = periodForDate($('import-date').value, kind);
    $('import-period-preview').textContent = period ? periodLabel(period.start, period.end) : (kind === 'yandex' ? 'Выберите вторник.' : 'Выберите вторник или пятницу.');
  }
  function renderYandex() {
    const value = state.yandex || {};
    $('yandex-status').textContent = value.connecting ? 'Ожидает входа' : value.verified ? 'Вход сохранён' : 'Нужен вход';
    $('yandex-status').className = `badge badge-${value.verified ? 'success' : 'attention'}`;
    $('yandex-connect').disabled = !!value.connecting || isActive();
    $('yandex-result').hidden = !value.error && !value.connecting;
    $('yandex-result').className = `inline-message${value.error ? ' error' : ''}`;
    $('yandex-result').textContent = value.error || (value.connecting ? 'Завершите вход в отдельном окне Яндекса. Подключение обновится автоматически.' : '');
  }
  $('yandex-connect').addEventListener('click', async () => {
    $('yandex-connect').disabled = true;
    try {
      await api('/api/yandex/connect', {method: 'POST', body: '{}'});
      toast('Открывается отдельное окно входа в Яндекс.');
      await refreshState();
    } catch(error) { toast(error.message, true); $('yandex-connect').disabled = false; }
  });
  $('run-operator').addEventListener('change', () => { $('run-date').value = scheduledDate(selectedKind('run-operator')); updatePeriodPreview(); });
  $('import-operator').addEventListener('change', () => { $('import-date').value = scheduledDate(selectedKind('import-operator')); updateImportPeriod(); });
  $('import-date').addEventListener('change', updateImportPeriod);
  $('today').textContent = formatDate(new Date().toISOString(), { day: 'numeric', month: 'long', year: 'numeric' });
  navigate(window.location.hash.slice(1) || 'overview');
  window.addEventListener('hashchange', () => navigate(window.location.hash.slice(1)));
  window.addEventListener('beforeunload', (event) => { if (state.editorDirty) { event.preventDefault(); event.returnValue = ''; } });
  $('retry-state').addEventListener('click', refreshState);
  $('run-search').addEventListener('input', renderRuns);
  $('run-filter').addEventListener('change', renderRuns);
  $('run-date').addEventListener('change', updatePeriodPreview);

  document.addEventListener('click', (event) => {
    if (event.target.closest('.skip-link')) {
      event.preventDefault();
      $('main-content').focus();
      return;
    }
    const historyLink = event.target.closest('[data-history-filter]');
    if (historyLink) {
      $('run-filter').value = historyLink.dataset.historyFilter;
      $('run-search').value = '';
      renderRuns();
    }
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (button.dataset.close) $(button.dataset.close).close();
    if (button.dataset.action === 'manual-run') openManualRun();
    if (button.dataset.action === 'import') {
      if (isActive()) return toast('Дождитесь завершения текущего запуска.', true);
      $('import-operator').innerHTML = state.operators.filter(o => ['glopro', 'yandex'].includes(o.kind)).map(o => `<option value="${escape(o.id)}">${escape(o.name)}</option>`).join('');
      if (button.dataset.importOperator) $('import-operator').value = button.dataset.importOperator;
      $('import-date').value = scheduledDate(selectedKind('import-operator'));
      updateImportPeriod();
      $('import-form-error').hidden = true;
      $('import-dialog').showModal();
    }
    if (button.dataset.runOperator) openManualRun(button.dataset.runOperator);
    if (button.dataset.editOperator) {
      if (state.editorDirty && state.editorId !== button.dataset.editOperator && !window.confirm('Перейти к другой инструкции и отменить несохранённые изменения?')) return;
      const id = button.dataset.editOperator;
      window.location.hash = 'instructions';
      if (state.editorId !== id) loadEditor(id);
    }
    if (button.dataset.runDetails) { renderDetails(button.dataset.runDetails); $('details-dialog').showModal(); }
  });

  document.querySelectorAll('dialog').forEach((dialog) => {
    dialog.addEventListener('click', (event) => {
      if (event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    });
  });

  $('editor-operator').addEventListener('change', () => {
    if (state.editorDirty && !window.confirm('Отменить несохранённые изменения и открыть другую инструкцию?')) { $('editor-operator').value = state.editorId; return; }
    loadEditor($('editor-operator').value);
  });
  $('markdown-editor').addEventListener('input', () => {
    state.editorDirty = $('markdown-editor').value !== state.originalMarkdown;
    $('editor-save').disabled = !state.editorDirty || !state.editorId;
    $('editor-status').textContent = state.editorDirty ? 'Есть несохранённые изменения' : 'Нет несохранённых изменений';
  });
  $('editor-reset').addEventListener('click', () => {
    $('markdown-editor').value = state.originalMarkdown;
    state.editorDirty = false;
    $('editor-save').disabled = true;
    $('editor-status').textContent = 'Изменения отменены';
  });
  $('editor-save').addEventListener('click', async () => {
    if (!state.editorId || state.editorLoading) return;
    const markdown = $('markdown-editor').value;
    $('editor-save').disabled = true;
    $('editor-status').textContent = 'Проверяем и сохраняем…';
    try {
      await api(`/api/operators/${encodeURIComponent(state.editorId)}`, { method: 'PUT', body: JSON.stringify({ markdown }) });
      state.originalMarkdown = markdown;
      state.editorDirty = $('markdown-editor').value !== markdown;
      $('editor-status').textContent = state.editorDirty ? 'Есть несохранённые изменения' : 'Сохранено · применяется к следующим запускам';
      toast('Инструкция сохранена.');
      await refreshState();
    } catch (error) { $('editor-status').textContent = 'Не удалось сохранить'; toast(error.message, true); }
    finally { $('editor-save').disabled = !state.editorDirty; }
  });

  $('run-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (isActive()) return showFormError('run-form-error', 'Дождитесь завершения текущего запуска.');
    if (!periodForDate($('run-date').value, selectedKind('run-operator'))) return showFormError('run-form-error', 'Для Яндекса выберите вторник, для GloPro — вторник или пятницу.');
    const button = $('run-submit');
    button.disabled = true;
    state.pending = true;
    updateRunButtons();
    $('run-form-error').hidden = true;
    try {
      await api('/api/run', { method: 'POST', body: JSON.stringify({ operator_id: $('run-operator').value, run_date: $('run-date').value }) });
      $('run-dialog').close();
      toast('Запуск принят. Результат появится в истории.');
      window.location.hash = 'runs';
      await refreshState();
    } catch (error) { showFormError('run-form-error', error.message); }
    finally { state.pending = false; button.disabled = false; updateRunButtons(); }
  });

  const fileInput = $('import-files');
  function renderSelectedFiles() { $('selected-files').innerHTML = Array.from(fileInput.files || []).map((file) => `<li>${svg('document')}<span>${escape(file.name)}</span></li>`).join(''); }
  fileInput.addEventListener('change', renderSelectedFiles);
  $('import-drop-zone').addEventListener('dragover', (event) => { event.preventDefault(); $('import-drop-zone').classList.add('dragover'); });
  $('import-drop-zone').addEventListener('dragleave', () => $('import-drop-zone').classList.remove('dragover'));
  $('import-drop-zone').addEventListener('drop', (event) => { event.preventDefault(); $('import-drop-zone').classList.remove('dragover'); if (event.dataTransfer.files.length) { fileInput.files = event.dataTransfer.files; renderSelectedFiles(); } });
  $('import-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (isActive()) return showFormError('import-form-error', 'Дождитесь завершения текущего запуска.');
    if (!periodForDate($('import-date').value, selectedKind('import-operator'))) return showFormError('import-form-error', 'Для Яндекса выберите вторник, для GloPro — вторник или пятницу.');
    if (!fileInput.files.length) return showFormError('import-form-error', 'Добавьте хотя бы один файл Excel.');
    if (Array.from(fileInput.files).some((file) => !/\.xlsx$/i.test(file.name))) return showFormError('import-form-error', 'Можно загрузить только файлы .xlsx.');
    if (fileInput.files.length > 200) return showFormError('import-form-error', 'Можно загрузить не больше 200 файлов за один раз.');
    if (Array.from(fileInput.files).some((file) => file.size > 30000000)) return showFormError('import-form-error', 'Размер одного файла не должен превышать 30 МБ.');
    const form = new FormData();
    Array.from(fileInput.files).forEach((file) => form.append('files', file));
    form.append('run_date', $('import-date').value);
    form.append('operator_id', $('import-operator').value);
    $('import-submit').disabled = true;
    $('import-form-error').hidden = true;
    state.pending = true;
    updateRunButtons();
    try {
      await api('/api/import', { method: 'POST', body: form, timeout: 90000 });
      $('import-dialog').close();
      fileInput.value = '';
      renderSelectedFiles();
      toast('Файлы приняты. Расчёт появится в истории.');
      window.location.hash = 'runs';
      await refreshState();
    } catch (error) { showFormError('import-form-error', error.message); }
    finally { state.pending = false; $('import-submit').disabled = false; updateRunButtons(); }
  });

  $('connection-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('connection-save').disabled = true;
    $('connection-result').hidden = true;
    try {
      await api('/api/connection', { method: 'POST', body: JSON.stringify({ username: $('connection-username').value, password: $('connection-password').value }) });
      $('connection-password').value = '';
      $('connection-username').value = '';
      $('connection-result').className = 'inline-message';
      $('connection-result').textContent = 'Данные доступа сохранены. Нажмите «Проверить вход», чтобы проверить подключение.';
      $('connection-result').hidden = false;
      toast('Данные доступа сохранены.');
      await refreshState();
    } catch (error) { $('connection-result').className = 'inline-message error'; showFormError('connection-result', error.message); }
    finally { $('connection-save').disabled = false; }
  });
  $('connection-test').addEventListener('click', async () => {
    $('connection-test').disabled = true;
    $('connection-test').textContent = 'Проверяем…';
    $('connection-result').hidden = true;
    try {
      const result = await api('/api/connection/test', { method: 'POST', body: '{}', timeout: 90000 });
      if (result.verified === false || result.success === false || result.ok === false) throw new Error(result.error || result.message || 'Не удалось подтвердить вход в GloPro.');
      await refreshState();
      const verified = state.connection.verified || result.verified === true || result.success === true || result.ok === true;
      if (!verified) throw new Error('Сервер не подтвердил успешный вход. Проверьте состояние подключения.');
      $('connection-result').className = 'inline-message';
      $('connection-result').textContent = typeof result.message === 'string' ? result.message : 'Вход в GloPro успешно проверен.';
      $('connection-result').hidden = false;
      toast('Подключение GloPro проверено.');
    } catch (error) { $('connection-result').className = 'inline-message error'; showFormError('connection-result', error.message); }
    finally { $('connection-test').textContent = 'Проверить вход'; $('connection-test').disabled = !state.connection.configured; }
  });

  $('create-operator-button').addEventListener('click', () => {
    $('create-id').value = '';
    $('create-markdown').value = newOperatorTemplate();
    $('create-form-error').hidden = true;
    $('create-dialog').showModal();
  });
  $('create-id').addEventListener('input', () => {
    const id = $('create-id').value;
    if (/^[a-z][a-z0-9_-]{0,59}$/.test(id)) $('create-markdown').value = $('create-markdown').value.replace(/^id:\s*.*$/m, `id: ${id}`);
  });
  $('create-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('create-submit').disabled = true;
    $('create-form-error').hidden = true;
    try {
      await api('/api/operators', { method: 'POST', body: JSON.stringify({ id: $('create-id').value, markdown: $('create-markdown').value }) });
      $('create-dialog').close();
      toast('Оператор создан. Для нового вида процесса потребуется обработчик.');
      await refreshState();
    } catch (error) { showFormError('create-form-error', error.message); }
    finally { $('create-submit').disabled = false; }
  });

  updateRunButtons();
  refreshState();
  window.setInterval(() => { if (!document.hidden) refreshState(); }, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshState(); });
})();
