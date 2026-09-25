'use strict';

/* Offline presentation and optional data-source features. All content is textContent. */
window.ZhixianExperience = Object.freeze({
  mount(api) {
    const { $, el, append, icon, button, iconButton, notice, empty, heading, tag, rpc, sync, setState, getState, getPage, go, render, toast, errorText, formatTime, valueText, available, showDialog, manualDialog, field, demo } = api;
    const catalog = { items: [], query: '', filter: 'all', cursor: null, more: false, total: null, busy: false, available: false, source: '', serial: 0, onSelect: null, multi: false, mode: '', selected: new Map() };
    const moments = { items: [], cursor: null, more: false, loaded: false, busy: false, available: false, source: '', detail: '', error: '', friend: null, results: new Map(), running: new Set(), serial: 0 };
    const histories = new Map(), momentMedia = new Map();
    let appearance = { theme: 'night', font_scale: 1, background_url: '' }, introTimer, searchTimer, importBusy = false, catchupSessionId = '', catchupBusy = false;
    const agent = { selected: [], result: null, busy: false, error: '', progress: '' };

    function safeMedia(value) {
      if (typeof value !== 'string' || !value || value.length > 28 * 1024 * 1024) return '';
      if (/^data:image\/(png|jpe?g|webp|gif);base64,[a-z0-9+/=\s]+$/i.test(value)) return value;
      try {
        const url = new URL(value, location.href);
        if (url.username || url.password) return '';
        if (url.protocol === 'file:' && location.protocol === 'file:') return url.href;
        if (url.protocol === location.protocol && url.origin === location.origin && /\.(png|jpe?g|webp|gif|svg)(?:$|\?)/i.test(url.pathname)) return url.href;
      } catch { /* Unsupported media stays a visible placeholder. */ }
      return '';
    }

    function updateState(state) {
      const next = state.appearance || {};
      const theme = next.theme === 'paper' ? 'paper' : 'night';
      const scale = Number(next.font_scale || appearance.font_scale || 1);
      const normalized = Math.max(90, Math.min(130, Math.round((scale > 3 ? scale : scale * 100) / 10) * 10));
      const background = next.background_url !== undefined ? next.background_url : appearance.background_url;
      appearance = { ...appearance, ...next, theme, font_scale: normalized / 100, background_url: background || '' };
      document.documentElement.dataset.theme = theme;
      document.documentElement.dataset.fontScale = String(normalized);
      document.documentElement.dataset.capture = state.status?.capture || 'idle';
      const image = $('#scene-background'), source = safeMedia(appearance.background_url);
      if (image.dataset.source !== source) { image.dataset.source = source; image.hidden = !source; if (source) image.src = source; else image.removeAttribute('src'); }
      document.documentElement.classList.toggle('has-scene-background', Boolean(source));
      const progress = state.catalog?.progress;
      if (importBusy && progress) {
        const target = $('.catalog-import-status');
        if (target) target.textContent = `正在索引 ${Number(progress.sessions || 0)} 个会话、${Number(progress.messages || 0)} 条消息；可关闭窗口继续使用。`;
      }
      const incoming = state.catalog?.preview_updates;
      const updates = Array.isArray(incoming) ? incoming : incoming && typeof incoming === 'object' ? Object.entries(incoming).map(([id, item]) => ({ id, ...item })) : [];
      if ($('#session-catalog').open && updates.length) {
        const rows = [...$('#session-catalog').querySelectorAll('.catalog-row')];
        for (const update of updates) {
          const item = catalog.items.find(item => item.id === update.id); if (!item) continue;
          Object.assign(item, update);
          const row = rows.find(row => row.dataset.sessionId === String(update.id)); if (!row) continue;
          $('.catalog-preview', row).textContent = previewText(item);
          const name = $('.catalog-title>span:first-child', row); if (name) name.textContent = displayName(item);
          const time = $('time', row); if (time) time.textContent = formatTime(item.updated || item.timestamp);
          row.classList.toggle('preview-pending', item.preview_status === 'pending');
        }
      }
    }

    function dismissIntro() {
      clearTimeout(introTimer);
      const intro = $('#cinema-intro');
      if (intro) { intro.classList.add('intro-leaving'); setTimeout(() => intro.remove(), 300); }
      document.documentElement.classList.remove('intro-active');
    }
    function startIntro(force = false) {
      if (!force && (new URLSearchParams(location.search).get('intro') === '0' || matchMedia('(prefers-reduced-motion: reduce)').matches)) return;
      $('#cinema-intro')?.remove(); clearTimeout(introTimer);
      const intro = el('section', 'cinema-intro'); intro.id = 'cinema-intro'; intro.setAttribute('aria-label', '知弦开场');
      const ambience = el('div', 'intro-ambience'); ambience.setAttribute('aria-hidden', 'true');
      for (let i = 0; i < 4; i++) ambience.append(el('i', `intro-orbit orbit-${i}`));
      const strings = el('div', 'intro-strings'); strings.setAttribute('aria-hidden', 'true');
      for (let i = 0; i < 7; i++) strings.append(el('i', `intro-string string-${i}`));
      const glyph = el('div', 'intro-glyph'); for (let i = 0; i < 3; i++) glyph.append(el('i'));
      const title = append(el('div', 'intro-title'), el('span', '', '知'), el('span', '', '弦'));
      const skip = button('跳过开场', 'arrow', dismissIntro, 'intro-skip'); skip.id = 'intro-skip';
      append(intro, ambience, strings, el('div', 'intro-corner corner-top'), el('div', 'intro-corner corner-bottom'), append(el('div', 'intro-center'), glyph, el('p', 'intro-kicker', 'ZHIXIAN / DESKTOP COMPANION'), title, el('p', 'intro-subtitle', '听见话语，也听见弦外之音。')), append(el('div', 'intro-bottom'), el('span', '', '进入你的对话空间'), el('div', 'intro-progress'), el('span', '', '01 / 知弦')), skip);
      document.body.append(intro); document.documentElement.classList.add('intro-active');
      introTimer = setTimeout(dismissIntro, matchMedia('(prefers-reduced-motion: reduce)').matches ? 300 : 3800);
      skip.focus({ preventScroll: true });
    }

    function messageKind(message) {
      if (['image', 'voice'].includes(message.kind)) return message.kind;
      if (message.transcript) return 'voice';
      if (message.image_description) return 'image';
      if (Array.isArray(message.media)) { const asset = message.media.find(asset => ['image', 'voice', 'audio'].includes(asset.kind)); if (asset) return asset.kind === 'audio' ? 'voice' : asset.kind; }
      const text = String(message.text || '');
      if (/^\[(图片|image)\]$/i.test(text)) return 'image';
      if (/^\[(语音|voice)\]/i.test(text)) return 'voice';
      return 'text';
    }
    function previewText(session) {
      if (session.preview_status === 'pending') return '正在补齐最近一条消息…';
      const last = session.last_message;
      const raw = typeof last === 'object' && last ? last.text : session.preview || last || '';
      const kind = typeof last === 'object' && last ? last.kind : session.kind || session.last_message_kind;
      const prefix = kind === 'image' ? '[图片]' : kind === 'voice' ? '[语音]' : '';
      if (prefix && String(raw).startsWith(prefix)) return String(raw);
      return `${prefix}${prefix && raw ? ' ' : ''}${raw || (prefix ? '' : '尚无可读的消息预览')}`;
    }
    function displayName(item) { return String(item.title || item.name || item.display_name || item.remark || '未命名会话'); }
    function catalogSourceName(source) { return ({ wechat_db: 'CipherTalk 本机数据库', weflow: 'WeFlow 本地接口', ocr: '微信可见窗口', import: '已导入记录', manual: '手动提供对话', demo: '合成演示数据', collected: '已采集会话' })[source] || '本地会话'; }

    function openCatalog(onSelect = null, options = {}) {
      const dialog = $('#session-catalog');
      if (dialog.open) { $('#session-search')?.focus({ preventScroll: true }); return; }
      catalog.serial++; catalog.items = []; catalog.query = ''; catalog.filter = 'all'; catalog.cursor = null; catalog.more = false; catalog.onSelect = onSelect; catalog.busy = false;
      catalog.multi = Boolean(options.multi); catalog.mode = options.mode || ''; catalog.selected = new Map((options.selected || []).map(item => [String(item.session_id), { session_id: String(item.session_id), title: item.title, is_group: Boolean(item.is_group), type: item.type || (item.is_group ? 'group' : 'private'), source: item.source }]));
      dialog.replaceChildren();
      const input = el('input', 'input'); input.id = 'session-search'; input.type = 'search'; input.placeholder = '搜索好友、群聊或会话名称'; input.autocomplete = 'off'; input.setAttribute('aria-label', input.placeholder);
      input.addEventListener('input', () => { clearTimeout(searchTimer); catalog.query = input.value; catalog.serial++; catalog.busy = false; searchTimer = setTimeout(() => fetchCatalog(true), 220); });
      const filters = el('div', 'catalog-filters');
      for (const [value, label] of [['all', '全部可用'], ['wechat_db', '本机微信数据库 · CipherTalk'], ['weflow', 'WeFlow'], ['import', '已导入'], ['collected', '已采集']]) {
        const filter = el('button', `catalog-filter${value === 'all' ? ' active' : ''}`, label); filter.type = 'button'; filter.dataset.filter = value;
        filter.addEventListener('click', () => { catalog.filter = value; catalog.serial++; catalog.busy = false; filters.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === filter)); fetchCatalog(true); }); filters.append(filter);
      }
      const isAgent = catalog.mode === 'agent';
      append(dialog,
        append(el('div', 'catalog-heading'), append(el('div'), el('div', 'eyebrow', isAgent ? '知弦 Agent · 分析范围' : catalog.multi ? '自动回复范围' : onSelect ? '选择联系人' : '你的对话空间'), el('h2', '', isAgent ? '选择最多 3 段对话' : catalog.multi ? '选择允许自动回复的会话' : onSelect ? '选择好友' : '全局会话')), iconButton('关闭会话窗口', 'close', () => dialog.close())),
        append(el('div', 'catalog-search'), icon('search', 18), input),
        filters, el('div', 'catalog-source'), el('div', 'catalog-list'), el('div', 'catalog-pagination'),
        append(el('div', 'catalog-footer'), el('span', 'catalog-import-status', isAgent ? '可选择任意来源，包括导入、手动提供和历史会话；仅分析并给出建议，不会发送。' : catalog.multi ? '只会列出已核验可实时接收的会话；归档记录不支持自动发送。' : importBusy ? '正在本机索引，可关闭此窗口继续使用' : '支持 CipherTalk / ChatLab 导出的 JSON、JSONL'), catalog.multi ? append(el('div', 'catalog-import-actions'), button('取消', null, () => dialog.close(), 'small subtle'), button('应用选择', 'check', async () => { try { await catalog.onSelect?.([...catalog.selected.values()]); dialog.close(); } catch (err) { toast(errorText(err), 'error'); } }, 'small primary')) : append(el('div', 'catalog-import-actions'), button('粘贴对话', 'pen', () => { dialog.close(); manualDialog(); }, 'small ghost'), importButton('files', '导入文件', 'upload'), importButton('folder', '导入目录', 'folder'))));
      dialog.showModal(); input.focus({ preventScroll: true }); fetchCatalog(true);
    }
    function importButton(mode, label, symbol) {
      const b = button(label, symbol, async () => {
        if (importBusy) return; importBusy = true;
        $('#session-catalog').querySelectorAll('.import-source').forEach(button => { button.disabled = true; });
        const status = $('.catalog-import-status'); if (status) status.textContent = '正在本机索引，可关闭此窗口继续使用…';
        try {
          const result = await rpc('import_chat_records', { mode });
          if (result.cancelled) return;
          if (result.success === false) throw new Error(result.message || '导入未完成。');
          toast(result.message || `已导入 ${result.sessions ?? result.session_count ?? 0} 个会话、${result.messages ?? result.message_count ?? 0} 条消息`);
          await sync(); if ($('#session-catalog').open) await fetchCatalog(true);
        } finally {
          importBusy = false; $('#session-catalog').querySelectorAll('.import-source').forEach(button => { button.disabled = !available(); });
          const status = $('.catalog-import-status'); if (status) status.textContent = '支持 CipherTalk / ChatLab 导出的 JSON、JSONL';
        }
      }, 'small subtle', !available() || importBusy);
      b.classList.add('import-source'); return b;
    }
    async function fetchCatalog(reset) {
      if (catalog.busy) return;
      const serial = ++catalog.serial; catalog.busy = true;
      if (reset) { catalog.items = []; catalog.cursor = null; }
      paintCatalog();
      try {
        const result = await rpc('list_sessions', { query: catalog.query.trim(), source: catalog.filter, cursor: reset ? null : catalog.cursor, limit: 12 });
        if (serial !== catalog.serial) return;
        catalog.available = result.available !== false; catalog.source = result.source || '';
        catalog.detail = result.warning || result.detail || result.message || ''; catalog.total = result.total ?? null;
        const seen = new Set(catalog.items.map(i => i.id));
        for (const item of result.items || []) if (item.id && !seen.has(item.id)) { catalog.items.push(item); seen.add(item.id); }
        for (const update of Array.isArray(getState().catalog?.preview_updates) ? getState().catalog.preview_updates : []) {
          const item = catalog.items.find(item => item.id === update.id && item.preview_status === 'pending');
          if (item) Object.assign(item, update);
        }
        catalog.cursor = result.next_cursor ?? null; catalog.more = Boolean(result.has_more && catalog.cursor !== null); catalog.error = '';
      } catch (err) {
        if (serial !== catalog.serial) return;
        catalog.error = errorText(err); catalog.available = false;
        if (reset) catalog.items = getState().sessions.filter(s => !catalog.query || displayName(s).includes(catalog.query));
      } finally { if (serial === catalog.serial) { catalog.busy = false; paintCatalog(); } }
    }
    function paintCatalog() {
      const dialog = $('#session-catalog'); if (!dialog.open) return;
      const list = $('.catalog-list', dialog), oldTop = list.scrollTop;
      list.replaceChildren();
      const source = $('.catalog-source', dialog); source.replaceChildren();
      if (catalog.error) source.append(notice(catalog.error, 'error'));
      else if (!catalog.available && !catalog.busy) source.append(notice(catalog.detail || '当前没有已连接的会话目录。选择本机微信数据库来源后，此列表只显示数据适配器已读取到的会话；也可连接 WeFlow 或导入记录。', 'info'));
      else { if (catalog.detail && !catalog.busy) source.append(notice(catalog.detail, 'info')); source.append(append(el('div', 'catalog-meta'), el('span', '', catalog.source === 'demo' ? '合成演示会话' : catalog.source === 'wechat_db' ? 'CipherTalk 本机微信数据库会话' : catalog.source === 'weflow' ? 'WeFlow 会话目录' : '当前可用会话'), el('span', '', catalog.total === null ? `已加载 ${catalog.items.length} 个` : `${catalog.total} 个会话`))); }
      if (catalog.busy && !catalog.items.length) list.append(append(el('div', 'catalog-loading'), el('div', 'skeleton wide'), el('div', 'skeleton medium'), el('div', 'skeleton wide')));
      for (const item of catalog.items) {
        if (catalog.multi && catalog.mode !== 'agent' && !item.auto_reply_eligible) continue;
        const group = Boolean(item.type === 'group' || item.is_group || String(item.id).endsWith('@chatroom'));
        const chosen = catalog.selected.has(String(item.id));
        const row = el('button', `catalog-row${chosen || item.id === getState().current_session?.id ? ' selected' : ''}${item.preview_status === 'pending' ? ' preview-pending' : ''}${catalog.multi ? ' catalog-multi-row' : ''}`); row.type = 'button'; row.dataset.sessionId = String(item.id); if (catalog.multi) row.setAttribute('aria-pressed', String(chosen));
        const name = displayName(item);
        const sourceHint = item.source === 'wechat_db' ? 'CipherTalk 数据库会话' : item.source === 'weflow' ? 'WeFlow 会话' : item.source === 'ocr' ? '微信可见窗口会话' : '本地会话';
        append(row, append(el('span', `catalog-avatar${group ? ' group-avatar' : ''}`), group ? icon('people', 19) : el('span', '', name.slice(0, 1))), append(el('span', 'catalog-copy'), append(el('span', 'catalog-title'), el('span', '', name), group ? tag('群聊') : null), el('span', 'catalog-preview', catalog.multi ? (catalog.mode === 'agent' ? `${catalogSourceName(item.source)} · ${group ? '群聊' : '私聊'} · 仅作分析建议` : `${sourceHint} · 允许回复范围；发送通道另行核验`) : previewText(item))), append(el('span', 'catalog-trailing'), el('time', '', formatTime(item.updated || item.timestamp)), catalog.multi ? el('span', `catalog-choice${chosen ? ' chosen' : ''}`, chosen ? '已选择' : '选择') : item.id === getState().current_session?.id ? icon('check', 14) : icon('arrow', 14)));
        row.addEventListener('click', async () => {
          row.disabled = true; row.classList.add('selecting');
          try { if (catalog.multi) { if (catalog.selected.has(String(item.id))) catalog.selected.delete(String(item.id)); else { if (catalog.mode === 'agent' && catalog.selected.size >= 3) { toast('Agent 一次最多分析 3 段对话。'); row.disabled = false; row.classList.remove('selecting'); return; } if (catalog.mode !== 'agent' && (item.source === 'ocr' || [...catalog.selected.values()].some(entry => entry.source === 'ocr'))) catalog.selected.clear(); catalog.selected.set(String(item.id), { session_id: String(item.id), title: name, is_group: group, type: group ? 'group' : 'private', source: item.source }); } paintCatalog(); } else { if (catalog.onSelect) await catalog.onSelect(item); else { await rpc('select_session', { session_id: item.id }); await sync(); go('workspace'); } dialog.close(); } }
          catch (err) { toast(errorText(err), 'error'); row.disabled = false; row.classList.remove('selecting'); }
        }); list.append(row);
      }
      if (!list.querySelector('.catalog-row') && !catalog.busy) list.append(empty(catalog.multi && catalog.mode === 'agent' ? '没有找到可分析的对话' : catalog.multi ? '没有已核验的实时会话' : catalog.query ? '没有找到匹配的会话' : '还没有可用的会话', catalog.multi && catalog.mode === 'agent' ? '检查数据源筛选，或手动补充、导入一段对话后再分析。' : catalog.multi ? '导入记录和未核验的会话不会出现在自动回复名单里。' : catalog.query ? '尝试换一个名字，或手动导入对话。' : '打开微信并读取一次，或在设置中连接会话数据源。', 'chat'));
      list.scrollTop = oldTop;
      const footer = $('.catalog-pagination', dialog); footer.replaceChildren();
      if (catalog.more) footer.append(button(catalog.busy ? '正在加载…' : '加载更多会话', 'chevron', () => fetchCatalog(false), 'small', catalog.busy));
      else if (catalog.items.length && !catalog.busy) footer.append(el('span', 'subtle-caption', catalog.available ? '已显示当前目录中的全部匹配会话' : '以上为当前已读取的会话'));
    }

    function renderAgent() {
      const root = el('section', 'page agent-page'), selected = agent.selected;
      root.append(heading('知弦 Agent', '一次整理最多三段对话，给出下一步建议和对应依据。', '对话分诊 · 只分析，不发送'));
      const intro = el('section', 'panel agent-mission');
      intro.append(append(el('div', 'agent-orbit'), el('span', '', '弦')), append(el('div', 'agent-mission-copy'), el('span', 'eyebrow', '本轮任务'), el('h2', '', '先看清语境，再决定下一步。'), el('p', '', 'Agent 会逐段检查所选对话，整理建议动作、理由与消息依据。任何回复都只作为候选文本供你自行复制和确认。')));
      intro.append(button(selected.length ? `管理选择（${selected.length}/3）` : '选择对话', 'people', () => openCatalog(async items => { agent.selected = items; agent.result = null; agent.error = ''; render(); }, { multi: true, mode: 'agent', selected }), 'subtle'));
      root.append(intro);
      const selection = el('section', 'panel agent-selection');
      selection.append(append(el('div', 'agent-section-heading'), el('h2', '', '本轮对话'), el('span', 'agent-count', `${selected.length} / 3`)));
      if (!selected.length) selection.append(el('p', 'field-hint', '选择任何可用来源中的对话：微信数据库、WeFlow、导入记录、手动对话或已读取会话。'));
      for (const item of selected) selection.append(append(el('div', 'agent-selected-row'), el('span', 'agent-selected-mark', item.is_group ? '群' : '弦'), append(el('span', 'agent-selected-copy'), el('strong', '', item.title || item.session_id), el('span', '', `${catalogSourceName(item.source)} · ${item.is_group ? '群聊' : '私聊'}`)), button('移除', 'close', () => { agent.selected = agent.selected.filter(x => x.session_id !== item.session_id); agent.result = null; render(); }, 'small ghost')));
      root.append(selection);
      const configured = Boolean(getState().config?.has_api_key && getState().config?.base_url && getState().config?.model_name);
      if (agent.error) root.append(notice(agent.error, 'error'));
      if (!configured && !demo) root.append(notice('尚未配置模型 API Key 和模型信息。请先到「设置」完成配置，再运行 Agent。', 'info'));
      const canRun = selected.length > 0 && !agent.busy && (configured || demo) && available();
      const runRow = append(el('div', 'agent-run-row'), button(agent.busy ? '正在分析…' : agent.result ? '重新分析' : '开始分诊', agent.busy ? null : 'spark', async () => {
        if (!agent.selected.length) return;
        agent.busy = true; agent.error = ''; agent.result = null; agent.progress = '正在逐段检查所选对话…'; render();
        try {
          const result = await rpc('run_agent', { session_ids: agent.selected.slice(0, 3).map(item => item.session_id) });
          if (!result || result.available === false || result.success === false) throw new Error(result?.message || result?.reason || 'Agent 暂时无法运行。');
          agent.result = result; agent.progress = '';
          if (result.status === 'partial') agent.error = result.message || '部分会话未能完成分析；已展示当前可用结果。';
        } catch (err) { agent.error = errorText(err); }
        finally { agent.busy = false; agent.progress = ''; render(); }
      }, 'primary', !canRun));
      runRow.append(demo ? tag('合成演示 · 不连接微信', 'demo-tag') : el('span', 'agent-safe-note', '不会发送或修改任何消息')); root.append(runRow);
      if (agent.busy) { const loading = el('section', 'panel agent-progress'); loading.setAttribute('role', 'status'); loading.setAttribute('aria-live', 'polite'); loading.append(el('span', 'agent-spinner'), el('div', '', agent.progress || '正在分析…'), el('p', '', `正在处理 ${selected.length} 段对话。此过程只读取内容并生成建议。`)); for (const item of selected) loading.append(el('div', 'agent-progress-item', `检查中 · ${item.title || item.session_id}`)); root.append(loading); }
      if (agent.result) {
        const results = el('section', 'agent-results'), cards = Array.isArray(agent.result.cards) ? agent.result.cards : [];
        results.append(append(el('div', 'agent-results-heading'), append(el('div'), el('span', 'eyebrow', demo ? '合成样例' : '分析结果'), el('h2', '', agent.result.status === 'partial' ? '部分完成' : '建议已整理'), el('p', 'field-hint', '依据来自所选会话快照；建议和候选文本不会触发发送。')), el('span', 'agent-results-count', `${cards.length} 项`)));
        for (const data of cards) results.append(agentCard(data)); root.append(results);
        if (Array.isArray(agent.result.trace) && agent.result.trace.length) { const trace = el('details', 'panel agent-trace'); trace.append(el('summary', '', '处理记录'), el('p', 'field-hint', '本轮工具状态摘要。')); const list = el('ul'); for (const step of agent.result.trace) list.append(el('li', '', `${step.session_id || '会话'} · ${step.tool || '处理'} · ${agentStatusLabel(step.status)}`)); trace.append(list); root.append(trace); }
      }
      return root;
    }
    function agentCard(data) {
      const card = el('article', 'panel agent-card'), selected = agent.selected.find(item => item.session_id === String(data.session_id)) || {};
      const labels = { reply_now: '建议现在回复', follow_up: '稍后跟进', wait: '先观察', no_action: '无需行动', review: '需要你判断' };
      const score = Number.isFinite(data.risk) ? Math.max(0, Math.min(9, Math.round(data.risk))) : null;
      const riskLevel = score === null ? 'unknown' : score <= 2 ? 'low' : score <= 5 ? 'medium' : 'high';
      const riskText = score === null ? '风险待评估' : `风险 ${score}/9 · ${riskLevel === 'low' ? '较低' : riskLevel === 'medium' ? '中等' : '较高'}`;
      card.append(append(el('div', 'agent-card-top'), append(el('div'), el('span', 'eyebrow', selected.title || data.title || '对话'), el('h3', '', labels[data.action] || data.action || '建议待确认')), tag(riskText, `risk-${riskLevel}`)));
      if (data.confidence !== undefined && data.confidence !== null) card.append(el('span', 'agent-confidence', `判断把握 ${typeof data.confidence === 'number' ? Math.round(data.confidence <= 1 ? data.confidence * 100 : data.confidence) : data.confidence}%`));
      if (data.reason) card.append(el('p', 'agent-reason', data.reason));
      if (Array.isArray(data.evidence) && data.evidence.length) { const block = el('section', 'agent-evidence'); block.append(el('h4', '', '消息依据 · 所选会话快照')); for (const item of data.evidence) block.append(append(el('blockquote', 'agent-quote'), el('p', '', item.text || '（无文本内容）'), append(el('footer'), el('span', '', item.side === 'me' ? '我' : selected.title || '对方'), selected.source ? el('span', '', `来源 · ${selected.source}`) : null, item.message_id ? el('span', 'agent-citation', `消息 ${item.message_id}`) : null))); card.append(block); }
      if (Array.isArray(data.candidates) && data.candidates.length) { const candidates = el('section', 'agent-candidates'); candidates.append(el('h4', '', '回复候选（复制后由你自行决定是否发送）')); for (const text of data.candidates) candidates.append(append(el('div', 'agent-candidate'), el('p', '', text), button('复制候选', 'copy', async () => { const result = await rpc('copy_reply', { text }); if (result?.success === false || result?.available === false) throw new Error(result.message || '复制失败。'); toast('候选文本已复制；请自行确认是否发送。'); }, 'small ghost'))); card.append(candidates); }
      if (data.status) card.append(el('span', 'agent-card-status', `状态 · ${agentStatusLabel(data.status)}`)); return card;
    }
    function agentStatusLabel(status) { return ({ done: '已完成', partial: '部分完成', success: '成功', completed: '已完成', sample: '合成样例', demo_only: '演示状态', running: '进行中', pending: '等待处理', skipped: '已跳过', failed: '失败', error: '异常', unavailable: '不可用' })[status] || status || '完成'; }
    function autoReplyState() { return getState().auto_reply || {}; }
    function autoReplyStatus(a) {
      if (a.status === 'emergency') return ['已紧急停止', 'error'];
      if (a.status === 'blocked') return [a.waiting_for_target ? '等待允许会话' : '等待微信连接', 'paused'];
      if (a.status === 'error') return ['需要处理', 'error'];
      if (a.enabled && !a.paused) return ['运行中', 'active'];
      if (a.enabled && a.paused) return ['已暂停', 'paused'];
      return ['已关闭', 'off'];
    }
    function autoReplyCount(value, fallback) { const number = Number(value); return Number.isFinite(number) && number > 0 ? number : fallback; }
    function autoReplyReason(item) {
      const reasons = { not_mentioned: '群聊中未明确 @ 我', unverified_session: '会话未通过实时核验', session_not_visible: '微信当前未显示此会话', cooldown: '处于同会话冷却时间', hourly_limit: '达到每小时发送限额', daily_limit: '达到每日发送限额', no_reply_needed: '判断无需回复', duplicate: '重复消息已忽略', unsupported_message: '暂不支持此类消息', send_failed: '微信未确认发送成功', signature_appended: '末尾附固定署名“（以上内容为知弦生成）”', demo_only: '合成演示未向微信发送消息' };
      return reasons[item.reason] || (item.reason ? String(item.reason).slice(0, 160) : item.status === 'failed' ? '发送未完成' : item.status === 'skipped' ? '根据安全规则跳过' : '');
    }
    function catchupStatusText(catchup) {
      const labels = { idle: '等待处理', judging: '正在判断是否值得回复', sending: '正在发送', sent: '已发送一条回复', skipped: '判断后未发送', failed: '处理失败' };
      if (!catchup) return '尚未运行历史补回。';
      const reason = autoReplyReason({ reason: catchup.reason, status: catchup.status });
      return `${labels[catchup.status] || '状态未知'}${reason ? ` · ${reason}` : ''}`;
    }
    function confirmCatchup(session) {
      const dialog = $('#editor'); dialog.replaceChildren(); dialog.className = 'dialog auto-reply-confirm';
      const group = Boolean(session.is_group || session.type === 'group');
      const groupRule = group ? (autoReplyState().group_mode === 'all' ? ' 群聊未明确 @ 我时，Jev 需要更高的判断置信度。' : ' 群聊仅在本轮有明确 @ 我证据时才会发送。') : '';
      const copy = append(el('div', 'auto-reply-confirm-copy'), el('span', 'auto-reply-confirm-symbol', '↗'), el('div', 'eyebrow', demo ? '合成演示' : '单次发送确认'), el('h2', '', '运行一次历史补回？'), el('p', '', demo ? '演示模式只会更新合成状态，不会连接微信或发送消息。' : `只检查当前微信可见会话“${session.title || '已选会话'}”中最新一轮尚未解决的历史入站消息。知弦会先判断现在是否值得回复；最多发送一条，并遵守会话核验、限额和其他发送规则。发送时会附固定署名“（以上内容为知弦生成）”。${groupRule}`));
      const checks = el('div', 'auto-reply-confirm-checks');
      checks.append(el('div', 'auto-reply-confirm-check', '点击下方确认即授权这一次判断与至多一条发送。'));
      let ownDisplayName;
      if (group) {
        const nameField = el('label', 'field auto-reply-catchup-select'); nameField.append(el('span', 'field-label', '我在该群的显示名'));
        ownDisplayName = el('input', 'input'); ownDisplayName.type = 'text'; ownDisplayName.maxLength = 80; ownDisplayName.autocomplete = 'off'; ownDisplayName.placeholder = autoReplyState().group_mode === 'all' ? '可留空；未 @ 我时要求更高置信度' : '留空时群聊会安全跳过';
        nameField.append(ownDisplayName, el('span', 'field-hint', '仅用于本次确认群聊中的 @ 我证据，不会保存。')); checks.append(nameField);
      }
      const run = button(demo ? '模拟运行一次' : '确认并运行一次', 'play', async () => {
        if (catchupBusy) return;
        catchupBusy = true; run.disabled = true;
        try {
          const params = { session_id: session.session_id, acknowledge_send: true };
          if (group) params.own_display_name = ownDisplayName.value.trim();
          const result = await rpc('catch_up_auto_reply', params);
          if (result?.started !== true) throw new Error(result?.message || '历史补回没有启动。');
          dialog.close(); await sync(); render(); toast(demo ? '演示模式：仅更新合成状态，没有发送消息' : '已开始历史补回；结果会显示在本页');
        } catch (err) { toast(errorText(err), 'error'); }
        finally { catchupBusy = false; run.disabled = false; }
      }, 'primary');
      dialog.append(copy, checks, append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), run)); dialog.showModal();
    }
    function renderAutoReply() {
      const a = autoReplyState(), [statusText, statusClass] = autoReplyStatus(a), root = el('section', 'page auto-reply-page');
      root.append(heading('自动回复', '仅对明确加入名单的实时微信会话，代你回复新收到的消息。', '知弦 · 主动协助'));
      const liveCard = el('section', `panel auto-reply-status ${statusClass}`);
      const statusIcon = append(el('span', 'auto-reply-status-icon'), icon(a.enabled && !a.paused ? 'pulse' : a.paused ? 'pause' : 'lock', 19));
      const statusCopy = append(el('div', 'auto-reply-status-copy'), el('span', 'auto-reply-kicker', '发送权限'), el('h2', '', statusText), el('p', '', a.detail || (a.enabled && !a.paused ? '知弦正在处理名单中的新消息。' : a.paused ? '自动回复已暂停，不会发送新回复。' : '自动回复默认关闭，每次启动都需要你明确确认。')), a.enabled && !a.paused ? el('p', 'auto-reply-live-explainer', '自动处理已开启：收到允许名单中的新消息后会自动判断并按规则发送，无需你逐条点击发送。') : null);
      const statusActions = el('div', 'auto-reply-status-actions');
      if (a.enabled && !a.paused) statusActions.append(button('暂停自动回复', 'pause', async () => { await rpc('pause_auto_reply', {}); await sync(); toast('自动回复已暂停'); }, 'small subtle', !available()), button('紧急停止', 'close', () => emergencyStop(), 'small danger', !available()));
      else if (a.enabled && a.paused) statusActions.append(button('恢复运行', 'play', () => confirmAutoReplyStart(), 'small primary', !available()), button('紧急停止', 'close', () => emergencyStop(), 'small danger', !available()));
      else statusActions.append(button('启动自动回复', 'play', () => confirmAutoReplyStart(), 'small primary', !available() || !a.allowlist?.length));
      liveCard.append(statusIcon, statusCopy, statusActions); root.append(liveCard);
      root.append(notice(`这项功能会通过微信发送消息，每条自动回复末尾都会附“（以上内容为知弦生成）”。数据库模式可在微信收至托盘时读取新消息；若目标会话原本已打开，发送时会短暂恢复窗口核验标题与输入框，再恢复原状态。不会自动切换到其他聊天。群聊默认只处理明确 @ 你的消息；Jev 仍会判断是否值得回复。`, 'warn'));

      const currentSession = getState().current_session, permittedSessions = a.allowlist || [], visibleId = currentSession?.id;
      if (!permittedSessions.some(item => item.session_id === catchupSessionId)) catchupSessionId = permittedSessions.find(item => item.session_id === visibleId)?.session_id || permittedSessions[0]?.session_id || '';
      const selectedCatchup = permittedSessions.find(item => item.session_id === catchupSessionId);
      const catchup = a.catchup;
      const catchupCard = el('section', 'panel auto-reply-catchup');
      const catchupHead = append(el('div', 'auto-reply-section-head'), append(el('div'), el('div', 'eyebrow', '单次操作 · 最多一条'), el('h2', '', '当前会话单次回复')));
      const catchupSelectLabel = el('label', 'field auto-reply-catchup-select'); catchupSelectLabel.append(el('span', 'field-label', '从允许名单选择会话'));
      const catchupSelect = el('select', 'input'); catchupSelect.setAttribute('aria-label', '历史补回会话');
      catchupSelect.append(new Option(permittedSessions.length ? '选择一个会话' : '请先将实时会话加入允许名单', ''));
      for (const item of permittedSessions) catchupSelect.append(new Option(`${item.title || item.session_id}${item.is_group ? ' · 群聊' : ''}`, item.session_id));
      catchupSelect.value = catchupSessionId; catchupSelect.disabled = !permittedSessions.length || catchupBusy;
      catchupSelect.addEventListener('change', () => { catchupSessionId = catchupSelect.value; render(true); });
      const dbSelected = selectedCatchup?.source === 'wechat_db';
      const chatMatches = Boolean(selectedCatchup && selectedCatchup.session_id === visibleId &&
        (dbSelected ? getState().status?.capture === 'live' : getState().current_session?.active));
      const configuredSource = currentSession?.source || getState().status?.source || getState().config?.source;
      const dbModeLabel = getState().status?.capture === 'live' ? 'CipherTalk DB · 实时监测' : 'CipherTalk DB · 历史读取';
      const sourceLabel = ({ ocr: 'OCR · 微信可见窗口', auto: 'OCR · 微信可见窗口', wechat_db: dbModeLabel, weflow: 'WeFlow · 本地接口', demo: '合成演示', manual: '手动上下文', import: '导入记录' })[configuredSource] || '来源未知';
      const currentIsGroup = Boolean(currentSession?.is_group || currentSession?.type === 'group' || (chatMatches && selectedCatchup?.is_group));
      const singleSession = append(el('div', 'auto-reply-current-session'), append(el('span', 'auto-reply-current-session-label'), dbSelected ? '当前选定会话' : '当前微信会话'), append(el('strong', '', currentSession?.title || '当前没有可见会话'), append(el('span', 'auto-reply-session-tag', currentIsGroup ? '群聊' : currentSession ? '单聊' : '—'), el('span', 'auto-reply-session-tag', sourceLabel))), el('span', 'auto-reply-session-risk', dbSelected ? '依据数据库最新来信判断；目标会话须原本已在微信打开。发送时知弦会短暂恢复托盘窗口，只核验标题与输入框，同名或目标不符则拒绝发送。' : currentIsGroup ? (a.group_mode === 'all' ? '群聊规则：评估所有新消息；单次回复若未明确 @ 我，Jev 需要更高置信度。' : currentSession?.source === 'ocr' ? '群聊风险：OCR 无法可靠识别 @ 我，默认规则下本次会安全跳过。' : '群聊规则：仅有明确 @ 我证据时才会回复。') : (chatMatches ? '单次操作只检查并处理这一条会话中的最新未解决消息，最多发送一条。' : '所选允许名单会话与微信当前显示会话不一致；请先切换微信会话。')));
      const catchupButton = button(catchupBusy ? '正在启动…' : '当前会话单次回复', 'spark', () => selectedCatchup && confirmCatchup(selectedCatchup), 'primary auto-reply-single-action', !available() || !selectedCatchup || !chatMatches || catchupBusy || ['judging', 'sending'].includes(catchup?.status));
      catchupCard.append(catchupHead, singleSession, catchupSelectLabel, catchupSelect, el('p', 'field-hint auto-reply-catchup-hint', dbSelected ? '从数据库判断选定会话的最新待回复消息；发送前知弦会恢复窗口核验。判断无需回复或核验失败时不会发送。' : '检查当前可见聊天中最新一轮尚未解决的入站消息；经确认后最多发送一条，判断无需回复或未通过核验时会显示跳过原因。'), catchupButton, el('p', 'auto-reply-catchup-state', catchupStatusText(catchup)));
      if (catchup?.session_id && catchup.session_id !== catchupSessionId) catchupCard.querySelector('.auto-reply-catchup-state').textContent = `${catchupStatusText(catchup)} · ${permittedSessions.find(item => item.session_id === catchup.session_id)?.title || '其他会话'}`;

      const settings = el('section', 'panel auto-reply-settings');
      settings.append(append(el('div', 'auto-reply-section-head'), append(el('div'), el('div', 'eyebrow', '范围与节奏'), el('h2', '', '你决定知弦可以回复谁')),
        button(`管理名单 · ${a.allowlist?.length || 0}`, 'people', () => openCatalog(async selected => { const typed = await confirmAutoReplyTypes(selected); if (!typed) return; await rpc('configure_auto_reply', { allowlist: typed, ...autoReplyPolicy() }); await sync(); toast('自动回复名单已更新'); }, { multi: true, selected: a.allowlist || [] }), 'small subtle', !available())));
      settings.append(el('p', 'field-hint auto-reply-explainer', '会话名单限定知弦可回复的对象。数据库可在微信后台监听；发送仍要求目标会话原本已打开，并会短暂恢复窗口核验标题与输入框。OCR 会话需要保持可见。'));
      const allowlist = el('div', 'auto-reply-allowlist');
      if (a.allowlist?.length) for (const item of a.allowlist) allowlist.append(append(el('div', 'auto-reply-person'), append(el('span', `auto-reply-avatar${item.is_group ? ' group' : ''}`), item.is_group ? icon('people', 15) : el('span', '', (item.title || '弦').slice(0, 1))), append(el('span', 'auto-reply-person-copy'), el('strong', '', item.title || item.session_id), el('span', '', item.is_group ? (a.group_mode === 'all' ? '群聊 · 评估所有新消息，由 Jev 判断是否回复' : '群聊 · 仅回复明确 @ 我') : '联系人 · 回复新消息')), button('移除', 'close', async () => { await rpc('configure_auto_reply', { allowlist: a.allowlist.filter(entry => entry.session_id !== item.session_id), ...autoReplyPolicy() }); await sync(); }, 'small ghost')));
      else allowlist.append(el('div', 'auto-reply-empty', '名单为空。知弦不会向任何会话发送消息。'));
      settings.append(allowlist);

      const form = el('form', 'auto-reply-policy');
      const modeField = el('label', 'field auto-reply-group-mode'); modeField.append(el('span', 'field-label', '群聊回复范围'));
      const select = el('select', 'input'); select.id = 'auto-reply-group-mode'; select.name = 'group_mode';
      select.append(new Option('仅在确认明确 @ 我时评估回复（推荐）', 'mention_only'), new Option('评估群聊所有新消息，由 Jev 判断是否需要回复', 'all')); select.value = a.group_mode === 'all' ? 'all' : 'mention_only';
      const mentionHint = 'OCR 没有可靠的 @ 标记，因此默认模式不会对 OCR 群聊自动发送；如需评估所有新消息，请选择对应模式，由 Jev 判断是否需要回复。';
      const allHint = 'Jev 会评估群聊中的所有新消息，并判断是否需要回复；仍受安全规则、冷却时间和发送限额约束。';
      const modeHint = el('span', 'field-hint', select.value === 'all' ? allHint : mentionHint);
      select.addEventListener('change', () => { modeHint.textContent = select.value === 'all' ? allHint : mentionHint; });
      modeField.append(select, modeHint);
      const timing = el('div', 'form-two auto-reply-timing');
      timing.append(autoReplyNumberField('合并等待（秒）', 'debounce_seconds', a.debounce_seconds, 2, 15, '等待对方连续发完消息。'), autoReplyNumberField('同会话冷却（秒）', 'cooldown_seconds', a.cooldown_seconds, 15, 3600, '避免短时间内重复发送。'));
      const limits = el('div', 'form-two auto-reply-limits');
      limits.append(autoReplyNumberField('每小时最多发送', 'hourly_limit', a.hourly_limit, 1, 30), autoReplyNumberField('每天最多发送', 'daily_limit', a.daily_limit, 1, 100));
      form.append(modeField, timing, limits);
      form.append(append(el('div', 'auto-reply-form-footer'), el('span', 'field-hint', '每次启动后都需要手动开启。限额按本机统计。'), button('保存规则', 'check', async () => { if (!form.reportValidity()) return; await rpc('configure_auto_reply', { allowlist: a.allowlist || [], ...autoReplyPolicy(form) }); await sync(); toast('自动回复规则已保存'); }, 'small')));
      root.append(catchupCard, settings, form);

      const recent = el('section', 'panel auto-reply-recent');
      recent.append(append(el('div', 'auto-reply-section-head'), append(el('div'), el('div', 'eyebrow', '运行记录'), el('h2', '', '最近处理')),
        append(el('div', 'auto-reply-quota'), tag(`${Number(a.sent_hour || 0)} / ${Number(a.hourly_limit || 0)} 本小时`), tag(`${Number(a.sent_day || 0)} / ${Number(a.daily_limit || 0)} 今日`))));
      const list = el('div', 'auto-reply-recent-list');
      const entries = (a.recent || []).slice(-12).reverse();
      if (!entries.length) list.append(el('div', 'auto-reply-empty', '还没有自动回复记录。这里仅显示会话与处理结果，不会展示消息正文。'));
      else for (const item of entries) {
        const outcome = item.status === 'simulated' ? '模拟回复' : item.status === 'sent' ? '已发送' : item.status === 'skipped' ? '已跳过' : item.status === 'failed' ? '发送失败' : '已处理';
        const cls = item.status === 'simulated' ? 'simulated' : item.status === 'sent' ? 'sent' : item.status === 'failed' ? 'failed' : 'skipped';
        list.append(append(el('div', 'auto-reply-event'), append(el('span', `auto-reply-outcome ${cls}`), outcome), append(el('span', 'auto-reply-event-title'), item.is_group ? icon('people', 14) : null, el('span', '', item.title || '未知会话')), el('span', 'auto-reply-event-reason', autoReplyReason(item)), el('time', '', formatTime(item.at))));
      }
      recent.append(list); root.append(recent); return root;
    }
    function autoReplyNumberField(label, name, value, min, max, hint = '') {
      const wrap = el('label', 'field'); wrap.append(el('span', 'field-label', label)); const input = el('input', 'input'); input.type = 'number'; input.id = `auto-reply-${name}`; input.name = name; input.min = String(min); input.max = String(max); input.step = '1'; input.value = String(autoReplyCount(value, min)); wrap.append(input); if (hint) wrap.append(el('span', 'field-hint', hint)); return wrap;
    }
    function autoReplyPolicy(form = $('.auto-reply-policy')) {
      const read = name => Number(form?.elements?.namedItem(name)?.value);
      return { group_mode: form?.elements?.namedItem('group_mode')?.value === 'all' ? 'all' : 'mention_only', debounce_seconds: read('debounce_seconds') || 4, cooldown_seconds: read('cooldown_seconds') ?? 45, hourly_limit: read('hourly_limit') || 8, daily_limit: read('daily_limit') || 40 };
    }
    function confirmAutoReplyStart() {
      const dialog = $('#editor'); dialog.replaceChildren(); dialog.className = 'dialog auto-reply-confirm';
      const copy = append(el('div', 'auto-reply-confirm-copy'), el('span', 'auto-reply-confirm-symbol', '↗'), el('div', 'eyebrow', demo ? '合成演示' : '发送前确认'), el('h2', '', demo ? '模拟启动自动回复' : '知弦将代表你发送微信消息'), el('p', '', demo ? '演示模式只会更新合成状态，不会连接微信或发送消息。自动发送回复将使用固定后缀“（以上内容为知弦生成）”。' : `当前名单包含 ${autoReplyState().allowlist?.length || 0} 个会话。收到新消息后，知弦可能会自动生成并发送回复。每条自动发送回复末尾都会附上固定署名“（以上内容为知弦生成）”。`));
      const checks = el('div', 'auto-reply-confirm-checks');
      const check = el('input'); check.type = 'checkbox'; check.id = 'auto-reply-confirm-check';
      checks.append(append(el('label', 'auto-reply-confirm-check'), check, el('span', '', demo ? '我已了解这是合成演示，不会向微信发送消息。' : '我已检查允许回复的会话、群聊规则、发送限额和固定署名；我确认知弦可以通过微信发送消息。')));
      const actions = append(el('div', 'dialog-footer'), button('返回检查', null, () => dialog.close()), button(demo ? '模拟启动' : '确认并启动', 'play', async () => { if (!check.checked) return; const form = $('.auto-reply-policy'); if (form && !form.reportValidity()) return; await rpc('configure_auto_reply', { allowlist: autoReplyState().allowlist || [], ...autoReplyPolicy(form) }); await rpc('start_auto_reply', { acknowledge_send: true }); dialog.close(); await sync(); toast(demo ? '演示模式：仅更新合成状态，没有发送消息' : '自动回复已启动'); }, 'primary', true));
      check.addEventListener('change', () => { const start = actions.querySelector('.btn.primary'); if (start) start.disabled = !check.checked; });
      dialog.append(copy, checks, actions); dialog.showModal();
    }
    function confirmAutoReplyTypes(selected) {
      const entries = selected.map(item => ({ ...item })), unknown = entries.filter(item => !item.auto_reply_type_known && !item.type);
      if (!unknown.length) return Promise.resolve(entries.map(item => ({ session_id: item.session_id, title: item.title, is_group: item.type === 'group', type: item.type || (item.is_group ? 'group' : 'private') })));
      return new Promise(resolve => {
        const dialog = $('#editor'); dialog.replaceChildren(); dialog.className = 'dialog auto-reply-confirm';
        const copy = append(el('div', 'auto-reply-confirm-copy'), el('div', 'eyebrow', '会话类型确认'), el('h2', '', '确认这些会话是单聊还是群聊'), el('p', '', 'OCR 目前无法可靠判断会话类型。请逐一选择；知弦不会把未知类型默认当成单聊。'));
        const form = el('div', 'auto-reply-type-list');
        for (const item of unknown) {
          const row = el('label', 'field'); row.append(el('span', 'field-label', item.title));
          const select = el('select', 'input'); select.dataset.sessionId = item.session_id; select.append(new Option('请选择会话类型', ''), new Option('单聊联系人', 'private'), new Option('群聊', 'group')); row.append(select); form.append(row);
        }
        const cancel = button('返回名单', null, () => { dialog.close(); resolve(null); });
        const save = button('确认会话类型', 'check', () => {
          for (const select of form.querySelectorAll('select')) { if (!select.value) return; const item = entries.find(entry => entry.session_id === select.dataset.sessionId); item.type = select.value; item.is_group = select.value === 'group'; }
          dialog.close(); resolve(entries.map(item => ({ session_id: item.session_id, title: item.title, is_group: item.type === 'group', type: item.type || (item.is_group ? 'group' : 'private') })));
        }, 'primary');
        form.addEventListener('change', () => { save.disabled = [...form.querySelectorAll('select')].some(select => !select.value); }); save.disabled = true;
        dialog.append(copy, form, append(el('div', 'dialog-footer'), cancel, save)); dialog.addEventListener('close', () => resolve(null), { once: true }); dialog.showModal();
      });
    }
    function emergencyStop() {
      const dialog = $('#editor'); dialog.replaceChildren(); dialog.className = 'dialog auto-reply-confirm';
      dialog.append(append(el('div', 'auto-reply-confirm-copy'), el('div', 'eyebrow', '紧急控制'), el('h2', '', '立即停止自动回复？'), el('p', '', '知弦会停止后续发送，并清除尚未处理的自动回复任务。')),
        append(el('div', 'dialog-footer'), button('继续运行', null, () => dialog.close()), button('紧急停止', 'close', async () => { await rpc('stop_auto_reply', { emergency: true }); dialog.close(); await sync(); toast('自动回复已紧急停止'); }, 'danger'))); dialog.showModal();
    }

    function historyControl(session) {
      if (['ocr', 'manual'].includes(session.source)) return el('div', 'history-caption', session.source === 'manual' ? '手动提供的对话上下文' : '屏幕采集仅覆盖可见上下文');
      const data = histories.get(session.id) || { cursor: session.next_cursor ?? null, more: session.has_more !== false, busy: false };
      if (!data.more) return el('div', 'history-caption', '已加载可用的历史消息');
      return append(el('div', 'history-control'), button(data.busy ? '正在加载历史…' : '加载更早消息', 'clock', async () => {
        data.busy = true; histories.set(session.id, data);
        try {
          const result = await rpc('load_session_messages', { session_id: session.id, cursor: data.cursor, limit: 30 });
          data.cursor = result.next_cursor ?? null; data.more = Boolean(result.has_more && data.cursor !== null);
          if (result.available === false) toast(result.message || '当前来源没有提供历史消息。');
          await sync();
          const current = getState().current_session, older = result.messages || result.items || [];
          if (older.length) {
            const unique = new Map(); [...older, ...(data.messages || []), ...(session.messages || []), ...(current?.id === session.id ? current.messages || [] : [])].forEach(m => { if (m.id !== undefined && m.id !== null) unique.set(m.id, m); });
            data.messages = [...unique.values()];
            if (current?.id === session.id) setState({ current_session: { ...current, messages: data.messages, next_cursor: data.cursor, has_more: data.more } });
          }
        } finally { data.busy = false; render(); }
      }, 'small ghost', data.busy));
    }
    function reconcileSnapshot(snapshot) {
      if (snapshot.current_session === null && Array.isArray(snapshot.sessions) && snapshot.sessions.length === 0) histories.clear();
      const current = snapshot.current_session, loaded = current ? histories.get(current.id) : null;
      if (!current || !loaded?.messages?.length || ['ocr', 'manual'].includes(current.source)) return snapshot;
      const merged = new Map();
      [...loaded.messages, ...(current.messages || [])].forEach(message => { if (message.id !== undefined && message.id !== null) merged.set(message.id, message); });
      loaded.messages = [...merged.values()];
      return { ...snapshot, current_session: { ...current, messages: loaded.messages, next_cursor: loaded.cursor, has_more: loaded.more } };
    }
    function messageContent(message, session) {
      const kind = messageKind(message);
      if (kind === 'text') return el('div', 'message-bubble', message.text || '');
      const root = el('div', `message-bubble media-message ${kind}-message`);
      root.append(append(el('div', 'media-label'), icon(kind, 15), el('span', '', kind === 'voice' ? '语音消息' : '图片消息'), kind === 'voice' && message.duration ? el('span', 'voice-duration', `${message.duration}″`) : null));
      if (kind === 'image') {
        const source = safeMedia(message.media_url);
        if (source) { const image = el('img', 'message-image'); image.src = source; image.alt = '会话中的图片'; image.loading = 'lazy'; image.addEventListener('error', () => { image.replaceWith(el('div', 'media-unavailable', '图片暂时无法显示')); }); root.append(image); }
        else root.append(append(el('div', 'media-unavailable'), icon('image', 27), el('span', '', '当前来源仅识别到图片标记')));
      } else { const wave = el('div', 'voice-wave'); wave.setAttribute('aria-hidden', 'true'); for (let i = 0; i < 24; i++) wave.append(el('i', `wave-${i % 5}`)); root.append(wave); }
      const existing = kind === 'voice' ? message.transcript : message.image_description;
      if (existing) root.append(el('p', 'media-description', existing));
      else root.append(el('p', 'media-note', kind === 'voice' ? '转写后可作为分析上下文。' : '识别图片内容后再补充对话分析。'));
      root.append(button(kind === 'voice' ? '加载语音播放器' : '加载图片预览', kind === 'voice' ? 'play' : 'image', async () => {
        const result = await rpc('load_media', { session_id: session.id, message_id: message.id, asset_id: message.asset_id || null });
        if (result.available === false || result.success === false) { toast(result.message || result.reason || '当前数据源未提供媒体文件。', 'error'); return; }
        const url = result.media_url || result.data_url || result.url || result.asset?.data_url || result.asset?.media_url || '';
        if (kind === 'image') {
          const source = safeMedia(url); if (!source) throw new Error('图片未提供可在本地展示的地址。');
          const image = el('img', 'message-image'); image.src = source; image.alt = '会话中的图片'; image.addEventListener('error', () => image.replaceWith(el('div', 'media-unavailable', '图片格式无法预览，可尝试模型识图。')));
          const previous = $('.message-image, .media-unavailable', root); if (previous) previous.replaceWith(image); else root.append(image);
        } else {
          const safe = url.length <= 20 * 1024 * 1024 && (/^data:audio\/(mpeg|mp3|wav|x-wav|ogg|webm|mp4|x-m4a|flac|x-flac|opus|aac);base64,[a-z0-9+/=\s]+$/i.test(url) || (location.protocol === 'file:' && /^file:\/\//i.test(url)));
          if (!safe) throw new Error('音频未提供可在本地播放的地址。');
          let audio = $('audio', root); if (!audio) { audio = el('audio', 'message-audio'); audio.controls = true; audio.preload = 'metadata'; audio.setAttribute('aria-label', '语音消息播放器'); audio.addEventListener('error', () => { if (!$('.audio-format-note', root)) root.append(el('p', 'media-note audio-format-note', '此音频格式暂时无法播放，仍可尝试转写。')); }); root.append(audio); } audio.src = url;
        }
      }, 'small ghost', !available()));
      root.append(button(kind === 'voice' ? (existing ? '重新转写' : '转成文字') : (existing ? '重新识图' : '分析图片'), kind === 'voice' ? 'voice' : 'spark', async b => {
        b.querySelector('span').textContent = kind === 'voice' ? '正在转写…' : '正在识图…';
        try {
          const result = await rpc(kind === 'voice' ? 'transcribe_voice' : 'analyze_image', { session_id: session.id, message_id: message.id });
          if (result.available === false || result.success === false) { toast(result.message || result.reason || '当前数据源或模型尚未支持此项能力。', 'error'); return; }
          await sync();
          const text = kind === 'voice' ? result.transcript || result.text : result.description || result.image_description || result.text;
          if (text && getState().current_session?.id === session.id) { const current = getState().current_session; setState({ current_session: { ...current, messages: current.messages.map(m => m.id === message.id ? { ...m, [kind === 'voice' ? 'transcript' : 'image_description']: text } : m) } }); }
          if (result.message) toast(result.message);
        } finally { if (b.isConnected) b.querySelector('span').textContent = kind === 'voice' ? '转成文字' : '分析图片'; }
      }, 'small ghost', !available()));
      root.append(button(kind === 'voice' ? '选择音频并转写' : '选择图片并分析', 'folder', () => chooseMedia(session, kind), 'small ghost', !available()));
      return root;
    }
    async function chooseMedia(session, kind) {
      if (!session?.id) throw new Error('请先选择会话，或手动加入一段对话。');
      const result = await rpc('choose_media', { session_id: session.id, kind });
      if (result.cancelled) return;
      if (result.success === false || result.available === false) throw new Error(result.message || '媒体文件没有成功加入。');
      await sync(); if (result.message) toast(result.message);
    }

    async function saveAppearance(changes) {
      const current = getState().appearance || appearance;
      const result = await rpc('set_appearance', { font_scale: current.font_scale || appearance.font_scale, theme: current.theme || appearance.theme, ...changes });
      if (result.appearance) setState({ appearance: result.appearance }); else await sync();
      toast('外观已更新');
    }
    function renderAppearance() {
      const root = el('section', 'page appearance-page');
      root.append(heading('让知弦，成为你的空间', '选择配色、阅读字号和一张属于你的背景。', 'PERSONAL SPACE'));
      const grid = el('div', 'appearance-grid');
      const main = el('div', 'appearance-main');
      const themes = el('section', 'panel appearance-section'); themes.append(el('h2', '', '知弦风格'), el('p', 'section-description', '安静的正文，带一点探索的仪式感。'));
      const choices = el('div', 'theme-choices');
      for (const [theme, label, desc] of [['night', '夜航', '深石墨 · 青碧微光'], ['paper', '纸白', '浅纸色 · 墨绿留白']]) {
        const b = el('button', `theme-choice theme-${theme}${appearance.theme === theme ? ' selected' : ''}`); b.type = 'button'; b.setAttribute('aria-pressed', String(appearance.theme === theme));
        const miniature = el('span', 'theme-miniature'); for (let i = 0; i < 4; i++) miniature.append(el('i'));
        append(b, miniature, append(el('span', 'theme-choice-copy'), el('b', '', label), el('span', '', desc)), appearance.theme === theme ? icon('check', 17) : null);
        b.addEventListener('click', () => saveAppearance({ theme }).catch(err => toast(errorText(err), 'error'))); choices.append(b);
      }
      themes.append(choices); main.append(themes);
      const reading = el('section', 'panel appearance-section'); reading.append(el('h2', '', '阅读字号'), el('p', 'section-description', '调整对话、分析与回复的文字大小，布局随之适配。'));
      const sizes = el('div', 'font-size-choices');
      for (const [scale, label] of [[.9, '紧凑'], [1, '标准'], [1.1, '舒适'], [1.2, '大字'], [1.3, '特大']]) sizes.append(button(`${label} ${Math.round(scale * 100)}%`, null, () => saveAppearance({ font_scale: scale }), `size-option${Math.abs(appearance.font_scale - scale) < .01 ? ' selected' : ''}`));
      reading.append(sizes, append(el('div', 'reading-preview'), el('span', 'reading-preview-label', '阅读预览'), el('p', '', '一句合适的回应，来自对语境的理解。'), el('span', '', '让重要的文字，清楚地留在眼前。'))); main.append(reading);
      const background = el('section', 'panel appearance-section'); background.append(el('h2', '', '自定义背景'), el('p', 'section-description', '选择本地图片。知弦会自动覆盖遮罩，保证消息始终易读。'));
      background.append(append(el('div', 'background-actions'), button('上传背景图', 'image', async () => { const result = await rpc('choose_background'); if (result.appearance) setState({ appearance: result.appearance }); else await sync(); if (demo) toast('演示模式使用内置合成背景。'); }, 'primary', !available()), button('恢复默认', 'refresh', async () => { const result = await rpc('clear_background'); if (result.appearance) setState({ appearance: result.appearance }); else await sync(); }, '', !available() || !appearance.background_url)));
      background.append(el('p', 'field-hint', '背景仅在本机显示，不会作为模型分析内容上传。')); main.append(background);
      const side = el('aside', 'appearance-aside');
      const sigil = el('div', 'appearance-sigil'); for (let i = 0; i < 3; i++) sigil.append(el('i'));
      side.append(sigil, el('div', 'eyebrow', 'ZHIXIAN / YOUR COMPANION'), el('h2', '', '每次进入，都有一点新意。'), el('p', '', '弦线、轨迹与微光只负责引导。对话本身，始终留在视觉中心。'), button('重播开场', 'play', () => startIntro(true), 'small'), el('p', 'field-hint', '开场约 4 秒，可随时跳过。系统开启减少动态效果时自动简化。'));
      append(grid, main, side); root.append(grid); return root;
    }

    async function loadMoments(reset = false) {
      if (moments.busy && !reset) return;
      const serial = ++moments.serial; moments.busy = true; moments.error = '';
      if (reset) { moments.items = []; moments.cursor = null; }
      if (getPage() === 'moments') render();
      try {
        const result = await rpc('list_moments', { cursor: reset ? null : moments.cursor, limit: 10, session_id: moments.friend?.id || null });
        if (serial !== moments.serial) return;
        moments.available = result.available !== false; moments.source = result.source || ''; moments.detail = result.warning || result.message || result.detail || '';
        const ids = new Set(moments.items.map(m => m.id));
        for (const item of result.items || []) if (item.id && !ids.has(item.id)) { moments.items.push(item); ids.add(item.id); }
        moments.cursor = result.next_cursor ?? null; moments.more = Boolean(result.has_more && moments.cursor !== null);
      } catch (err) { if (serial === moments.serial) { moments.error = errorText(err); moments.available = false; } }
      finally { if (serial === moments.serial) { moments.loaded = true; moments.busy = false; if (getPage() === 'moments') render(); } }
    }
    function renderMoments() {
      const root = el('section', 'page moments-page');
      root.append(heading('朋友圈', '在回应近况之前，先斟酌关系与分寸。', 'SOCIAL CONTEXT', [button('手动补充', 'pen', manualMomentDialog, '', !available()), button('刷新动态', 'refresh', () => loadMoments(true), 'subtle', moments.busy || !available())]));
      const grid = el('div', 'moments-layout'), sidebar = el('aside', 'moments-sidebar');
      sidebar.append(append(el('div', 'moment-friend-avatar'), moments.friend ? el('span', '', displayName(moments.friend).slice(0, 1)) : icon('moments', 29)), el('h2', '', moments.friend ? displayName(moments.friend) : '全部动态'), el('p', '', moments.friend ? '仅查看这位好友的可用动态' : '选择一位好友，查看适合怎样互动。'), button('选择好友', 'people', () => openCatalog(async item => { moments.friend = item; moments.loaded = false; await loadMoments(true); }), '', !available()));
      if (moments.friend) sidebar.append(button('显示全部', 'arrow', () => { moments.friend = null; return loadMoments(true); }, 'small ghost'));
      sidebar.append(append(el('div', 'moments-principle'), icon('lock', 14), el('p', '', '这里只提供点赞与评论建议，不会替你点赞或发送评论。')));
      const feed = el('div', 'moments-feed');
      if (moments.error) feed.append(notice(moments.error, 'error'));
      if (moments.loaded && (!moments.available || moments.detail)) feed.append(notice(moments.detail || '当前数据源尚未提供朋友圈。窗口 OCR 只读取可见聊天，无法获取全量好友动态。你可以手动补充一条。', 'info'));
      if (moments.busy && !moments.items.length) feed.append(append(el('div', 'panel moment-loading'), el('div', 'skeleton short'), el('div', 'skeleton wide'), el('div', 'skeleton medium')));
      else if (!moments.items.length) feed.append(append(el('div', 'panel'), empty('等待一条近况', moments.friend ? '这位好友没有可用动态。可以切换好友或手动补充。' : '连接支持朋友圈的数据源，或粘贴一条好友动态，获取互动建议。', 'moments', button('手动补充动态', 'plus', manualMomentDialog, 'small', !available()))));
      for (const item of moments.items) feed.append(momentCard(item));
      if (moments.more) feed.append(button(moments.busy ? '加载中…' : '加载更多动态', 'chevron', () => loadMoments(false), '', moments.busy));
      append(grid, sidebar, feed); root.append(grid);
      if (!moments.loaded && !moments.busy) queueMicrotask(() => loadMoments(true));
      return root;
    }
    function momentCard(item) {
      const author = typeof item.author === 'object' ? item.author.name || item.author.title || '好友' : item.author || item.author_name || item.name || '好友';
      const card = el('article', 'panel moment-card');
      const identity = append(el('div', 'session-identity'), el('div', 'avatar', String(author).slice(0, 1)), append(el('div'), el('h3', '', author), el('span', 'timestamp', formatTime(item.timestamp || item.updated))));
      card.append(append(el('div', 'moment-top'), identity), el('p', 'moment-text', item.text || item.content || '这条动态没有文字。'));
      const media = Array.isArray(item.media) ? item.media : [];
      if (media.length) {
        const gallery = el('div', 'moment-gallery');
        for (const [index, asset] of media.slice(0, 9).entries()) gallery.append(momentMediaTile(item, asset, index));
        card.append(gallery);
        if (media.length > 9) card.append(el('p', 'field-hint', `此动态共有 ${media.length} 个附件，当前展示前 9 个。`));
      } else {
        const images = Array.isArray(item.images) ? item.images : Array.isArray(item.media_urls) ? item.media_urls : [];
        if (images.length) { const gallery = el('div', 'moment-gallery'); for (const image of images.slice(0, 9)) { const source = safeMedia(typeof image === 'string' ? image : image.url || image.media_url); if (source) { const img = el('img'); img.src = source; img.alt = '朋友圈配图'; img.loading = 'lazy'; gallery.append(img); } else gallery.append(append(el('div', 'moment-image-placeholder'), icon('image', 24), el('span', '', '图片地址不可在本地直接预览'))); } card.append(gallery); }
      }
      const running = moments.running.has(item.id), result = moments.results.get(item.id);
      card.append(append(el('div', 'moment-actions'), el('span', 'subtle-caption', item.source === 'manual' ? '手动提供的动态' : moments.source === 'demo' ? '合成演示动态' : '来自可用数据源'), button(running ? '正在斟酌…' : result ? '重新分析' : '分析这条动态', 'spark', async () => {
        moments.running.add(item.id); render();
        try { const value = await rpc('analyze_moment', { moment_id: item.id, session_id: item.session_id || moments.friend?.id || null }); if (value.available === false) throw new Error(value.message || '当前模型暂不支持朋友圈建议。'); moments.results.set(item.id, value); }
        finally { moments.running.delete(item.id); if (getPage() === 'moments') render(); }
      }, running ? '' : 'subtle', running || !available())));
      if (running) card.append(append(el('div', 'moment-advice'), el('div', 'skeleton wide'), el('div', 'skeleton medium')));
      else if (result) {
        const advice = el('section', 'moment-advice');
        advice.append(append(el('div', 'moment-advice-heading'), icon('heart', 16), el('h3', '', typeof result.like === 'boolean' ? (result.like ? '可以点个赞' : '暂时不点赞也合适') : '互动建议')));
        if (result.recommendation) advice.append(el('p', 'moment-recommendation', valueText(result.recommendation, '')));
        if (result.reason) advice.append(el('p', 'moment-reason', result.reason));
        const mediaWarning = result.warning || result.warnings;
        if (mediaWarning) advice.append(notice(Array.isArray(mediaWarning) ? mediaWarning.join('；') : valueText(mediaWarning, ''), 'info'));
        for (const comment of Array.isArray(result.comments) ? result.comments : []) { const text = typeof comment === 'string' ? comment : comment.text || comment.comment || ''; if (text) advice.append(append(el('div', 'comment-suggestion'), el('p', '', text), button('复制', 'copy', () => api.action('copy_reply', { text }, '已复制评论建议，请自行确认发送'), 'small ghost'))); }
        card.append(advice);
      }
      return card;
    }
    function momentMediaTile(post, asset, index) {
      const key = `${post.id}:${asset.id || index}`;
      const state = momentMedia.get(key) || { source: '', busy: false, error: '' };
      momentMedia.set(key, state);
      const tile = el('div', 'moment-media-tile'); tile.dataset.assetId = String(asset.id || '');
      const kind = String(asset.kind || 'image');
      const label = kind === 'image' ? `配图 ${index + 1}` : kind === 'voice' || kind === 'audio' ? '音频附件' : kind === 'video' ? '视频附件' : '媒体附件';
      const paint = () => {
        tile.replaceChildren();
        if (state.source) {
          const image = el('img', 'moment-loaded-image'); image.src = state.source; image.alt = `朋友圈${label}`;
          image.addEventListener('error', () => { state.source = ''; state.error = '图片已获取，但当前格式无法显示。'; paint(); });
          tile.append(image, el('span', 'moment-media-loaded-label', label)); return;
        }
        tile.append(append(el('div', 'moment-media-symbol'), icon(kind === 'voice' || kind === 'audio' ? 'voice' : 'image', 23)), el('span', 'moment-media-label', label));
        const unavailable = ['unavailable', 'missing', 'unsupported', 'remote_only'].includes(String(asset.status || ''));
        const explanation = state.error || asset.reason || (kind === 'image' ? unavailable ? '数据源未提供可读取的本地配图' : '配图尚未加载' : '此附件暂不支持图片预览');
        tile.append(el('p', `moment-media-reason${state.error ? ' media-load-error' : ''}`, explanation));
        if (kind === 'image' && asset.id) tile.append(button(state.busy ? '正在加载…' : state.error ? '重试加载配图' : '加载配图', 'image', async () => {
          state.busy = true; state.error = ''; paint();
          try {
            const result = await rpc('load_media', { asset_id: asset.id });
            if (result.available === false || result.success === false) { state.error = result.message || result.reason || '上游没有提供可读取的配图。'; return; }
            const source = safeMedia(result.data_url || result.media_url || result.url || '');
            if (!source) { state.error = result.message || '没有返回可在本地展示的图片，不会自动抓取外链。'; return; }
            state.source = source;
          } catch (err) { state.error = errorText(err); }
          finally { state.busy = false; if (tile.isConnected) paint(); else if (getPage() === 'moments') render(); }
        }, 'small ghost', state.busy || !available()));
        else if (!asset.id && kind === 'image') tile.append(el('span', 'field-hint', '此配图尚未登记可加载的媒体资产。'));
      };
      paint(); return tile;
    }
    function manualMomentDialog() {
      const { dialog, body } = showDialog('手动补充朋友圈动态');
      const form = el('form');
      append(form, el('p', 'dialog-copy', '只分析你主动提供的内容。无需访问微信朋友圈，也不会代你进行互动。'), field('好友名称', 'moment-author', moments.friend ? displayName(moments.friend) : '', { placeholder: '这条动态来自谁' }), field('动态内容', 'moment-text', '', { rows: 7, placeholder: '粘贴好友的动态文字，也可以补充图片中可见的内容。' }));
      const feedback = el('div', 'moment-form-feedback');
      const save = button('加入动态列表', 'plus', async () => {
        const author = $('#moment-author').value.trim(), text = $('#moment-text').value.trim();
        if (!author || !text) { feedback.replaceChildren(notice('请填写好友名称和动态内容。', 'error')); return; }
        const result = await rpc('manual_moment', { author, text, session_id: moments.friend?.id || null });
        if (result.available === false) throw new Error(result.message || '暂时无法导入动态。');
        dialog.close(); moments.loaded = false; await loadMoments(true); go('moments'); toast('动态已加入，点击分析即可获取建议。');
      }, 'primary');
      append(form, feedback, append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), save)); form.addEventListener('submit', event => { event.preventDefault(); save.click(); }); body.append(form);
    }

    $('#global-sessions').addEventListener('click', () => openCatalog());
    $('#session-catalog').addEventListener('close', () => { catalog.serial++; catalog.busy = false; clearTimeout(searchTimer); });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') dismissIntro();
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); dismissIntro(); openCatalog(); }
    });
    document.addEventListener('pointerdown', event => {
      const target = event.target.closest('button');
      if (target && !target.disabled && !matchMedia('(prefers-reduced-motion: reduce)').matches) { target.classList.remove('interaction-pulse'); void target.offsetWidth; target.classList.add('interaction-pulse'); setTimeout(() => target.classList.remove('interaction-pulse'), 450); }
    });
    updateState(getState()); startIntro();
    return { updateState, reconcileSnapshot, renderAppearance, renderMoments, renderAutoReply, renderAgent, openCatalog, messageContent, historyControl, chooseMedia, startIntro };
  }
});
