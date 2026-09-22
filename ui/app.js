'use strict';

/* All conversation and model output is inserted with textContent. The only
   innerHTML used below is our fixed, local SVG icon dictionary. */
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = String(text); return n; };
  const append = (node, ...children) => { children.flat(Infinity).forEach(c => { if (c !== null && c !== undefined && c !== false) node.append(typeof c === 'string' ? document.createTextNode(c) : c); }); return node; };
  const paths = {
    chat: '<path d="M5 5h14v11H9l-4 4V5Z"/><path d="M9 9h6M9 12h4"/>',
    book: '<path d="M4 4h7a3 3 0 0 1 3 3v13a4 4 0 0 0-4-2H4V4Zm16 0h-3a3 3 0 0 0-3 3"/><path d="M20 4v14h-2M7 8h4M7 11h4"/>',
    people: '<circle cx="9" cy="8" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 14a5 5 0 0 1 3 5"/>',
    settings: '<path d="M4 7h7m5 0h4M4 17h3m5 0h8"/><circle cx="14" cy="7" r="3"/><circle cx="10" cy="17" r="3"/>',
    compact: '<rect x="3" y="4" width="18" height="16" rx="2"/><rect x="12" y="11" width="9" height="9" rx="1.5"/>',
    expand: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5M3 3l6 6m12-6-6 6M3 21l6-6m12 6-6-6"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>', pause: '<path d="M8 5v14M16 5v14"/>',
    pulse: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1"/>',
    copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    fill: '<path d="M13 5H4v15h16v-8M13 11l7-7m-6 0h6v6"/>',
    check: '<path d="m5 12 4 4L19 6"/>', close: '<path d="m6 6 12 12M18 6 6 18"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    edit: '<path d="m15 4 5 5L9 20H4v-5L15 4Z"/><path d="m12 7 5 5"/>',
    trash: '<path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/>',
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.5"/>',
    lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>',
    window: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/>',
    eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    eyeoff: '<path d="m3 3 18 18M10 5c7-1 12 7 12 7a19 19 0 0 1-4 4M6 6a21 21 0 0 0-4 6s4 7 10 7a11 11 0 0 0 5-1M10 10a3 3 0 0 0 4 4"/>',
    upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 14v7h16v-7"/>',
    folder: '<path d="M3 5h6l2 3h10v12H3V5Z"/>',
    pen: '<path d="M4 20h16M5 16l3-1L19 4l-3-3L5 12v4Z"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
    chevron: '<path d="m7 10 5 5 5-5"/>',
    spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z"/>',
    link: '<path d="m9 15 6-6M8 7l2-2a5 5 0 0 1 7 7l-2 2M16 17l-2 2a5 5 0 0 1-7-7l2-2"/>',
  };
  function icon(name, size = 18) {
    const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [k, v] of Object.entries({ width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' })) s.setAttribute(k, v);
    s.innerHTML = paths[name] || paths.info;
    return s;
  }
  const demo = new URLSearchParams(location.search).get('demo') === '1';
  const pending = new Map();
  let bridge = null, counter = 0, page = 'workspace', compact = window.innerWidth <= 640, online = false, search = '', currentState;
  let renderQueued = false;
  const blank = () => ({ config: { base_url: '', model_name: '', has_api_key: false, reply_model: '', reply_base_url: '', has_reply_api_key: false, relationship: '朋友', style: '自然简洁', auto_analyze: true, context_limit: 30, save_history: false, always_on_top: false, source: 'ocr', weflow_url: 'http://127.0.0.1:5031', weflow_has_token: false, debounce_ms: 1800 }, status: { capture: 'idle', analysis: 'idle', detail: '等待开始读取微信', last_error: '', source: 'ocr', connected: false }, sessions: [], current_session: null, analysis: null, notes: [], contacts: [], version: '1.1.0' });
  currentState = blank();
  const configured = () => currentState.config.has_api_key && currentState.config.base_url && currentState.config.model_name;
  const live = () => ['live', 'searching'].includes(currentState.status.capture);
  const available = () => online || demo;
  function setState(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    currentState = { ...currentState, ...snapshot, config: { ...currentState.config, ...(snapshot.config || {}) }, status: { ...currentState.status, ...(snapshot.status || {}) } };
    for (const k of ['sessions', 'notes', 'contacts']) if (!Array.isArray(currentState[k])) currentState[k] = [];
    if (!renderQueued) { renderQueued = true; requestAnimationFrame(() => { renderQueued = false; render(); }); }
  }
  function rpc(method, params = {}) {
    if (demo) return demoRequest(method, params);
    if (!bridge) return Promise.reject(new Error('请通过「启动知弦」桌面入口打开，浏览器预览无法连接微信。'));
    return new Promise((resolve, reject) => {
      const id = String(++counter);
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error('操作等待超时，请检查模型连接或微信窗口后重试。')); }, ['analyze', 'test_connection'].includes(method) ? 180000 : 45000);
      pending.set(id, { resolve, reject, timeout });
      try { bridge.request(JSON.stringify({ id, method, params })); } catch (err) { clearTimeout(timeout); pending.delete(id); reject(err); }
    });
  }
  async function sync() { const snapshot = await rpc('bootstrap'); if (snapshot) setState(snapshot); return snapshot; }
  function errorText(error) { return error && error.message ? error.message : String(error || '操作未完成，请重试。'); }
  function toast(message, level = 'info') {
    const item = append(el('div', `toast${level === 'error' ? ' error' : ''}`), icon(level === 'error' ? 'info' : 'check', 16), el('span', '', message));
    $('#toasts').append(item);
    setTimeout(() => item.remove(), level === 'error' ? 7000 : 4000);
  }
  function button(text, name, handler, cls = '', disabled = false) {
    const b = el('button', `btn ${cls}`.trim()); b.type = 'button'; b.disabled = disabled;
    if (name) b.append(icon(name, cls.includes('small') ? 13 : 15)); b.append(el('span', '', text));
    b.addEventListener('click', async () => {
      if (!handler || b.disabled) return;
      b.disabled = true; b.setAttribute('aria-busy', 'true');
      try { await handler(b); } catch (err) { toast(errorText(err), 'error'); }
      finally { b.disabled = disabled; b.removeAttribute('aria-busy'); }
    }); return b;
  }
  function iconButton(title, name, handler, cls = '') {
    const b = button('', null, handler, cls); b.className = `icon-button ${cls}`; b.replaceChildren(icon(name, 16)); b.title = title; b.setAttribute('aria-label', title); return b;
  }
  const notice = (text, kind = 'info') => append(el('div', `notice notice-${kind}`), icon('info', 15), el('span', '', text));
  const tag = (text, cls = '') => el('span', `tag ${cls}`, text);
  function formatTime(value) {
    if (!value) return '';
    if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(String(value))) return String(value).slice(0, 5);
    const date = typeof value === 'number' ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
    return Number.isNaN(date.getTime()) ? String(value).slice(0, 25) : date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  function valueText(value, fallback = '暂未提供') {
    if (value === null || value === undefined || value === '') return fallback;
    if (typeof value === 'object') return Object.entries(value).map(([k, v]) => `${k}：${valueText(v, '')}`).join('；');
    return String(value);
  }
  function percentage(value) { if (typeof value !== 'number' || !Number.isFinite(value)) return ''; return `${Math.round(value <= 1 ? value * 100 : value)}%`; }
  function go(nextPage) { page = nextPage; search = ''; render(true); $('#main').scrollTop = 0; }
  function heading(title, subtitle, eyebrow, actions = []) {
    return append(el('div', 'page-heading'), append(el('div'), eyebrow ? el('div', 'eyebrow', eyebrow) : null, el('h1', '', title), subtitle ? el('p', 'page-subtitle', subtitle) : null), actions.length ? append(el('div', 'page-heading-actions'), actions) : null);
  }
  function empty(title, desc, name, action) { return append(el('div', 'empty'), append(el('div', 'empty-icon'), icon(name, 24)), el('h3', '', title), el('p', '', desc), action || null); }
  async function action(method, params = {}, success) { const result = await rpc(method, params); if (success) toast(success); await sync(); return result; }
  async function toggleCapture() { await action(live() ? 'pause_capture' : 'start_capture'); }
  async function toggleCompact() { const enabled = !compact; await rpc('set_compact', { enabled }); compact = enabled; $('#app').classList.toggle('compact', compact); render(true); }
  function renderNavigation() {
    const nav = $('#navigation'); nav.replaceChildren();
    for (const [id, label, name] of [['workspace', '工作台', 'chat'], ['knowledge', '知识库', 'book'], ['contacts', '联系人', 'people'], ['settings', '设置', 'settings']]) {
      const b = el('button', `nav-button${page === id ? ' active' : ''}`); b.type = 'button'; b.setAttribute('aria-label', label); b.setAttribute('aria-current', page === id ? 'page' : 'false'); b.title = label;
      append(b, icon(name, 21), el('span', '', label)); b.addEventListener('click', () => go(id)); nav.append(b);
    }
  }
  function renderChrome() {
    const status = currentState.status;
    const chip = $('#global-status');
    const labels = { idle: '尚未开始', searching: '寻找微信窗口', live: '正在观察', paused: '已暂停', error: '读取异常' };
    chip.textContent = status.analysis === 'running' ? 'Jev 正在分析' : (labels[status.capture] || '等待连接');
    chip.className = `status-chip ${status.analysis === 'running' ? 'running' : status.capture === 'live' ? 'live' : status.capture === 'error' ? 'error' : ''}`;
    $('#footer-status').textContent = status.detail || '本地工作台已就绪';
    $('#footer-meta').textContent = currentState.config.model_name ? `${currentState.config.model_name} · ${sourceLabel(currentState.config.source)}` : 'Jev · 语境与判断';
    $('#version').textContent = String(currentState.version || '1.1.0');
    $('#demo-label').hidden = !demo;
    $('#mini-expand').hidden = !compact;
    renderNavigation();
  }
  function render(force = false) {
    renderChrome();
    if (page === 'settings' && !force && $('#config-form')) return;
    if ((page === 'knowledge' || page === 'contacts') && !force && document.activeElement?.id === 'collection-search') return;
    const main = $('#main'); const oldScroll = main.scrollTop;
    const timeline = $('.timeline'); const oldTimeline = timeline ? { top: timeline.scrollTop, end: timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 45, session: timeline.dataset.session } : null;
    const assist = $('.assistant-panel'); const assistScroll = assist?.scrollTop || 0;
    const judgmentOpen = $('.judgment-details')?.open || false;
    if (page === 'workspace') main.replaceChildren(configured() ? workspace() : onboarding());
    else if (page === 'settings') main.replaceChildren(settings());
    else main.replaceChildren(collection(page));
    main.scrollTop = oldScroll;
    const nextTimeline = $('.timeline');
    if (nextTimeline) nextTimeline.scrollTop = !oldTimeline || oldTimeline.end || nextTimeline.dataset.session !== oldTimeline.session ? nextTimeline.scrollHeight : oldTimeline.top;
    const nextJudgment = $('.judgment-details'); if (nextJudgment) nextJudgment.open = judgmentOpen;
    const nextAssist = $('.assistant-panel'); if (nextAssist) nextAssist.scrollTop = assistScroll;
  }
  function sourceLabel(source) { return source === 'weflow' ? 'WeFlow 本地接口' : source === 'manual' ? '手动提供的对话' : '微信窗口 · 本地 OCR'; }
  function workspace() {
    const s = currentState, current = s.current_session, busy = s.status.analysis === 'running';
    const root = el('section', 'page workspace');
    root.append(heading('对话工作台', '当前会话的上下文、判断与回复建议。', null, [
      button('手动补充', 'pen', () => manualDialog(), 'ghost', !available()),
      button('分析当前', 'spark', () => action('analyze', { session_id: current?.id }), '', !available() || !current?.messages?.length || busy),
      button(live() ? '暂停观察' : '开始观察', live() ? 'pause' : 'play', toggleCapture, live() ? 'subtle' : 'primary', !available()),
    ]));
    if (compact) root.append(append(el('div', 'compact-heading'), append(el('div'), el('h2', '', current?.title || '等待一段对话'), el('p', '', live() ? '新消息将触发实时分析' : '当前观察已暂停')), append(el('div', 'compact-controls'), button('分析', 'spark', () => action('analyze', { session_id: current?.id }), 'small', !current?.messages?.length || busy), button(live() ? '暂停' : '开始', live() ? 'pause' : 'play', toggleCapture, 'small subtle'))));
    if (s.status.last_error) { const n = notice(s.status.last_error, 'error'); n.classList.add('inline-error'); root.append(n); }
    const grid = el('div', 'workspace-grid');
    const chat = el('section', 'panel chat-panel'); chat.setAttribute('aria-label', '当前对话');
    const identity = append(el('div', 'session-identity'), el('div', 'avatar', current?.title?.slice(0, 1) || '弦'), append(el('div'), el('div', `session-name${!current ? ' session-empty-title' : ''}`, current?.title || '尚未选择会话'), append(el('div', 'session-caption'), el('span', live() && current ? 'live-label' : '', current ? `${current.messages?.length || 0} 条上下文` : '在微信中打开一段聊天'))));
    const sessionSelect = el('select', 'session-select'); sessionSelect.setAttribute('aria-label', '切换已读取会话');
    if (!s.sessions.length) { sessionSelect.append(new Option('最近会话', '')); sessionSelect.disabled = true; }
    else for (const session of s.sessions) sessionSelect.append(new Option(session.title || '未命名会话', session.id, false, session.id === current?.id));
    sessionSelect.addEventListener('change', () => action('select_session', { session_id: sessionSelect.value }).catch(err => toast(errorText(err), 'error')));
    chat.append(append(el('div', 'panel-top'), identity, append(el('div', 'session-picker'), sessionSelect)));
    chat.append(append(el('div', 'source-strip'), icon('window', 13), el('span', '', sourceLabel(current?.source || s.config.source)), el('span', '', '·'), el('span', '', current?.active ? '当前可见会话' : current ? '历史上下文' : '仅观察当前可见窗口')));
    const timeline = el('div', 'timeline'); timeline.dataset.session = current?.id || ''; timeline.setAttribute('role', 'log'); timeline.setAttribute('aria-label', '聊天记录');
    if (current?.messages?.length) {
      timeline.append(el('div', 'time-divider', '已读取的对话上下文'));
      for (const [idx, message] of current.messages.entries()) {
        const m = el('article', `message ${message.side === 'me' ? 'me' : 'other'}${idx === current.messages.length - 1 ? ' message-latest' : ''}`);
        append(m, append(el('div', 'message-meta'), el('span', 'sender', message.side === 'me' ? '我' : message.sender || current.title), el('time', '', formatTime(message.timestamp))), el('div', 'message-bubble', message.text || ''));
        timeline.append(m);
      }
    } else timeline.append(empty(live() ? '正在寻找你的对话' : '从一段对话开始', live() ? '请保持微信聊天窗口可见。知弦会在这里整理识别到的消息。' : '打开电脑微信并选择会话，然后开始观察。也可以手动补充一段对话。', 'chat', !live() ? button('开始观察微信', 'play', toggleCapture, 'primary small', !available()) : null));
    chat.append(timeline);
    chat.append(append(el('div', 'chat-bottom'), append(el('div', 'capture-hint'), icon('eye', 13), el('span', '', live() ? '持续读取可见消息' : '开始观察后读取可见消息')), button('读取一次', 'refresh', () => action('one_shot'), 'small ghost', !available())));
    const assistant = el('section', 'assistant-panel'); assistant.setAttribute('aria-label', 'Jev 分析与回复');
    const analysis = s.analysis?.session_id === current?.id ? s.analysis : null;
    assistant.append(analysisPanel(analysis, busy));
    if (analysis?.warning) assistant.append(notice(analysis.warning, 'warn'));
    assistant.append(replies(analysis, current, busy));
    append(grid, chat, assistant); root.append(grid); return root;
  }
  function analysisPanel(analysis, busy) {
    const panel = el('section', `panel analysis-panel${!analysis && !busy ? ' empty-analysis' : ''}`);
    const heading = append(el('div', 'section-row'), append(el('h2', 'section-title'), append(el('span', 'analysis-emblem'), icon('pulse', 17)), el('span', '', 'Jev 对话分析')), busy ? append(el('span', 'analysis-loading'), bars(), el('span', '', '分析中')) : el('span', 'timestamp', analysis ? `${formatTime(analysis.updated_at)} 更新` : '等待消息'));
    panel.append(heading);
    if (busy && !analysis) {
      panel.append(append(el('div', 'skeleton-stack'), el('div', 'skeleton wide'), el('div', 'skeleton medium'), el('div', 'skeleton short')));
      panel.append(el('p', 'subtle-caption', '正在结合对话、联系人和知识库理解语境…')); return panel;
    }
    if (!analysis) { panel.append(empty('先倾听，再回应', 'Jev 会结合消息意图、真实需求和沟通风险，为你判断下一步。', 'pulse')); return panel; }
    panel.append(el('p', 'analysis-lead', valueText(analysis.intent, '当前意图尚不明确')));
    const insights = el('div', 'insight-grid');
    for (const [label, key, name] of [['真实需求', 'need', 'chat'], ['建议行动', 'action', 'arrow']]) insights.append(append(el('div', 'insight-item'), append(el('div', 'label'), icon(name, 12), el('span', '', label)), el('div', 'value', valueText(analysis[key]))));
    panel.append(insights);
    const decisions = el('div', 'decision-line');
    if (typeof analysis.should_reply === 'boolean') decisions.append(tag(analysis.should_reply ? '适合实质回应' : '暂不补充实质内容', 'mint'));
    if (analysis.urgency !== null && analysis.urgency !== undefined && analysis.urgency !== '') decisions.append(tag(`紧迫度 · ${valueText(analysis.urgency)}`));
    if (analysis.risk !== null && analysis.risk !== undefined && analysis.risk !== '') decisions.append(tag(`关系风险 · ${typeof analysis.risk === 'number' ? `${analysis.risk} / 9` : valueText(analysis.risk)}`, (typeof analysis.risk === 'number' && analysis.risk >= 6) || /高|high|urgent/i.test(valueText(analysis.risk)) ? 'risk' : 'neutral'));
    if (percentage(analysis.confidence)) decisions.append(tag(`意图置信度 ${percentage(analysis.confidence)}`, 'neutral'));
    panel.append(decisions);
    if (analysis.answers && Object.keys(analysis.answers).length) panel.append(judgmentDetails(analysis));
    return panel;
  }
  function judgmentDetails(analysis) {
    const answers = analysis.answers, details = el('details', 'judgment-details');
    details.append(el('summary', '', '查看 7 项判断'));
    const content = el('div', 'judgment-list');
    const binary = (key, yes, no) => typeof answers[key]?.noul === 'number' ? `${answers[key].noul >= .5 ? yes : no}（${percentage(answers[key].noul)} 肯定概率）` : '未返回';
    const rows = [
      ['字面含义', binary('literal_question', '以字面表达为主', '可能另有含义')],
      ['真实意图', valueText(analysis.intent, '未返回')],
      ['关系风险', typeof analysis.risk === 'number' ? `${analysis.risk} / 9` : valueText(analysis.risk, '未返回')],
      ['实质回应', binary('should_reply_now', '适合补充已知事实或计划', '宜少说或先核实背景')],
      ['建议行动', valueText(analysis.action, '未返回')],
      ['对方需求', valueText(analysis.need, '未返回')],
      ['紧张缓解', binary('tension_resolved', '已缓解或没有紧张', '仍有紧张感')],
    ];
    for (const [label, value] of rows) content.append(append(el('div', 'judgment-row'), el('span', 'judgment-label', label), el('span', 'judgment-value', value)));
    content.append(el('p', 'subtle-caption', '“实质回应”判断是否有足够事实支持下一句话，不代表发送时机。概率来自模型返回，缺失时保留为空。'));
    details.append(content); return details;
  }
  function bars() { return append(el('span', 'activity-bars'), el('i'), el('i'), el('i')); }
  function replies(analysis, session, busy) {
    const section = el('section', 'replies-section');
    append(section, append(el('div', 'replies-heading'), append(el('div'), el('h2', '', '回复建议'), el('p', 'subtle-caption', '选择合适的表达，确认后自行发送。')), analysis && typeof analysis.latency_ms === 'number' ? el('span', 'timestamp', `本次 ${(analysis.latency_ms / 1000).toFixed(1)} s`) : null));
    const candidates = Array.isArray(analysis?.candidates) ? analysis.candidates : [];
    if (!candidates.length) {
      const e = el('div', 'panel');
      e.append(empty(busy ? '正在组织候选回应' : analysis ? '这次没有生成候选' : '合适的话，留待此刻', busy ? '分析完成后，候选回复会出现在这里。' : analysis ? (analysis.warning || 'Jev 已给出判断。你可以在模型设置中配置可生成文本的模型，再次分析。') : '每条候选都会附上选择理由。复制或回填前，你始终保留最终判断。', 'pen', analysis && !busy ? button('查看模型设置', 'settings', () => go('settings'), 'small ghost') : null)); section.append(e); return section;
    }
    const ordered = candidates.map((candidate, idx) => ({ candidate, idx }));
    ordered.sort((a, b) => Number(b.idx === analysis.best_index) - Number(a.idx === analysis.best_index));
    for (const { idx, candidate } of ordered) {
      const best = idx === analysis.best_index, card = el('article', `reply-card${best ? ' best' : ''}`);
      const number = append(el('div', 'reply-number'), el('span', 'reply-index', String(idx + 1).padStart(2, '0')), best ? append(el('b', 'recommendation-label'), icon('check', 12), el('span', '', 'Jev 推荐')) : el('span', '', '备选表达'));
      card.append(append(el('div', 'reply-top'), number, percentage(candidate.score) ? el('span', 'reply-score', `匹配 ${percentage(candidate.score)}`) : null));
      card.append(el('p', 'reply-text', candidate.text || ''));
      if (candidate.reason) card.append(append(el('div', 'reply-reason'), icon('info', 12), el('span', '', candidate.reason)));
      const copy = button('复制', 'copy', () => action('copy_reply', { text: candidate.text }, '已复制回复'), 'small ghost', !candidate.text || !available());
      const fill = button('填入微信', 'fill', () => action('fill_reply', { session_id: session.id, index: idx }, demo ? '演示模式：未向微信写入内容' : '已填入微信，请确认后自行发送'), `small ${best ? 'primary' : ''}`, !session?.active || !candidate.text || !available());
      if (!session?.active) fill.title = '需在微信中打开同名会话，并重新读取后回填';
      card.append(append(el('div', 'reply-bottom'), el('span', 'reply-footer-hint', session?.active ? '仅填入，不自动发送' : '回填前需重新读取当前会话'), append(el('div', 'reply-actions'), copy, fill)));
      section.append(card);
    }
    return section;
  }

  function field(label, name, value, { type = 'text', placeholder = '', hint = '', optional = false, rows, options } = {}) {
    const wrap = el('div', 'field'), lab = el('label', '', label); lab.htmlFor = name;
    if (optional) lab.append(el('span', 'optional', '选填'));
    let input;
    if (options) { input = el('select', 'input'); for (const [v, t] of options) input.append(new Option(t, v, false, String(value) === String(v))); }
    else { input = el(rows ? 'textarea' : 'input', 'input'); if (!rows) input.type = type; else input.rows = rows; input.value = value ?? ''; input.placeholder = placeholder; }
    input.name = name; input.id = name; input.autocomplete = type === 'password' ? 'new-password' : 'off'; input.spellcheck = false;
    if (type === 'number') { input.min = name === 'context_limit' ? '3' : '400'; input.max = name === 'context_limit' ? '200' : '10000'; input.step = name === 'context_limit' ? '1' : '100'; }
    append(wrap, lab);
    if (type === 'password') {
      const toggle = iconButton('显示密钥', 'eye', () => { input.type = input.type === 'password' ? 'text' : 'password'; toggle.replaceChildren(icon(input.type === 'password' ? 'eye' : 'eyeoff', 16)); toggle.title = input.type === 'password' ? '显示密钥' : '隐藏密钥'; toggle.setAttribute('aria-label', toggle.title); });
      wrap.append(append(el('div', 'input-wrap'), input, toggle));
    } else wrap.append(input);
    if (hint) wrap.append(el('p', 'field-hint', hint));
    return wrap;
  }
  function toggle(label, name, checked, desc) {
    const text = append(el('div', 'switch-copy'), el('div', '', label), desc ? el('p', 'field-hint', desc) : null);
    const l = el('label', 'switch'), input = document.createElement('input'); input.type = 'checkbox'; input.name = name; input.id = name; input.checked = Boolean(checked); input.setAttribute('aria-label', label); append(l, input, el('span', 'switch-track'));
    return append(el('div', 'switch-row'), text, l);
  }
  function primaryFields(form) {
    const c = currentState.config;
    form.append(field('API Key', 'api_key', '', { type: 'password', placeholder: c.has_api_key ? '已安全保存，留空保留' : '输入你的 Jev API Key', hint: c.has_api_key ? '密钥不会回传到界面。输入新值即可替换。' : '只保存在这台电脑，由 Windows 加密保护。' }));
    form.append(field('Base URL', 'base_url', c.base_url, { placeholder: 'https://你的模型服务地址', hint: '填写模型提供方给出的 API 地址。' }));
    form.append(field('Model name', 'model_name', c.model_name, { placeholder: '填写你的 Jev 模型名称' }));
  }
  function readConfig(form) {
    const data = new FormData(form), out = {};
    for (const [key, value] of data.entries()) out[key] = String(value).trim();
    form.querySelectorAll('input[type="checkbox"]').forEach(i => { out[i.name] = i.checked; });
    for (const k of ['context_limit', 'debounce_ms']) if (k in out) out[k] = Number(out[k]);
    return out;
  }
  function validateConfig(config) {
    if (!config.api_key && !currentState.config.has_api_key) throw new Error('请填写 API Key。');
    if (!config.base_url) throw new Error('请填写 Base URL。');
    let url; try { url = new URL(config.base_url); } catch { throw new Error('Base URL 格式不正确，请填写完整的 http:// 或 https:// 地址。'); }
    if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Base URL 必须使用 http:// 或 https://。');
    if (!config.model_name) throw new Error('请填写 Model name。');
    if ('context_limit' in config && (!Number.isInteger(config.context_limit) || config.context_limit < 3 || config.context_limit > 200)) throw new Error('上下文条数需要是 3–200 之间的整数。');
    if ('debounce_ms' in config && (!Number.isFinite(config.debounce_ms) || config.debounce_ms < 400 || config.debounce_ms > 10000)) throw new Error('消息合并等待时间需要在 400–10000 毫秒之间。');
  }
  function formFeedback(form, message, error = false) {
    let f = $('.form-feedback', form); if (!f) { f = el('div', 'form-feedback'); f.setAttribute('role', 'status'); form.append(f); }
    f.className = `form-feedback${error ? ' error' : ''}`; f.textContent = message;
  }
  function configActions(form, onboardingMode = false) {
    const test = button('测试连接', 'link', async () => {
      try { const config = readConfig(form); validateConfig(config); formFeedback(form, '正在验证模型连接，请稍候…'); const result = await rpc('test_connection', { config }); formFeedback(form, result.message || (result.success ? '连接成功' : '连接未通过'), result.success === false); }
      catch (err) { formFeedback(form, errorText(err), true); }
    }, '', !available());
    const save = button(onboardingMode ? '进入工作台' : '保存设置', onboardingMode ? 'arrow' : 'check', async () => {
      try { const config = readConfig(form); validateConfig(config); await rpc('save_config', { config }); await sync(); for (const field of form.querySelectorAll('input[type="password"]')) field.value = ''; toast('模型设置已保存'); go(onboardingMode ? 'workspace' : 'settings'); }
      catch (err) { formFeedback(form, errorText(err), true); }
    }, 'primary', !available());
    form.append(append(el('div', 'form-actions'), test, save));
    form.addEventListener('submit', e => { e.preventDefault(); save.click(); });
  }
  function onboarding() {
    const root = el('section', 'onboarding');
    const copy = el('div', 'welcome-copy');
    copy.append(append(el('div', 'welcome-wordmark'), icon('pulse', 21), el('span', '', '知弦 / ZHIXIAN')));
    const title = el('h1'); append(title, '微信里的对话，', document.createElement('br'), el('em', '', '有 Jev 一起斟酌。')); copy.append(title);
    copy.append(el('p', 'welcome-intro', '在电脑端读取对话、理解沟通意图，获得有依据的回复建议。只填三项模型配置，即可开始。'));
    const steps = el('div', 'welcome-steps');
    for (const [n, text] of [['01', '接入你已经拥有的 Jev 模型'], ['02', '打开电脑微信，选择一段对话'], ['03', '开始观察，获取即时分析与回应建议']]) steps.append(append(el('div', 'welcome-step'), el('span', 'step-number', n), el('span', '', text)));
    copy.append(steps);
    const form = el('form', 'welcome-form'); form.id = 'onboarding-form'; form.autocomplete = 'off'; form.append(el('div', 'eyebrow', '第一次见面'), el('h2', '', '三项配置，就此开始'), el('p', 'form-description', '使用你自己的模型服务。保存后，随时可以在设置中调整。')); primaryFields(form); configActions(form, true);
    form.append(append(el('div', 'privacy-caption'), icon('lock', 12), el('span', '', '仅将选中的对话上下文和相关背景发往你配置的 API。本地历史默认关闭，候选回复不会自动发送到微信。')));
    return append(root, copy, form);
  }
  function settings() {
    const root = el('section', 'page settings-page'); root.append(heading('模型与偏好', '管理模型连接、消息来源和本地数据。', null));
    const grid = el('div', 'settings-grid'), form = el('form', 'panel settings-form'); form.id = 'config-form'; form.autocomplete = 'off';
    form.append(el('h2', '', 'Jev 模型连接'), el('p', 'form-description', '日常使用只需配置以下三项。仅选中的上下文与相关背景会发到此 API，本地历史默认关闭。')); primaryFields(form);
    const advanced = el('details', 'advanced'); advanced.append(el('summary', '', '高级选项'));
    const body = el('div', 'advanced-body'), c = currentState.config;
    const generation = el('section', 'advanced-section'); generation.append(el('h3', '', '候选回复生成（可选）'), el('p', 'field-hint', 'OpenRouter 接入可复用同一密钥，默认用 deepseek/deepseek-v4.1-flash 起草回复。TypeSafe 直连或自定义判断服务需另配生成模型，才能提供候选回复。'));
    generation.append(field('生成模型名称', 'reply_model', c.reply_model, { optional: true, placeholder: '留空：OpenRouter 使用默认回复模型' }), field('生成模型 Base URL', 'reply_base_url', c.reply_base_url, { optional: true, placeholder: '留空：按服务自动匹配' }), field('生成模型 API Key', 'reply_api_key', '', { type: 'password', optional: true, placeholder: c.has_reply_api_key ? '已保存，留空保留' : '留空：仅同服务复用主密钥' }));
    const source = el('section', 'advanced-section'); source.append(el('h3', '', '消息读取'));
    source.append(field('读取来源', 'source', c.source === 'auto' ? 'ocr' : c.source, { options: [['ocr', '微信可见窗口（本地 OCR）'], ['weflow', '已有 WeFlow 本地服务']] }), el('p', 'field-hint', '默认读取微信可见窗口，无需数据库密钥。切换会话后会重新建立上下文。'));
    source.append(field('WeFlow 地址', 'weflow_url', c.weflow_url, { optional: true, placeholder: 'http://127.0.0.1:5031' }), field('WeFlow 访问令牌', 'weflow_token', '', { type: 'password', optional: true, placeholder: c.weflow_has_token ? '已保存，留空保留' : '仅当你的服务需要验证时填写' }));
    source.append(button('检查读取来源', 'refresh', async () => { const result = await rpc('refresh_sources'); const sources = Array.isArray(result) ? result : result?.sources; if (sources?.length) toast(sources.map(s => typeof s === 'string' ? s : s.title || s.name || s.source || '已发现窗口').join('；')); else toast(typeof result?.message === 'string' ? result.message : '已刷新来源。请确认微信窗口已打开。'); }, 'small', !available()));
    const behavior = el('section', 'advanced-section'); behavior.append(el('h3', '', '分析与表达'));
    behavior.append(append(el('div', 'form-two'), field('默认关系', 'relationship', c.relationship, { placeholder: '朋友 / 同事 / 客户' }), field('表达风格', 'style', c.style, { placeholder: '自然、简洁、有分寸' })));
    behavior.append(field('群聊回复对象', 'reply_to', c.reply_to || '', { optional: true, placeholder: '例如：项目负责人', hint: '群聊中需要针对特定成员分析时填写，留空使用当前对话语境。' }));
    behavior.append(append(el('div', 'form-two'), field('上下文消息条数', 'context_limit', c.context_limit, { type: 'number' }), field('合并消息等待（毫秒）', 'debounce_ms', c.debounce_ms, { type: 'number', hint: '等对方连续几条消息说完后再分析。' })));
    behavior.append(toggle('新消息到来时自动分析', 'auto_analyze', c.auto_analyze), toggle('窗口保持置顶', 'always_on_top', c.always_on_top), toggle('在本机保留会话历史', 'save_history', c.save_history, '关闭时仍会在当前运行期间保留上下文。'));
    const data = el('section', 'advanced-section'); data.append(el('h3', '', '本地数据'));
    data.append(append(el('div', 'data-actions'), button('打开数据文件夹', 'folder', () => rpc('open_data_folder'), 'small', !available()), button('清除会话历史', 'trash', () => confirmDialog('清除会话历史', '此操作会清除本地保存的聊天上下文和分析记录，知识库、联系人和模型设置仍会保留。', async () => { await action('clear_history', {}, '会话历史已清除'); }), 'small danger', !available())));
    append(body, generation, source, behavior, data); advanced.append(body); form.append(advanced); configActions(form);
    const aside = el('aside', 'settings-aside');
    for (const [idx, title, desc] of [['01', '模型负责判断', 'Jev 用对话语境理解意图、需求和风险。模型服务的实际能力决定可用的分析与生成功能。'], ['02', '窗口负责倾听', '保持电脑微信的聊天窗口可见。需要后台读取更多会话时，可切换到你已有的 WeFlow 服务。'], ['03', '你保留决定权', '知弦只给出分析和候选。点击回填时会校验当前会话，最终发送始终由你完成。']]) aside.append(append(el('div', 'settings-help'), el('div', 'help-index', idx), el('h3', '', title), el('p', '', desc)));
    append(grid, form, aside); root.append(grid); return root;
  }

  function collection(kind) {
    const notes = kind === 'knowledge', items = notes ? currentState.notes : currentState.contacts;
    const root = el('section', 'page collection-page');
    const actions = notes ? [button('导入文本', 'upload', importNotes, '', !available()), button('添加知识', 'plus', () => noteDialog(), 'primary', !available())] : [button('添加联系人', 'plus', () => contactDialog(), 'primary', !available())];
    root.append(heading(notes ? '背景知识库' : '联系人', notes ? '为分析补充事实、偏好和常用背景。' : '记录关系与沟通习惯，自动匹配当前会话。', null, actions));
    const input = el('input', 'input'); input.id = 'collection-search'; input.placeholder = notes ? '搜索标题、内容或标签' : '搜索姓名、别名或关系'; input.setAttribute('aria-label', input.placeholder); input.value = search;
    input.addEventListener('input', () => { search = input.value; renderCollectionItems(root, kind); });
    root.append(append(el('div', 'collection-toolbar'), append(el('div', 'search-wrap'), icon('search', 16), input), el('span', 'collection-count', `${items.length} ${notes ? '条背景知识' : '位联系人'}`)));
    root.append(el('div', 'collection-results')); renderCollectionItems(root, kind); return root;
  }
  function renderCollectionItems(root, kind) {
    const notes = kind === 'knowledge', items = notes ? currentState.notes : currentState.contacts;
    const filtered = items.filter(item => JSON.stringify(item).toLowerCase().includes(search.trim().toLowerCase()));
    const results = $('.collection-results', root); results.replaceChildren();
    if (!filtered.length) { results.append(append(el('div', 'collection-empty'), empty(search ? '没有找到匹配内容' : notes ? '了解你，从背景开始' : '给重要的关系，留一份备注', search ? '换个关键词试试，或添加新的条目。' : notes ? '可以记录工作安排、个人偏好、项目资料，或一段你希望助手记住的背景。' : '称呼、你们的关系、最近在谈的事，都能帮助模型给出更合适的判断。', notes ? 'book' : 'people', !search ? button(notes ? '添加第一条知识' : '添加第一位联系人', 'plus', () => notes ? noteDialog() : contactDialog(), 'small', !available()) : null))); return; }
    const grid = el('div', 'collection-grid');
    for (const item of filtered) {
      const card = el('article', `item-card${notes ? '' : ' contact-card'}`);
      const tools = append(el('div', 'item-actions'), iconButton('编辑', 'edit', () => notes ? noteDialog(item) : contactDialog(item)), iconButton('删除', 'trash', () => confirmDialog(notes ? '删除这条知识' : '删除这位联系人', `删除后，分析时将不再使用「${item.title || item.name}」的背景信息。`, () => action(notes ? 'delete_note' : 'delete_contact', { id: item.id }, '已删除')), 'danger'));
      if (!notes) card.append(el('div', 'contact-avatar', (item.name || '联').slice(0, 1)));
      card.append(append(el('div', 'item-top'), el('h3', '', notes ? item.title : item.name), tools));
      if (!notes && item.aliases?.length) card.append(el('div', 'contact-aliases', `匹配别名：${Array.isArray(item.aliases) ? item.aliases.join('、') : item.aliases}`));
      card.append(el('p', 'item-content', (notes ? item.content : item.notes) || '尚未添加备注'));
      const tags = el('div', 'item-tags');
      if (notes) { if (item.always) tags.append(tag('每次分析使用', 'mint')); for (const text of Array.isArray(item.tags) ? item.tags : String(item.tags || '').split(/[,，]/).filter(Boolean)) tags.append(tag(text)); }
      else if (item.relationship) tags.append(tag(item.relationship, 'mint'));
      card.append(tags); grid.append(card);
    }
    results.append(grid);
  }
  function showDialog(title) {
    const dialog = $('#editor'); if (dialog.open) dialog.close(); dialog.replaceChildren();
    dialog.append(append(el('div', 'dialog-head'), el('h2', '', title), iconButton('关闭', 'close', () => dialog.close())));
    const body = el('div', 'dialog-body'); dialog.append(body); dialog.showModal(); return { dialog, body };
  }
  function noteDialog(note = {}) {
    const { dialog, body } = showDialog(note.id ? '编辑背景知识' : '添加背景知识'), form = el('form');
    append(form, field('标题', 'note-title', note.title, { placeholder: '例如：近期工作安排' }), field('具体内容', 'note-content', note.content, { rows: 7, placeholder: '补充希望模型了解的事实、偏好或背景…' }), field('标签', 'note-tags', Array.isArray(note.tags) ? note.tags.join('，') : note.tags, { placeholder: '用逗号分隔，例如：工作，项目', optional: true }), toggle('每次分析都使用', 'note-always', note.always, '关闭后，由会话内容匹配相关知识。'));
    const save = button('保存知识', 'check', async () => {
      const title = $('#note-title').value.trim(), content = $('#note-content').value.trim();
      if (!title || !content) { formFeedback(form, '请填写标题和具体内容。', true); return; }
      try { await rpc('save_note', { note: { ...(note.id ? { id: note.id } : {}), title, content, tags: $('#note-tags').value.split(/[,，]/).map(t => t.trim()).filter(Boolean), always: $('#note-always').checked } }); dialog.close(); await sync(); toast('背景知识已保存'); } catch (err) { formFeedback(form, errorText(err), true); }
    }, 'primary');
    form.append(append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), save)); form.addEventListener('submit', e => { e.preventDefault(); save.click(); }); body.append(form);
  }
  function contactDialog(contact = {}) {
    const { dialog, body } = showDialog(contact.id ? '编辑联系人' : '添加联系人'), form = el('form');
    append(form, field('微信会话名称', 'contact-name', contact.name, { placeholder: '填写微信中显示的名称', hint: '名称用于匹配读取到的微信会话。' }), field('别名', 'contact-aliases', Array.isArray(contact.aliases) ? contact.aliases.join('，') : contact.aliases, { placeholder: '用逗号分隔其他可能显示的名称', optional: true }), field('你们的关系', 'contact-relationship', contact.relationship, { placeholder: '例如：项目同事、朋友、客户' }), field('沟通背景', 'contact-notes', contact.notes, { rows: 5, optional: true, placeholder: '记录对方的沟通偏好、近期在讨论的事…' }));
    const save = button('保存联系人', 'check', async () => {
      const name = $('#contact-name').value.trim(); if (!name) { formFeedback(form, '请填写微信会话名称。', true); return; }
      try { await rpc('save_contact', { contact: { ...(contact.id ? { id: contact.id } : {}), name, aliases: $('#contact-aliases').value.split(/[,，]/).map(s => s.trim()).filter(Boolean), relationship: $('#contact-relationship').value.trim(), notes: $('#contact-notes').value.trim() } }); dialog.close(); await sync(); toast('联系人已保存'); } catch (err) { formFeedback(form, errorText(err), true); }
    }, 'primary');
    form.append(append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), save)); form.addEventListener('submit', e => { e.preventDefault(); save.click(); }); body.append(form);
  }
  function confirmDialog(title, desc, confirm) {
    const { dialog, body } = showDialog(title); body.append(el('p', 'dialog-copy', desc));
    body.append(append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), button('确认清除', 'trash', async () => { await confirm(); dialog.close(); }, 'danger')));
  }
  function manualDialog() {
    const { dialog, body } = showDialog('补充一段对话'), form = el('form');
    form.append(el('p', 'dialog-copy', '用于补充未能识别的上下文，或分析一段你主动提供的对话。手动文本不会改变微信中的消息。'));
    form.append(field('会话名称', 'manual-title', currentState.current_session?.title || '', { placeholder: '这段对话的联系人或主题' }), field('对话内容', 'manual-text', '', { rows: 9, placeholder: '对方：明天的方案可以再聊聊吗？\n我：可以，你想重点看哪一部分？' }));
    const save = button('加入工作台', 'arrow', async () => {
      const title = $('#manual-title').value.trim(), text = $('#manual-text').value.trim(); if (!title || !text) { formFeedback(form, '请填写会话名称和对话内容。', true); return; }
      try { await rpc('manual_context', { title, text }); dialog.close(); await sync(); toast('对话已加入，可点击「分析当前」'); } catch (err) { formFeedback(form, errorText(err), true); }
    }, 'primary');
    form.append(append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), save)); form.addEventListener('submit', e => { e.preventDefault(); save.click(); }); body.append(form);
  }
  function importNotes() {
    const input = document.createElement('input'); input.type = 'file'; input.accept = '.txt,.md,text/plain,text/markdown';
    input.addEventListener('change', async () => {
      const file = input.files?.[0]; if (!file) return;
      if (file.size > 2 * 1024 * 1024) { toast('文件较大，请选择小于 2 MB 的文本文件。', 'error'); return; }
      try { const text = await file.text(); if (!text.trim()) throw new Error('文件内容为空。'); await action('import_notes', { text }, '知识库文本已导入'); } catch (err) { toast(errorText(err), 'error'); }
    }); input.click();
  }

  let demoSnapshot;
  function initializeDemo() {
    demoSnapshot = blank(); demoSnapshot.config = { ...demoSnapshot.config, has_api_key: true, base_url: 'https://demo.example.invalid/v1', model_name: 'jev-demo', auto_analyze: true };
    const now = new Date();
    const messages = [
      ['other', '林以宁', '明天提案的结构我重新看了一遍，整体方向没有问题。', -14],
      ['me', '我', '太好了。我把案例部分也补充了，稍后发你新版。', -12],
      ['other', '林以宁', '嗯，不过开头的用户洞察，感觉还是有一点抽象。', -8],
      ['other', '林以宁', '客户之前很在意落地，你觉得要不要放个更具体的场景？', -5],
      ['other', '林以宁', '如果今晚能一起过一下就更好了，十几分钟应该就够。', -1],
    ].map(([side, sender, text, offset], i) => ({ id: `demo-message-${i}`, side, sender, text, timestamp: new Date(now.getTime() + offset * 60000).toISOString(), source: 'ocr' }));
    demoSnapshot.current_session = { id: 'demo-session-1', title: '林以宁', messages, source: 'ocr', active: true };
    demoSnapshot.sessions = [{ id: 'demo-session-1', title: '林以宁', count: messages.length, preview: messages.at(-1).text, updated: now.toISOString(), source: 'ocr' }];
    demoSnapshot.status = { capture: 'live', analysis: 'idle', detail: '演示模式 · 所有对话与分析均为合成数据', last_error: '', source: 'ocr', connected: false };
    demoSnapshot.analysis = demoAnalysis('demo-session-1');
    demoSnapshot.notes = [{ id: 'demo-note-1', title: '品牌提案 · 本周背景', content: '客户更重视具体业务场景。第一轮提案聚焦用户洞察和可执行的路径，避免过多概念表达。', tags: ['工作', '提案'], always: false }];
    demoSnapshot.contacts = [{ id: 'demo-contact-1', name: '林以宁', aliases: ['以宁'], relationship: '项目同事', notes: '负责客户沟通，喜欢先确认方向再讨论细节。' }];
  }
  function demoAnalysis(sessionId) {
    return { session_id: sessionId, intent: '对方想一起把方案说得更具体，也在确认你今晚是否方便。', need: '用真实场景支撑洞察，降低明天提案的不确定性。', action: '先接住建议，再给出明确的时间与准备动作。', should_reply: true, urgency: null, risk: 1, confidence: null, answers: { literal_question: { noul: .91 }, true_intent: { choice: 'request_action' }, danger_level: { score: 1 }, should_reply_now: { noul: .84 }, best_action: { choice: 'make_plan' }, she_needs: { choice: 'action' }, tension_resolved: { noul: .93 } }, candidates: [
      { text: '我也觉得加一个具体场景会更有说服力。我先把开头改一版，今晚 8 点一起过一下，你方便吗？', score: null, reason: '回应具体建议，并主动给出下一步和可商量的时间。' },
      { text: '可以，我们就从客户最在意的使用场景切入。你今晚哪个时间方便？我提前把案例和洞察整理到一起。', score: null, reason: '给对方留出时间选择，同时明确自己的准备工作。' },
      { text: '你提的这个点很关键。我先补一个完整的用户场景，晚些时候我们花十几分钟对一遍。', score: null, reason: '表达认可、承诺行动，语气自然，具体时间可继续确认。' },
    ], best_index: 0, usage: null, latency_ms: 1840, updated_at: new Date().toISOString(), warning: '' };
  }
  async function demoRequest(method, params) {
    if (!demoSnapshot) initializeDemo();
    if (method === 'bootstrap') return structuredClone(demoSnapshot);
    if (method === 'test_connection') return { success: true, message: '演示连接已通过。这是模拟结果，未请求任何模型服务。', mode: 'demo', generation_available: true };
    if (method === 'save_config') { const c = { ...params.config }; for (const [secret, flag] of [['api_key', 'has_api_key'], ['reply_api_key', 'has_reply_api_key'], ['weflow_token', 'weflow_has_token']]) { if (c[secret]) c[flag] = true; delete c[secret]; } Object.assign(demoSnapshot.config, c); }
    else if (method === 'start_capture') { demoSnapshot.status.capture = 'live'; if (demoSnapshot.current_session?.source === 'ocr') demoSnapshot.current_session.active = true; }
    else if (method === 'pause_capture') { demoSnapshot.status.capture = 'paused'; if (demoSnapshot.current_session) demoSnapshot.current_session.active = false; }
    else if (method === 'analyze') {
      demoSnapshot.status.analysis = 'running'; setState(structuredClone(demoSnapshot));
      await new Promise(resolve => setTimeout(resolve, 1300));
      demoSnapshot.analysis = demoAnalysis(params.session_id || demoSnapshot.current_session?.id); demoSnapshot.status.analysis = 'idle';
    } else if (method === 'copy_reply') { if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(params.text); else { const t = el('textarea'); t.value = params.text; document.body.append(t); t.select(); const copied = document.execCommand('copy'); t.remove(); if (!copied) throw new Error('浏览器未允许复制，请使用桌面版。'); } }
    else if (method === 'fill_reply') { /* Intentionally no external action in demo mode. */ }
    else if (method === 'set_compact') return { enabled: params.enabled };
    else if (method === 'save_note' || method === 'save_contact') {
      const note = method === 'save_note', list = note ? demoSnapshot.notes : demoSnapshot.contacts, item = { ...(note ? params.note : params.contact) };
      if (!item.id) item.id = `demo-${Date.now()}`;
      const idx = list.findIndex(i => i.id === item.id); if (idx >= 0) list[idx] = item; else list.push(item);
    } else if (method === 'delete_note' || method === 'delete_contact') { const key = method === 'delete_note' ? 'notes' : 'contacts'; demoSnapshot[key] = demoSnapshot[key].filter(i => i.id !== params.id); }
    else if (method === 'clear_history') { demoSnapshot.sessions = []; demoSnapshot.current_session = null; demoSnapshot.analysis = null; }
    else if (method === 'manual_context') {
      const id = `demo-session-${Date.now()}`;
      const messages = params.text.split('\n').filter(Boolean).map((line, i) => { const me = /^我[:：]/.test(line); return { id: `${id}-${i}`, side: me ? 'me' : 'other', sender: me ? '我' : params.title, text: line.replace(/^(我|对方)[:：]\s*/, ''), timestamp: new Date().toISOString(), source: 'manual' }; });
      demoSnapshot.current_session = { id, title: params.title, messages, source: 'manual', active: false }; demoSnapshot.sessions.push({ id, title: params.title, count: messages.length, source: 'manual' }); demoSnapshot.analysis = null;
    } else if (method === 'select_session') {
      if (params.session_id === 'demo-session-1') { const notes = demoSnapshot.notes, contacts = demoSnapshot.contacts, config = demoSnapshot.config; initializeDemo(); Object.assign(demoSnapshot, { notes, contacts, config }); }
      else if (params.session_id !== demoSnapshot.current_session?.id) throw new Error('演示只保留当前手动会话；完整会话存储由桌面版提供。');
    } else if (method === 'import_notes') demoSnapshot.notes.push({ id: `demo-note-${Date.now()}`, title: '导入的背景文本', content: params.text, tags: ['导入'], always: false });
    else if (method === 'refresh_sources') return { sources: ['演示微信窗口（合成数据）'] };
    else if (method === 'one_shot') { toast('演示模式：当前消息来自合成数据，未读取微信。'); }
    else if (method === 'open_data_folder') toast('演示模式没有本地数据文件夹。');
    else throw new Error(`演示模式未实现操作：${method}`);
    setState(structuredClone(demoSnapshot)); return { success: true };
  }
  function offline() {
    if (online || demo) return;
    const n = $('#offline-notice'); n.hidden = false; n.replaceChildren(icon('window', 15), el('span', '', '当前为浏览器预览。请通过项目中的「启动知弦」桌面入口打开，以连接微信和本地模型设置。'));
    render(true);
  }
  function connect() {
    if (demo) { online = true; initializeDemo(); setState(structuredClone(demoSnapshot)); return; }
    if (typeof qt !== 'undefined' && typeof QWebChannel !== 'undefined') {
      new QWebChannel(qt.webChannelTransport, channel => {
        bridge = channel.objects.bridge; if (!bridge) { offline(); return; } online = true;
        bridge.response.connect(raw => {
          try { const response = typeof raw === 'string' ? JSON.parse(raw) : raw; const wait = pending.get(String(response.id)); if (!wait) return; clearTimeout(wait.timeout); pending.delete(String(response.id)); response.ok ? wait.resolve(response.data) : wait.reject(new Error(typeof response.error === 'string' ? response.error : response.error?.message || '操作未完成。')); } catch { toast('桌面服务返回的数据无法读取。', 'error'); }
        });
        bridge.event.connect(raw => {
          try { const event = typeof raw === 'string' ? JSON.parse(raw) : raw; if (event.type === 'state') setState(event.data); if (event.type === 'toast') toast(event.data?.message || '', event.data?.level); } catch { /* Ignore malformed events; never expose their raw content. */ }
        });
        sync().catch(err => { toast(errorText(err), 'error'); render(true); });
      });
    } else offline();
  }
  $('#compact-button').append(icon('compact', 20), el('span', '', '悬浮'));
  $('#compact-button').addEventListener('click', () => toggleCompact().catch(err => toast(errorText(err), 'error')));
  $('#mini-expand').append(icon('expand', 16));
  $('#mini-expand').addEventListener('click', () => toggleCompact().catch(err => toast(errorText(err), 'error')));
  $('.brand').addEventListener('click', e => { e.preventDefault(); go('workspace'); });
  $('#editor').addEventListener('click', e => { if (e.target === $('#editor')) { const r = $('#editor').getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) $('#editor').close(); } });
  document.addEventListener('keydown', e => { if ((e.ctrlKey || e.metaKey) && e.key === ',') { e.preventDefault(); go('settings'); } });
  $('#app').classList.toggle('compact', compact);
  window.addEventListener('resize', () => {
    const next = window.innerWidth <= 640;
    if (next !== compact) { compact = next; $('#app').classList.toggle('compact', compact); render(); }
  });
  connect();
})();
