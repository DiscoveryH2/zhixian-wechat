'use strict';

window.ZhixianRelationships = Object.freeze({
  mount(api) {
    const { $, el, append, button, heading, notice, empty, rpc, sync, getState, getPage, go, render, toast, errorText, showDialog, field, available, demo, openCatalog } = api;
    const analysis = { selected: null, params: {}, statistics: null, result: null, busy: false, error: '', question: '', progress: '', cancel: false };
    const twins = { items: [], selected: null, loaded: false, busy: false, error: '', draft: '', cursor: null, more: false };
    const repaint = () => { if (['insights', 'personas'].includes(getPage())) render(true); };
    const modes = { companion: '陪伴对话', memorial: '纪念对话', rehearsal: '沟通排练' };
    const date = value => value ? new Date(value * 1000).toLocaleDateString() : '时间未提供';

    function contactPicker(onChoose) {
      const { dialog, body } = showDialog('选择有稳定帐号的联系人');
      const input = el('input', 'input'); input.type = 'search'; input.placeholder = '搜索名字或帐号 ID'; input.setAttribute('aria-label', input.placeholder);
      const host = el('div', 'identity-list'); let serial = 0, cursor = null, items = [], timer;
      body.append(input, notice('只关联同一本人帐号下的相同发送者 ID。群成员也会进入目录，同名不会合并。', 'info'), host);
      async function load(reset = true) {
        const request = ++serial;
        host.setAttribute('aria-busy', 'true');
        try {
          const page = await rpc('list_relationship_contacts', { query: input.value, cursor: reset ? null : cursor, limit: 24 });
          if (request !== serial || !dialog.open) return;
          items = reset ? page.items : [...items, ...page.items]; cursor = page.next_cursor;
          host.replaceChildren();
          if (!items.length) host.append(empty('还没有可关联的联系人', '先导入私聊或群聊，或在洞察页索引本机数据库会话。', 'people', button('导入聊天记录', 'upload', async () => { await rpc('import_chat_records'); await load(); }, 'small')));
          for (const item of items) host.append(button(`${item.name} · ${item.username} · 本人 ${item.owner}`, 'people', async () => { dialog.close(); await onChoose(item); }, 'identity-choice'));
          if (page.has_more) host.append(button('加载更多联系人', 'chevron', () => load(false), 'small'));
        } catch (err) { if (request === serial) host.replaceChildren(notice(errorText(err), 'error')); }
        finally { if (request === serial) host.removeAttribute('aria-busy'); }
      }
      input.addEventListener('input', () => { serial++; clearTimeout(timer); timer = setTimeout(() => load(), 250); });
      dialog.addEventListener('close', () => { serial++; clearTimeout(timer); }, { once: true });
      load(); input.focus();
    }

    async function selectAnalysis(item, params) {
      if (analysis.busy) return;
      analysis.selected = item; analysis.params = params; analysis.result = null; analysis.statistics = null; analysis.error = ''; analysis.busy = true; repaint();
      try { analysis.statistics = await rpc('history_statistics', params); }
      catch (err) { analysis.error = errorText(err); }
      finally { analysis.busy = false; repaint(); }
    }

    function selectArchive() {
      openCatalog(async item => {
        $('#session-catalog').close();
        analysis.cancel = false; analysis.busy = true; analysis.error = ''; analysis.progress = '正在索引所选会话…'; repaint();
        let page;
        try {
          let count = 0;
          do {
            page = await rpc('sync_analysis_archive', { session_id: item.id });
            count += page.indexed || 0;
            analysis.progress = `本次已处理 ${count.toLocaleString()} 条，${page.complete ? '快照索引完成' : '继续读取更早记录…'}`; repaint();
          } while (!page.complete && !analysis.cancel);
          if (analysis.cancel && !page.complete) { analysis.progress = `本次已处理 ${count.toLocaleString()} 条，索引已暂停，断点已保存。`; toast('索引已暂停，断点保存在本机；再次选择同一会话可继续。'); }
        } catch (err) { analysis.error = errorText(err); }
        finally { analysis.busy = false; repaint(); }
        if (page?.archive_id) await selectAnalysis({ ...item, id: page.archive_id }, { session_id: page.archive_id });
      });
    }

    function evidenceView(record) {
      const block = el('div', 'source-memory');
      append(block, el('span', 'eyebrow', record.interpretation === 'model_media_interpretation' ? '媒体识别文字' : record.source_kind === 'moment' ? '朋友圈原文' : record.source_kind === 'group' ? '群聊原文' : '私聊原文'),
        el('blockquote', '', record.quote || record.text), el('p', 'field-hint', `${record.source_title || ''} · ${record.sender || ''}${record.timestamp ? ' · '+date(record.timestamp) : ''}`));
      if (record.interpretation === 'model_media_interpretation') block.append(notice('媒体识别文字，可能有误，请核对原图片或语音。', 'info'));
      if (record.session_id) block.append(button('定位原始消息', 'chat', async () => {
        await rpc('select_search_result', { session_id: record.session_id, message_id: record.id }); await sync(); go('workspace');
        for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
      }, 'small ghost'));
      return block;
    }

    function statCard(value, title) { return append(el('div', 'panel insight-stat'), el('strong', '', Number(value || 0).toLocaleString()), el('span', '', title)); }
    function renderInsights() {
      const root = el('section', 'page insights-page');
      root.append(heading('历史洞察', '群聊结构、个人沟通与朋友圈，通过稳定身份和原始证据联系起来。', 'RELATIONSHIP OBSERVATORY', [
        button('选择群聊 / 会话', 'chat', selectArchive, 'subtle', analysis.busy || !available()),
        button('选择联系人', 'people', () => contactPicker(item => selectAnalysis(item, { contact_id: item.id })), 'primary', analysis.busy || !available())]));
      if (demo) { root.append(notice('历史洞察和数字分身需在桌面版导入记录后使用；当前演示未读取任何真实档案。', 'info')); return root; }
      if (analysis.error) root.append(notice(analysis.error, 'error'));
      if (analysis.progress) root.append(append(el('div', 'notice notice-info index-progress'), el('span', '', analysis.progress), analysis.busy ? button('暂停索引', 'pause', () => { analysis.cancel = true; }, 'small ghost') : null));
      if (!analysis.statistics) { root.append(empty(analysis.busy ? '正在计算完整索引…' : '从完整记录中寻找下一步', '联系人洞察关联私聊、共同群聊里的本人发言和已索引朋友圈；群聊洞察分析所选群的全部已索引消息。', 'pulse')); return root; }
      const stats = analysis.statistics;
      root.append(el('h2', '', analysis.selected.name || analysis.selected.title || '所选记录'), notice(stats.warning, 'info'));
      root.append(append(el('div', 'insight-stats'), statCard(stats.messages, '已索引消息'), statCard(stats.sessions.length, '关联会话'), statCard(stats.speaker_count, '稳定发送者'), statCard(stats.moments, '朋友圈动态')));
      root.append(el('p', 'field-hint', `${date(stats.first_at)} — ${date(stats.last_at)} · 文字 ${stats.text_messages} · 图片/语音 ${stats.media_messages}（已理解 ${stats.understood_media}） · 方向未知 ${stats.unknown_direction}`));
      const panels = el('div', 'insight-panels'), people = el('section', 'panel insight-panel'), sources = el('section', 'panel insight-panel');
      people.append(el('h3', '', '发言分布'));
      for (const speaker of stats.speakers.slice(0, 12)) {
        const row = el('div', 'speaker-row'), meter = el('meter'); meter.min = 0; meter.max = stats.messages || 1; meter.value = speaker.messages;
        append(row, el('span', '', speaker.name || '身份未知'), meter, el('strong', '', String(speaker.messages))); people.append(row);
      }
      sources.append(el('h3', '', '证据来源'));
      for (const source of stats.sessions.slice(0, 20)) sources.append(append(el('div', 'source-row'), el('span', '', `${source.type === 'group' ? '群聊' : '私聊'} · ${source.title}`), el('strong', '', String(source.messages))));
      if (stats.sessions.length > 20) sources.append(el('p', 'field-hint', `另有 ${stats.sessions.length-20} 个来源参与统计。`));
      panels.append(people, sources); root.append(panels);
      const local = el('section', 'panel insight-panel'); local.append(el('h3', '', '先处理这些可见问题'));
      if (stats.unknown_direction) local.append(el('p', '', `${stats.unknown_direction} 条消息无法确定方向：补齐发送者身份后，再用于回复或分身。`));
      if (stats.uninterpreted_media) local.append(el('p', '', `${stats.uninterpreted_media} 条图片/语音尚未进入文字分析：先识别与关键决定有关的媒体，再判断需求。`));
      const dominant = stats.speakers[0];
      if (analysis.params.session_id && dominant && stats.messages > 20 && dominant.messages / stats.messages > .7) local.append(el('p', '', `${dominant.name} 贡献了 ${Math.round(dominant.messages/stats.messages*100)}% 的消息。讨论收敛前，可邀请其他成员明确确认各自负责的事项。`));
      local.append(el('p', 'field-hint', '发言量只描述记录分布，不代表重要性、亲密度或真实心理。')); root.append(local);
      const form = el('section', 'panel insight-panel');
      form.append(field('这次想弄清楚什么', 'history-question', analysis.question, { rows: 2, optional: true, placeholder: '例如：这个群有哪些待明确的需求？我应该如何推进沟通？' }));
      form.querySelector('textarea').addEventListener('input', event => { analysis.question = event.target.value; });
      form.append(button(analysis.busy ? '正在核对来源…' : '生成可执行洞察', 'spark', async () => {
        analysis.busy = true; analysis.error = ''; repaint();
        try { analysis.result = await rpc('analyze_history', { ...analysis.params, question: analysis.question }); }
        catch (err) { analysis.error = errorText(err); }
        finally { analysis.busy = false; repaint(); }
      }, 'primary', analysis.busy || !(stats.text_messages || stats.understood_media || stats.moments)), el('p', 'field-hint', '点击后把完整统计与跨时间抽样文字交给配置的聊天模型。模型不会自动创建任务或发送消息。')); root.append(form);
      if (analysis.result) {
        root.append(notice(`${analysis.result.warning} 本次使用 ${analysis.result.sampled_records} 条抽样来源。`, 'info'));
        for (const insight of analysis.result.insights) {
          const card = el('article', 'panel insight-card'); append(card, el('h3', '', insight.title), el('p', '', insight.observation), el('p', 'next-action', `下一步：${insight.action}`));
          const evidence = el('details', 'insight-evidence'); evidence.append(el('summary', '', `核对 ${insight.evidence.length} 条原始依据`));
          for (const record of insight.evidence) evidence.append(evidenceView(record)); card.append(evidence); root.append(card);
        }
        if (!analysis.result.insights.length) root.append(empty('暂无有充分依据的结论', '更换问题或补齐关键记录后再分析。', 'info'));
      }
      return root;
    }

    async function loadTwins(reset = true) {
      if (twins.busy) return;
      twins.busy = true; twins.error = ''; repaint();
      try { const page = await rpc('list_personas', { cursor: reset ? null : twins.cursor, limit: 24 }); twins.items = reset ? page.items : [...twins.items, ...page.items]; twins.cursor = page.next_cursor; twins.more = page.has_more; twins.loaded = true; }
      catch (err) { twins.error = errorText(err); }
      finally { twins.busy = false; repaint(); }
    }
    async function selectTwin(id) { twins.selected = await rpc('get_persona', { id }); twins.draft = ''; repaint(); }
    function createTwin() {
      contactPicker(contact => {
        const { dialog, body } = showDialog(`为 ${contact.name} 创建数字分身`), form = el('form');
        append(form, notice('用这位联系人本人说过的话建立可核对的模拟。聊天不会发送到微信；生成对话不会写回原始记忆。', 'info'),
          field('分身名称', 'persona-name', contact.name),
          field('体验模式', 'persona-mode', 'companion', { options: Object.entries(modes) }),
          field('共同群聊中的本人发言', 'persona-groups', 'yes', { options: [['yes','纳入'],['no','不纳入']] }),
          field('已索引朋友圈文字', 'persona-moments', 'yes', { options: [['yes','纳入'],['no','不纳入']] }),
          el('p', 'field-hint', `稳定帐号：${contact.username} · 本人帐号：${contact.owner}`));
        const save = button('建立来源记忆', 'spark', async () => {
          const item = await rpc('create_persona', { contact_id: contact.id, name: $('#persona-name').value, mode: $('#persona-mode').value, include_groups: $('#persona-groups').value === 'yes', include_moments: $('#persona-moments').value === 'yes' });
          dialog.close(); await loadTwins(); twins.selected = item; twins.draft = ''; repaint();
        }, 'primary'); form.append(append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), save));
        form.addEventListener('submit', event => { event.preventDefault(); save.click(); }); body.append(form);
      });
    }

    function manageMemories() {
      const pid = twins.selected.id, { dialog, body } = showDialog('分身的来源记忆');
      const host = el('div', 'memory-list'); body.append(notice('这些原始文字只作为模拟的来源。停用一条后会清除分身对话历史，防止旧回复重新带回被移除的记忆。原聊天档案会保留。', 'info'), host);
      let cursor = null, items = [];
      async function load(reset = true) {
        const page = await rpc('persona_memories', { id: pid, cursor: reset ? null : cursor, limit: 16 });
        if (!dialog.open) return;
        items = reset ? page.items : [...items, ...page.items]; cursor = page.next_cursor; host.replaceChildren();
        for (const item of items) {
          const memory = evidenceView(item); memory.classList.toggle('memory-inactive', !item.active);
          memory.append(button(item.active ? '停用这条记忆' : '恢复这条记忆', item.active ? 'eyeoff' : 'eye', async () => {
            await rpc('set_persona_memory', { id: pid, memory_id: item.id, active: !item.active });
            twins.selected = await rpc('get_persona', { id: pid }); await load(); repaint();
          }, 'small ghost')); host.append(memory);
        }
        if (page.has_more) host.append(button('加载更多记忆', 'chevron', () => load(false), 'small'));
      }
      load().catch(err => host.append(notice(errorText(err), 'error')));
    }

    function deleteTwin() {
      const item = twins.selected, { dialog, body } = showDialog('删除这个数字分身');
      body.append(el('p', '', `删除 ${item.name} 的模拟、来源记忆副本与分身对话。原始聊天和朋友圈索引继续保留。`),
        button('删除分身', 'trash', async () => { await rpc('delete_persona', { id: item.id }); twins.selected = null; dialog.close(); await loadTwins(); }, 'danger'));
    }

    async function sendTwin() {
      if (twins.busy || !twins.selected || !twins.draft.trim()) return;
      const pid = twins.selected.id, text = twins.draft;
      twins.busy = true; twins.error = ''; repaint();
      try { await rpc('chat_persona', { id: pid, text }); twins.selected = await rpc('get_persona', { id: pid }); twins.draft = ''; }
      catch (err) { twins.error = errorText(err); }
      finally { twins.busy = false; repaint(); requestAnimationFrame(() => { const timeline = $('.persona-timeline'); if (timeline) timeline.scrollTop = timeline.scrollHeight; }); }
    }

    function renderPersonas() {
      const root = el('section', 'page personas-page');
      root.append(heading('数字分身', '让熟悉的表达有来处，让每一次模拟有边界。', 'ECHOES OF CONNECTION', [button('创建分身', 'plus', createTwin, 'primary', twins.busy || !available()), button('刷新', 'refresh', () => loadTwins(), 'subtle', twins.busy)]));
      if (demo) { root.append(notice('数字分身需在桌面版导入指定联系人的记录后创建。此演示未制作真实人物分身。', 'info')); return root; }
      if (twins.error) root.append(notice(twins.error, 'error'));
      const layout = el('div', 'persona-layout'), catalogue = el('aside', 'panel persona-catalogue');
      catalogue.append(el('h2', '', '你的分身'));
      for (const item of twins.items) catalogue.append(button(`${item.name} · ${modes[item.mode]} · ${item.active_memories} 条记忆`, 'heart', () => selectTwin(item.id), `persona-choice ${item.id === twins.selected?.id ? 'selected' : ''}`, twins.busy));
      if (!twins.items.length) catalogue.append(el('p', 'field-hint', '选择稳定身份后，私聊、共同群聊和朋友圈会成为可核对的来源。'));
      if (twins.more) catalogue.append(button('加载更多分身', 'chevron', () => loadTwins(false), 'small', twins.busy));
      layout.append(catalogue);
      const conversation = el('section', 'panel persona-conversation'), item = twins.selected;
      if (!item) conversation.append(append(el('div', 'persona-welcome'), el('div', 'persona-orb'), el('h2', '', '从熟悉的话语开始'), el('p', '', '陪伴、纪念或沟通排练，建立一个由原始记录支撑的对话空间。'), button('选择联系人并创建', 'people', createTwin, 'primary', twins.busy)));
      else {
        conversation.append(append(el('div', 'persona-header'), append(el('div'), el('span', 'eyebrow', 'AI 模拟 · '+modes[item.mode]), el('h2', '', item.name)),
          append(el('div', 'persona-tools'), button('来源记忆', 'book', manageMemories, 'small', twins.busy), button('删除', 'trash', deleteTwin, 'small ghost', twins.busy))));
        conversation.append(el('p', 'field-hint', `${item.active_memories} 条启用记忆 · 依据私聊 ${item.coverage.sessions.filter(s => s.type === 'private').length} 个、共同群 ${item.coverage.include_groups ? item.coverage.sessions.filter(s => s.type === 'group').length : 0} 个。生成回复不会成为真实经历。`));
        const timeline = el('div', 'persona-timeline'); timeline.setAttribute('aria-live', 'polite');
        if (!item.turns.length) timeline.append(el('p', 'persona-first-word', '说一句想说的话，熟悉的表达会从有来源的记忆里回应。'));
        for (const turn of item.turns) {
          timeline.append(append(el('div', 'persona-bubble persona-user'), el('span', 'eyebrow', '你'), el('p', '', turn.user_text)));
          const reply = append(el('div', 'persona-bubble persona-reply'), el('span', 'eyebrow', `${item.name} · AI模拟`), el('p', '', turn.reply));
          reply.append(button('朗读', 'voice', () => rpc('speak_text', { text: turn.reply }), 'small ghost'));
          const sources = el('details', 'persona-sources'); sources.append(el('summary', '', `本次使用 ${turn.evidence.length} 条原始依据`));
          for (const record of turn.evidence) sources.append(evidenceView(record)); reply.append(sources); timeline.append(reply);
        }
        if (twins.busy) timeline.append(el('p', 'persona-thinking', '正在从来源记忆中寻找回应…'));
        conversation.append(timeline);
        const form = el('form', 'persona-composer'), input = el('textarea', 'input'); input.id = 'persona-message'; input.rows = 3; input.maxLength = 2000; input.placeholder = '在这个模拟空间里，说一句想说的话…'; input.setAttribute('aria-label', input.placeholder); input.value = twins.draft; input.disabled = twins.busy;
        input.addEventListener('input', event => { twins.draft = event.target.value; });
        input.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); sendTwin(); } });
        form.append(input, button(twins.busy ? '回应中…' : '发送到模拟空间', 'arrow', sendTwin, 'primary', twins.busy)); form.addEventListener('submit', event => { event.preventDefault(); sendTwin(); }); conversation.append(form);
        conversation.append(el('p', 'field-hint', '点击发送会把当前输入、检索到的原始记忆和最近模拟对话交给配置的聊天模型。语音使用本机系统 TTS。'));
      }
      layout.append(conversation); root.append(layout);
      if (!twins.loaded && !twins.busy && !twins.error) queueMicrotask(() => loadTwins());
      return root;
    }
    function importMoments(onDone) { contactPicker(async contact => { const result = await rpc('import_moments', { contact_id: contact.id }); if (!result.cancelled) { toast(`已索引 ${result.posts} 条朋友圈。`); if (onDone) await onDone(); } }); }
    return { renderInsights, renderPersonas, importMoments, contactPicker };
  }
});
