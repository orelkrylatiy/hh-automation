(() => {
  'use strict';

  const OPS = '/api/ops';
  let opsRefreshTimer = null;

  function profile() {
    try {
      return typeof currentProfile !== 'undefined' ? currentProfile : 'default';
    } catch (_) {
      return 'default';
    }
  }

  function singleAccount(action) {
    if (typeof requireSingleAccount === 'function') return requireSingleAccount(action);
    return true;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({detail: response.statusText}));
    if (!response.ok) throw new Error(data.detail || response.statusText);
    return data;
  }

  function statusClass(ok) {
    return ok ? 'state state-invitation' : 'state state-discard';
  }

  function modeLabel(mode) {
    if (mode === 'live') return 'LIVE';
    if (mode === 'dry-run') return 'DRY-RUN';
    return 'UTILITY';
  }

  function resultLabel(item) {
    if (item.running) return '<span class="state state-response">Выполняется</span>';
    if (item.cancelled) return '<span class="state state-default">Отменена</span>';
    if (Number(item.returncode) === 0) return '<span class="state state-invitation">Успешно</span>';
    return '<span class="state state-discard">Ошибка</span>';
  }

  function safeTime(value) {
    if (!value) return '—';
    try {
      return typeof timeAgo === 'function' ? timeAgo(value) : new Date(value).toLocaleString('ru-RU');
    } catch (_) {
      return value;
    }
  }

  function renderLogText(box, text) {
    if (!box) return;
    box.innerHTML = '';
    const lines = String(text || '').split(/(?<=\n)/);
    if (!text) {
      box.textContent = '(лог пока пуст)';
      return;
    }
    lines.forEach(line => {
      const span = document.createElement('span');
      if (/error|exception|traceback|failed|status=[1-9]/i.test(line)) span.className = 'log-error';
      else if (/warn|fallback|skip/i.test(line)) span.className = 'log-warn';
      else if (/info|success|успеш|HH_RUN_START|HH_RUN_END/i.test(line)) span.className = 'log-info';
      span.textContent = line;
      box.appendChild(span);
    });
    box.scrollTop = box.scrollHeight;
  }

  async function loadSourceLog(source = 'apply') {
    const box = document.getElementById('ops-log-box');
    const meta = document.getElementById('ops-log-meta');
    if (!box || !meta) return;
    meta.textContent = 'Загрузка…';
    try {
      const data = await request(`${OPS}/logs?profile=${encodeURIComponent(profile())}&source=${encodeURIComponent(source)}&lines=500`);
      renderLogText(box, (data.lines || []).join(''));
      meta.textContent = `${data.label} · ${data.exists ? `обновлён ${safeTime(data.updated_at)}` : 'файл ещё не создан'} · ${data.lines.length} строк`;
    } catch (error) {
      meta.textContent = 'Ошибка загрузки';
      box.textContent = `Ошибка: ${error.message}`;
    }
  }

  async function loadLogSources() {
    const select = document.getElementById('ops-log-source');
    if (!select) return;
    try {
      const data = await request(`${OPS}/log-sources?profile=${encodeURIComponent(profile())}`);
      const current = select.value || 'apply';
      select.innerHTML = (data.sources || []).map(item =>
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}${item.exists ? '' : ' · нет файла'}</option>`
      ).join('');
      if ([...select.options].some(option => option.value === current)) select.value = current;
      await loadSourceLog(select.value || 'apply');
    } catch (error) {
      select.innerHTML = '<option value="apply">Логи недоступны</option>';
    }
  }

  async function trackOperation(opId, outputId) {
    const output = document.getElementById(outputId);
    let failures = 0;
    while (true) {
      try {
        const data = await request(`${OPS}/status/${encodeURIComponent(opId)}?profile=${encodeURIComponent(profile())}&lines=700`);
        failures = 0;
        if (output) {
          output.style.display = 'block';
          const header = [
            data.command_preview ? `$ ${data.command_preview}` : '',
            `mode=${data.mode || '—'}  id=${opId}`,
            data.running ? 'status=running' : `status=finished  exit=${data.returncode}`,
          ].filter(Boolean).join('\n');
          const tail = data.log_text || data.stdout || data.stderr || '';
          output.textContent = `${header}\n\n${tail || '(ожидаем вывод...)'}`;
          output.scrollTop = output.scrollHeight;
        }
        if (!data.running) {
          if (Number(data.returncode) === 0) toast('Операция завершена успешно');
          else toast(`Операция завершена с кодом ${data.returncode}`, 'error');
          if (typeof window.run === 'function' && typeof currentPage !== 'undefined' && currentPage === 'run') {
            setTimeout(() => window.run(), 500);
          }
          return data;
        }
      } catch (error) {
        failures += 1;
        if (failures >= 8) {
          if (output) output.textContent += `\n\nПотерян контакт с сервером: ${error.message}`;
          toast('Потерян контакт с операцией', 'error');
          return null;
        }
      }
      await new Promise(resolve => setTimeout(resolve, 900));
    }
  }

  async function startOperation(kind, payload, outputId) {
    if (!singleAccount('Запуск операций')) return null;
    const output = document.getElementById(outputId);
    if (output) {
      output.style.display = 'block';
      output.textContent = 'Запуск production wrapper…';
    }
    try {
      const data = await request(`${OPS}/run/${kind}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      if (output) output.textContent = `$ ${data.command_preview}\n\nОперация ${data.op_id} запущена…`;
      toast(`${kind}: запущено`);
      trackOperation(data.op_id, outputId);
      return data;
    } catch (error) {
      if (output) output.textContent = `Ошибка: ${error.message}`;
      toast(error.message, 'error');
      return null;
    }
  }

  async function loadRunData() {
    const overviewBox = document.getElementById('ops-overview');
    const activeBox = document.getElementById('ops-active');
    const historyBody = document.getElementById('ops-history-body');
    try {
      const [overview, operations, history] = await Promise.all([
        request(`${OPS}/overview?profile=${encodeURIComponent(profile())}`),
        request(`${OPS}/operations?profile=${encodeURIComponent(profile())}`),
        request(`/api/operations/history?profile=${encodeURIComponent(profile())}&scope=profile&limit=15`),
      ]);

      if (overviewBox) {
        const live = overview.live_hh || {};
        const token = overview.token || {};
        const snapshot = overview.local_snapshot_at;
        const defs = overview.defaults || {};
        overviewBox.innerHTML = `
          <div class="ops-fact"><span>HH live</span><strong class="${statusClass(live.reachable)}">${live.reachable ? 'Доступен' : 'Ошибка'}</strong><small>${escapeHtml(safeTime(live.checked_at))}</small></div>
          <div class="ops-fact"><span>Активные переговоры</span><strong>${live.active_negotiations ?? '—'}</strong><small>из HH API прямо сейчас</small></div>
          <div class="ops-fact"><span>Резюме</span><strong>${live.resumes ?? '—'}</strong><small>из HH API прямо сейчас</small></div>
          <div class="ops-fact"><span>Локальный снимок</span><strong>${snapshot ? escapeHtml(safeTime(snapshot)) : 'Нет'}</strong><small>дашборд/таблицы SQLite</small></div>
          <div class="ops-fact"><span>Токен</span><strong>${escapeHtml(token.status || '—')}</strong><small>${token.expires_in_seconds == null ? '' : `ещё ${Math.max(0, Math.round(token.expires_in_seconds / 3600))} ч`}</small></div>
          <div class="ops-fact"><span>Production safety</span><strong>apply-safe</strong><small>${escapeHtml(defs.hard_filter || '')}</small></div>`;
      }

      const active = (operations.operations || []).filter(item => item.running);
      if (activeBox) {
        activeBox.innerHTML = active.length ? active.map(item => `
          <div class="ops-active-row">
            <div><strong>${escapeHtml(item.operation)}</strong> <span class="state state-response">${escapeHtml(modeLabel(item.mode))}</span><div class="ops-command">${escapeHtml(item.command_preview || '')}</div></div>
            <div class="ops-active-actions"><span>${escapeHtml(safeTime(item.started_at))}</span><button class="btn btn-danger" onclick="opsCancel('${escapeHtml(item.op_id)}')">Отменить</button></div>
          </div>`).join('') : '<div class="admin-empty-state">Сейчас ничего не выполняется</div>';
      }

      if (historyBody) {
        historyBody.innerHTML = (history.operations || []).map(item => `<tr>
          <td>${escapeHtml(item.operation || '—')}</td>
          <td>${escapeHtml(modeLabel(item.mode || (item.dry_run ? 'dry-run' : 'live')))}</td>
          <td>${resultLabel(item)}</td>
          <td>${escapeHtml(safeTime(item.finished_at || item.started_at))}</td>
          <td class="ops-command">${escapeHtml(item.command_preview || 'старый запуск без command audit')}</td>
          <td>${item.log_source ? `<button class="btn btn-secondary" onclick="opsShowRunLog('${escapeHtml(item.op_id)}')">Лог</button>` : '—'}</td>
        </tr>`).join('') || '<tr class="empty-row"><td colspan="6">Запусков пока нет</td></tr>';
      }
    } catch (error) {
      if (overviewBox) overviewBox.innerHTML = `<div class="admin-error-state">Ошибка runtime status: ${escapeHtml(error.message)}</div>`;
    }
  }

  async function runPage() {
    if (!singleAccount('Операционная консоль')) return;
    clearTimeout(opsRefreshTimer);
    const content = document.getElementById('content');
    if (!content) return;

    let defaults = {};
    try {
      defaults = await request(`${OPS}/defaults?profile=${encodeURIComponent(profile())}`);
    } catch (_) {}

    content.innerHTML = `
      <div class="ops-source-banner"><strong>Production console</strong><span>Кнопки ниже запускают те же <code>scripts/all-profiles.sh → apply.sh/reply.sh</code>, что cron. Произвольный shell намеренно не доступен.</span></div>
      <div class="section"><div class="section-title">Состояние прямо сейчас</div><div class="ops-facts" id="ops-overview"><div class="admin-loading-state">Проверяем HH и runtime…</div></div></div>
      <div class="run-grid">
        <div class="run-card"><div class="run-card-header"><h3>🎯 Автоотклики</h3><span class="state state-invitation">apply-safe</span></div>
          <p>Hard-filter + AI-сопроводительное + static fallback + force-message + skip-tests + timeout.</p>
          <div class="ops-mini-meta">Поиск: <strong>${escapeHtml(defaults.search || 'Frontend разработчик')}</strong> · лимит: ${defaults.max_responses || 100}</div>
          <div class="run-actions"><button class="btn btn-primary" onclick="goPage('apply-form')">Настроить и запустить</button></div>
        </div>
        <div class="run-card"><div class="run-card-header"><h3>💬 Автоответы</h3><span class="state state-invitation">reply.sh</span></div>
          <p>Тот же безопасный reply worker, который использует cron. Dry-run не вызывает LLM и ничего не отправляет.</p>
          <div class="form-group"><label>Максимум чатов</label><input id="ops-reply-chats" type="number" min="1" max="1000" value="${Number(defaults.max_chats) || 100}"></div>
          <div class="run-actions"><button class="btn btn-secondary" onclick="opsRunReply(true)">Dry-run</button><button class="btn btn-danger" onclick="opsRunReply(false)">LIVE</button></div>
          <pre class="run-output" id="ops-reply-output"></pre>
        </div>
        <div class="run-card"><div class="run-card-header"><h3>📤 Обновить резюме</h3><span class="state state-response">LIVE</span></div>
          <p>Запускается через общий per-profile flock, поэтому не пересечётся с cron/apply/reply этого аккаунта.</p>
          <div class="run-actions"><button class="btn btn-danger" onclick="opsRunUtility('update')">Обновить</button></div>
          <pre class="run-output" id="ops-update-output"></pre>
        </div>
        <div class="run-card"><div class="run-card-header"><h3>🔁 Полный проход</h3><span class="state state-default">daily.sh</span></div>
          <p>Один проход: отклики, затем ответы. Это production workflow, а не отдельная логика админки.</p>
          <div class="run-actions"><button class="btn btn-secondary" onclick="opsRunDaily(true)">Dry-run</button><button class="btn btn-danger" onclick="opsRunDaily(false)">LIVE</button></div>
          <pre class="run-output" id="ops-daily-output"></pre>
        </div>
        <div class="run-card"><div class="run-card-header"><h3>🔑 Обновить токен</h3><span class="state state-default">utility</span></div>
          <p>Запускает тот же <code>refresh-token</code> через общий профильный wrapper.</p>
          <div class="run-actions"><button class="btn btn-secondary" onclick="opsRunUtility('refresh')">Refresh token</button></div>
          <pre class="run-output" id="ops-refresh-output"></pre>
        </div>
      </div>
      <div class="section"><div class="section-title">Активные операции</div><div id="ops-active" class="ops-active-list"><div class="admin-loading-state">Загрузка…</div></div></div>
      <div class="section"><div class="section-title">Runtime-логи</div>
        <div class="filters"><select id="ops-log-source" onchange="opsLoadLog(this.value)"></select><button class="btn btn-secondary" onclick="opsLoadLog(document.getElementById('ops-log-source').value)">Обновить</button></div>
        <div id="ops-log-meta" class="ops-log-meta">Загрузка…</div><div class="log-box" id="ops-log-box"></div>
      </div>
      <div class="section"><div class="section-title">История запусков</div><div class="table-wrap"><table><thead><tr><th>Операция</th><th>Режим</th><th>Результат</th><th>Время</th><th>Команда</th><th></th></tr></thead><tbody id="ops-history-body"></tbody></table></div></div>`;

    await Promise.all([loadRunData(), loadLogSources()]);
    opsRefreshTimer = setTimeout(() => {
      try {
        if (typeof currentPage !== 'undefined' && currentPage === 'run') loadRunData();
      } catch (_) {}
    }, 5000);
  }

  async function applyPage() {
    if (!singleAccount('Отклики')) return;
    const content = document.getElementById('content');
    if (!content) return;
    content.innerHTML = '<div class="admin-loading-state">Загрузка production-конфигурации…</div>';

    try {
      const [defaults, resumes] = await Promise.all([
        request(`${OPS}/defaults?profile=${encodeURIComponent(profile())}`),
        request(`/api/resumes?profile=${encodeURIComponent(profile())}&scope=profile`),
      ]);
      const options = ['<option value="">Автовыбор как в cron</option>'].concat(
        (resumes.items || []).map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title || item.id)}</option>`)
      ).join('');
      const fallback = defaults.cover_letter_fallback || {};
      const ai = defaults.cover_letter_ai || {};
      content.innerHTML = `
        <div class="ops-source-banner"><strong>Один путь выполнения</strong><span>UI → <code>all-profiles.sh apply</code> → <code>apply.sh</code> → <code>apply-safe</code>. Это тот же путь, что cron.</span></div>
        <div class="ops-safety-grid">
          <div><span>Hard filter</span><strong>${escapeHtml(defaults.hard_filter || '—')}</strong></div>
          <div><span>Cover prompt</span><strong>${escapeHtml(defaults.cover_prompt || '—')}</strong></div>
          <div><span>AI</span><strong>${ai.configured ? escapeHtml(ai.model || 'настроен') : 'не настроен'}</strong></div>
          <div><span>Fallback</span><strong>${fallback.enabled ? (fallback.configured ? 'config message' : 'legacy template') : 'выключен'}</strong></div>
          <div><span>Письмо</span><strong>обязательно</strong></div><div><span>Тестовые задания</span><strong>пропускаются</strong></div>
        </div>
        <div class="letter-form ops-canonical-form">
          <div class="form-row"><div class="form-group"><label>Поиск</label><input id="ops-apply-search" value="${escapeHtml(defaults.search || '')}"></div><div class="form-group"><label>Резюме</label><select id="ops-apply-resume">${options}</select></div></div>
          <div class="form-row"><div class="form-group"><label>Максимум успешных откликов</label><input id="ops-apply-limit" type="number" min="1" max="1000" value="${Number(defaults.max_responses) || 100}"></div><div class="form-group"><label>Задержка, сек</label><input id="ops-apply-delay" value="${escapeHtml(defaults.response_delay || '1-3')}"></div></div>
          <div class="form-row"><div class="form-group"><label>Страниц поиска</label><input id="ops-apply-pages" type="number" min="1" max="100" value="${Number(defaults.total_pages) || 20}"></div><div class="form-group"><label>Вакансий на страницу</label><input id="ops-apply-perpage" type="number" min="1" max="100" value="${Number(defaults.per_page) || 50}"></div></div>
          <div class="form-row"><div class="form-group"><label>Timeout всего запуска, сек</label><input id="ops-apply-timeout" type="number" min="30" max="14400" value="${Number(defaults.timeout) || 3600}"></div></div>
          <div class="run-actions"><button class="btn btn-secondary" onclick="opsSubmitApply(true)">🧪 Dry-run</button><button class="btn btn-danger" onclick="opsSubmitApply(false)">🚀 LIVE</button></div>
          <pre class="run-output" id="ops-apply-output"></pre>
        </div>`;
    } catch (error) {
      content.innerHTML = `<div class="admin-error-state">Ошибка: ${escapeHtml(error.message)}</div>`;
    }
  }

  async function logsPage() {
    if (!singleAccount('Логи')) return;
    const content = document.getElementById('content');
    content.innerHTML = `
      <div class="ops-source-banner"><strong>Не один log.txt</strong><span>Здесь доступны CLI, apply/reply/daily wrapper logs, cron и ops report. Это реальные файлы runtime.</span></div>
      <div class="filters"><select id="ops-log-source" onchange="opsLoadLog(this.value)"></select><button class="btn btn-secondary" onclick="opsLoadLog(document.getElementById('ops-log-source').value)">Обновить</button></div>
      <div id="ops-log-meta" class="ops-log-meta">Загрузка…</div><div class="log-box" id="ops-log-box"></div>`;
    await loadLogSources();
  }

  async function enhanceDashboardWithLiveSource(originalDashboard, args) {
    await originalDashboard(...args);
    if (typeof viewScope !== 'undefined' && viewScope === 'all') return;
    const content = document.getElementById('content');
    if (!content) return;
    const strip = document.createElement('div');
    strip.className = 'ops-live-strip';
    strip.innerHTML = '<span class="admin-loading-state">Сверяем локальный снимок с live HH…</span>';
    content.prepend(strip);
    try {
      const overview = await request(`${OPS}/overview?profile=${encodeURIComponent(profile())}`);
      const live = overview.live_hh || {};
      strip.innerHTML = `<strong>Источник данных:</strong><span>таблицы ниже = локальный SQLite снимок (${escapeHtml(safeTime(overview.local_snapshot_at))})</span><span class="${statusClass(live.reachable)}">HH live ${live.reachable ? 'доступен' : 'ошибка'}</span><span>проверено ${escapeHtml(safeTime(live.checked_at))}</span>`;
    } catch (error) {
      strip.innerHTML = `<strong>Источник данных:</strong><span>локальный SQLite снимок</span><span class="state state-discard">Live HH не проверен</span>`;
    }
  }

  function install() {
    window.opsLoadLog = loadSourceLog;
    window.opsShowRunLog = async opId => {
      const select = document.getElementById('ops-log-source');
      const box = document.getElementById('ops-log-box');
      const meta = document.getElementById('ops-log-meta');
      if (!box || !meta) return;
      try {
        const data = await request(`${OPS}/status/${encodeURIComponent(opId)}?profile=${encodeURIComponent(profile())}&lines=1200`);
        renderLogText(box, data.log_text || data.stdout || data.stderr || '');
        meta.textContent = `Запуск ${opId} · ${data.command_preview || data.operation || ''}`;
        box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
        if (select && data.log_source) select.value = data.log_source;
      } catch (error) { toast(error.message, 'error'); }
    };
    window.opsCancel = async opId => {
      if (!confirm(`Отменить операцию ${opId}?`)) return;
      try {
        await request(`${OPS}/cancel/${encodeURIComponent(opId)}?profile=${encodeURIComponent(profile())}`, {method: 'POST'});
        toast('Отмена запрошена');
        await loadRunData();
      } catch (error) { toast(error.message, 'error'); }
    };
    window.opsRunReply = async dryRun => {
      const maxChats = Number.parseInt(document.getElementById('ops-reply-chats')?.value || '100', 10) || 100;
      if (!dryRun && !confirm(`Отправлять реальные ответы с аккаунта "${profile()}"?`)) return;
      await startOperation('reply', {profile: profile(), dry_run: dryRun, confirm_live: !dryRun, max_chats: maxChats}, 'ops-reply-output');
    };
    window.opsRunDaily = async dryRun => {
      if (!dryRun && !confirm(`Запустить полный LIVE-проход apply + reply для "${profile()}"?`)) return;
      let defaults = {};
      try { defaults = await request(`${OPS}/defaults?profile=${encodeURIComponent(profile())}`); } catch (_) {}
      await startOperation('daily', {
        profile: profile(), dry_run: dryRun, confirm_live: !dryRun,
        search: defaults.search || '', max_responses: defaults.max_responses || 100,
        per_page: defaults.per_page || 50, total_pages: defaults.total_pages || 20,
        max_chats: defaults.max_chats || 100,
      }, 'ops-daily-output');
    };
    window.opsRunUtility = async kind => {
      const live = kind === 'update' || kind === 'boost';
      if (live && !confirm(`Выполнить ${kind} для аккаунта "${profile()}"?`)) return;
      await startOperation(kind, {profile: profile(), confirm_live: live}, `ops-${kind}-output`);
    };
    window.opsSubmitApply = async dryRun => {
      const limit = Number.parseInt(document.getElementById('ops-apply-limit')?.value || '100', 10) || 100;
      if (!dryRun && !confirm(`Отправить до ${limit} реальных откликов с аккаунта "${profile()}" через production apply-safe?`)) return;
      const payload = {
        profile: profile(), dry_run: dryRun, confirm_live: !dryRun,
        search: document.getElementById('ops-apply-search')?.value.trim() || '',
        resume_id: document.getElementById('ops-apply-resume')?.value || '',
        max_responses: limit,
        response_delay: document.getElementById('ops-apply-delay')?.value.trim() || '1-3',
        total_pages: Number.parseInt(document.getElementById('ops-apply-pages')?.value || '20', 10) || 20,
        per_page: Number.parseInt(document.getElementById('ops-apply-perpage')?.value || '50', 10) || 50,
        timeout: Number.parseInt(document.getElementById('ops-apply-timeout')?.value || '3600', 10) || 3600,
      };
      await startOperation('apply', payload, 'ops-apply-output');
    };

    window.run = runPage;
    window.applyForm = applyPage;
    window.submitApplyForm = dryRun => window.opsSubmitApply(Boolean(dryRun));
    window.logs = logsPage;
    window.runReplyEmployers = dryRun => window.opsRunReply(Boolean(dryRun));

    const originalDashboard = window.dashboard;
    if (typeof originalDashboard === 'function' && !originalDashboard.__opsSourceWrapped) {
      const wrapped = (...args) => enhanceDashboardWithLiveSource(originalDashboard, args);
      wrapped.__opsSourceWrapped = true;
      window.dashboard = wrapped;
    }

    // init() ran in the legacy inline script before deferred enhancement assets.
    // If it restored one of the operational pages, redraw it using the canonical UI.
    try {
      if (typeof currentPage !== 'undefined' && currentPage === 'run') window.run();
      if (typeof currentPage !== 'undefined' && currentPage === 'apply-form') window.applyForm();
    } catch (_) {}
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, {once: true});
  else install();
})();
