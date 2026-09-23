'use strict';

/* Offline presentation and optional data-source features. All content is textContent. */
window.ZhixianExperience = Object.freeze({
  mount(api) {
    const { $, el, append, icon, button, iconButton, notice, empty, heading, tag, rpc, sync, setState, getState, getPage, go, render, toast, errorText, formatTime, valueText, available, showDialog, manualDialog, field, demo } = api;
    const catalog = { items: [], query: '', filter: 'all', cursor: null, more: false, total: null, busy: false, available: false, source: '', serial: 0, onSelect: null };
    const moments = { items: [], cursor: null, more: false, loaded: false, busy: false, available: false, source: '', detail: '', error: '', friend: null, results: new Map(), running: new Set(), serial: 0 };
    const histories = new Map(), momentMedia = new Map();
    let appearance = { theme: 'night', font_scale: 1, background_url: '' }, introTimer, searchTimer, importBusy = false;

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

    function openCatalog(onSelect = null) {
      const dialog = $('#session-catalog');
      if (dialog.open) { $('#session-search')?.focus({ preventScroll: true }); return; }
      catalog.serial++; catalog.items = []; catalog.query = ''; catalog.filter = 'all'; catalog.cursor = null; catalog.more = false; catalog.onSelect = onSelect; catalog.busy = false;
      dialog.replaceChildren();
      const input = el('input', 'input'); input.id = 'session-search'; input.type = 'search'; input.placeholder = '搜索好友、群聊或会话名称'; input.autocomplete = 'off'; input.setAttribute('aria-label', input.placeholder);
      input.addEventListener('input', () => { clearTimeout(searchTimer); catalog.query = input.value; catalog.serial++; catalog.busy = false; searchTimer = setTimeout(() => fetchCatalog(true), 220); });
      const filters = el('div', 'catalog-filters');
      for (const [value, label] of [['all', '全部可用'], ['weflow', 'WeFlow'], ['import', '已导入'], ['collected', '已采集']]) {
        const filter = el('button', `catalog-filter${value === 'all' ? ' active' : ''}`, label); filter.type = 'button'; filter.dataset.filter = value;
        filter.addEventListener('click', () => { catalog.filter = value; catalog.serial++; catalog.busy = false; filters.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === filter)); fetchCatalog(true); }); filters.append(filter);
      }
      append(dialog,
        append(el('div', 'catalog-heading'), append(el('div'), el('div', 'eyebrow', onSelect ? '选择联系人' : '你的对话空间'), el('h2', '', onSelect ? '选择好友' : '全局会话')), iconButton('关闭会话窗口', 'close', () => dialog.close())),
        append(el('div', 'catalog-search'), icon('search', 18), input),
        filters, el('div', 'catalog-source'), el('div', 'catalog-list'), el('div', 'catalog-pagination'),
        append(el('div', 'catalog-footer'), el('span', 'catalog-import-status', importBusy ? '正在本机索引，可关闭此窗口继续使用' : '支持 CipherTalk / ChatLab 导出的 JSON、JSONL'), append(el('div', 'catalog-import-actions'), button('粘贴对话', 'pen', () => { dialog.close(); manualDialog(); }, 'small ghost'), importButton('files', '导入文件', 'upload'), importButton('folder', '导入目录', 'folder'))));
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
      else if (!catalog.available && !catalog.busy) source.append(notice(catalog.detail || '当前仅覆盖已读取的可见聊天。连接支持目录的 WeFlow，或导入 CipherTalk / ChatLab 导出的文件或目录；此页面不会自动读取微信数据库。', 'info'));
      else { if (catalog.detail && !catalog.busy) source.append(notice(catalog.detail, 'info')); source.append(append(el('div', 'catalog-meta'), el('span', '', catalog.source === 'demo' ? '合成演示会话' : catalog.source === 'weflow' ? 'WeFlow 会话目录' : '当前可用会话'), el('span', '', catalog.total === null ? `已加载 ${catalog.items.length} 个` : `${catalog.total} 个会话`))); }
      if (catalog.busy && !catalog.items.length) list.append(append(el('div', 'catalog-loading'), el('div', 'skeleton wide'), el('div', 'skeleton medium'), el('div', 'skeleton wide')));
      else if (!catalog.items.length) list.append(empty(catalog.query ? '没有找到匹配的会话' : '还没有可用的会话', catalog.query ? '尝试换一个名字，或手动导入对话。' : '打开微信并读取一次，或在设置中连接会话数据源。', 'chat'));
      for (const item of catalog.items) {
        const group = Boolean(item.type === 'group' || item.is_group || String(item.id).endsWith('@chatroom'));
        const row = el('button', `catalog-row${item.id === getState().current_session?.id ? ' selected' : ''}${item.preview_status === 'pending' ? ' preview-pending' : ''}`); row.type = 'button'; row.dataset.sessionId = String(item.id);
        const name = displayName(item);
        append(row, append(el('span', `catalog-avatar${group ? ' group-avatar' : ''}`), group ? icon('people', 19) : el('span', '', name.slice(0, 1))), append(el('span', 'catalog-copy'), append(el('span', 'catalog-title'), el('span', '', name), group ? tag('群聊') : null), el('span', 'catalog-preview', previewText(item))), append(el('span', 'catalog-trailing'), el('time', '', formatTime(item.updated || item.timestamp)), item.id === getState().current_session?.id ? icon('check', 14) : icon('arrow', 14)));
        row.addEventListener('click', async () => {
          row.disabled = true; row.classList.add('selecting');
          try { if (catalog.onSelect) await catalog.onSelect(item); else { await rpc('select_session', { session_id: item.id }); await sync(); go('workspace'); } dialog.close(); }
          catch (err) { toast(errorText(err), 'error'); row.disabled = false; row.classList.remove('selecting'); }
        }); list.append(row);
      }
      list.scrollTop = oldTop;
      const footer = $('.catalog-pagination', dialog); footer.replaceChildren();
      if (catalog.more) footer.append(button(catalog.busy ? '正在加载…' : '加载更多会话', 'chevron', () => fetchCatalog(false), 'small', catalog.busy));
      else if (catalog.items.length && !catalog.busy) footer.append(el('span', 'subtle-caption', catalog.available ? '已显示当前目录中的全部匹配会话' : '以上为当前已读取的会话'));
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
    return { updateState, reconcileSnapshot, renderAppearance, renderMoments, openCatalog, messageContent, historyControl, chooseMedia, startIntro };
  }
});
