'use strict';

window.ZhixianWorkbench = Object.freeze({
  mount(api) {
    const { $, el, append, button, heading, notice, empty, rpc, sync, getState, getPage, go, render, toast, errorText, showDialog, field, available, demo, openCatalog } = api;
    const actions = { items: [], state: 'open', loaded: false, busy: false, error: '', cursor: null, more: false, serial: 0 };
    let searchTimer, searchSerial = 0;

    async function loadActions(reset = true) {
      if (actions.busy) return;
      const serial = ++actions.serial; actions.busy = true; actions.error = '';
      if (getPage() === 'actions') render();
      try {
        const result = await rpc('list_actions', { state: actions.state, cursor: reset ? null : actions.cursor, limit: 24 });
        if (serial !== actions.serial) return;
        actions.items = reset ? result.items || [] : [...actions.items, ...result.items || []];
        actions.more = Boolean(result.has_more); actions.cursor = result.next_cursor; actions.loaded = true;
      } catch (err) { actions.error = errorText(err); }
      finally { actions.busy = false; if (getPage() === 'actions') render(); }
    }

    function actionDialog(session = getState().current_session, proposal = {}) {
      if (!session) return openCatalog(async item => { await rpc('select_session', { session_id: item.id }); await sync(); actionDialog(getState().current_session); });
      const { dialog, body } = showDialog('确认一项客户跟进'), form = el('form');
      const evidence = (session.messages || []).filter(m => m.id).slice(-12);
      append(form, el('p', 'field-hint', `会话：${session.title}。行动由你确认保存，模型不会自动建立承诺或发送消息。`),
        field('行动标题', 'action-title', proposal.title || '', { placeholder: '例如：核对客户报价后回复' }),
        field('补充说明', 'action-detail', proposal.detail || '', { rows: 3, optional: true }),
        field('跟进时间', 'action-due', '', { type: 'datetime-local', optional: true, hint: '按本机时区设置；留空表示时间待定。' }),
        field('绑定消息证据', 'action-evidence', '', { options: [['', '手动行动，无消息证据'], ...evidence.map(m => [m.id, `${m.side === 'me' ? '我' : '对方'}：${String(m.text || '').slice(0,70)}`])] }));
      if (proposal.evidence?.length) { form.append(el('h3', '', '模型提案的消息依据')); for (const e of proposal.evidence) form.append(el('blockquote', 'action-evidence', e.quote)); if (proposal.deadline_quote) form.append(el('p', 'field-hint', `原文时间：${proposal.deadline_quote}；请自行核对并设置具体日期。`)); }
      const save = button('确认保存', 'check', async () => {
        const due = $('#action-due').value;
        await rpc('create_action', { session_id: session.id, action: { title: $('#action-title').value, detail: $('#action-detail').value,
          due_at: due ? new Date(due).toISOString() : null, message_ids: proposal.message_ids?.length ? proposal.message_ids : $('#action-evidence').value ? [$('#action-evidence').value] : [] } });
        dialog.close(); actions.loaded = false; await loadActions(); toast('跟进已保存到本机行动中心');
      }, 'primary', !available());
      form.append(append(el('div', 'dialog-footer'), button('取消', null, () => dialog.close()), save));
      form.addEventListener('submit', event => { event.preventDefault(); save.click(); }); body.append(form);
    }

    function renderActions() {
      const root = el('section', 'page actions-page');
      root.append(heading('客户行动中心', '把值得跟进的对话变成有时间、有证据、可关闭的行动。', 'MISSION CONTROL',
        [button('扫描承诺', 'spark', scanFollowups, 'subtle', !available()), button('新增跟进', 'plus', () => actionDialog(), 'primary', !available()), button('刷新', 'refresh', () => loadActions(), 'subtle', actions.busy)]));
      const hero = el('section', 'action-hero panel');
      hero.append(el('span', 'eyebrow', '你的下一步'), el('h2', '', '承诺有来处，跟进有闭环。'), el('p', '', '每项行动保留会话和原文依据；你确认内容与时间，再决定怎样回应。'), el('span', 'action-orbit', '弦'));
      root.append(hero);
      const filters = el('div', 'action-filters');
      for (const [value, label] of [['open','待跟进'],['done','已完成'],['dismissed','已取消'],['all','全部']]) filters.append(button(label, null, () => {
        if (actions.busy) return; actions.state = value; actions.loaded = false; return loadActions();
      }, `small ${actions.state === value ? 'primary' : 'ghost'}`, actions.busy));
      root.append(filters);
      if (actions.error) root.append(notice(actions.error, 'error'));
      if (demo) root.append(notice('合成演示任务，修改不会持久保存。', 'info'));
      const grid = el('div', 'action-grid');
      if (!actions.items.length && !actions.busy) grid.append(empty('当前没有跟进任务', '从客户会话中绑定消息，确认下一步。完成后保留记录，方便复盘。', 'check'));
      for (const item of actions.items) {
        const card = el('article', `panel action-card state-${item.state}`), date = item.due_at ? new Date(item.due_at) : null;
        append(card, el('span', 'eyebrow', item.session_title || '客户会话'), el('h3', '', item.title), el('p', '', item.detail || '具体步骤待核对'),
          el('div', `action-date${date && date < new Date() && item.state === 'open' ? ' overdue' : ''}`, date ? `跟进 · ${date.toLocaleString()}` : '时间待定'));
        for (const evidence of item.evidence || []) card.append(el('blockquote', 'action-evidence', `${evidence.side === 'me' ? '我' : '对方'}：${evidence.text}`));
        const controls = el('div', 'action-controls');
        controls.append(button('查看会话', 'chat', async () => { await rpc('select_session', { session_id: item.session_id }); await sync(); go('workspace'); }, 'small ghost'));
        for (const [state, label] of item.state === 'open' ? [['done','完成'],['dismissed','取消']] : [['open','重新打开']]) controls.append(button(label, state === 'done' ? 'check' : null, async () => {
          await rpc('update_action', { id: item.id, state, revision: item.revision }); await loadActions(); toast(`行动已${state === 'done' ? '完成' : state === 'dismissed' ? '取消' : '重新打开'}`);
        }, `small ${state === 'done' ? 'primary' : 'ghost'}`));
        card.append(controls); grid.append(card);
      }
      root.append(grid);
      if (actions.busy) root.append(el('p', 'field-hint', '正在读取本机行动…'));
      if (actions.more) root.append(button('加载更多行动', 'chevron', () => loadActions(false), '', actions.busy));
      if (!actions.loaded && !actions.busy && !actions.error) queueMicrotask(() => loadActions());
      return root;
    }

    async function scanFollowups() {
      let session = getState().current_session;
      if (!session) return openCatalog(async item => { await rpc('select_session', { session_id: item.id }); await sync(); return scanFollowups(); });
      const result = await rpc('extract_followups', { session_id: session.id });
      const { dialog, body } = showDialog('核对客户承诺与请求');
      body.append(notice(result.warning || '逐项核对后保存，不会自动创建任务。', 'info'));
      if (!result.proposals?.length) body.append(empty('没有可核对的未完成行动', '当前已加载消息中没有取得明确证据；可以加载更多历史或手动建立跟进。', 'check'));
      for (const proposal of result.proposals || []) {
        const card = el('article', 'panel action-card'); card.append(el('h3', '', proposal.title), el('p', '', proposal.detail));
        for (const evidence of proposal.evidence || []) card.append(el('blockquote', 'action-evidence', evidence.quote));
        card.append(button('核对并建立跟进', 'check', () => { dialog.close(); actionDialog(session, proposal); }, 'small primary')); body.append(card);
      }
    }

    function openSearch() {
      const { dialog, body } = showDialog('检索导入的完整聊天记录');
      const input = el('input', 'input'); input.type = 'search'; input.placeholder = '正文关键词，至少 3 个字符'; input.setAttribute('aria-label', input.placeholder);
      const results = el('div', 'message-search-results');
      body.append(input, el('p', 'field-hint', '本机索引检索，先显示一页。范围为主动导入的归档；图片和语音显示类型，已识别的内容仍需核对。'), results);
      let items = [], cursor = null, busy = false;
      async function search(reset = true) {
        const serial = ++searchSerial, query = input.value.trim();
        if (query.length < 3) { results.replaceChildren(el('p', 'field-hint', '联系人名称可在全局会话中搜索，正文至少输入 3 个字符。')); return; }
        busy = true; if (reset) results.replaceChildren(el('p', 'field-hint', '正在检索索引…'));
        try {
          const page = await rpc('search_messages', { query, cursor: reset ? null : cursor, limit: 20 });
          if (serial !== searchSerial || !dialog.open) return;
          items = reset ? page.items || [] : [...items, ...page.items || []]; cursor = page.next_cursor;
          results.replaceChildren();
          if (!items.length) results.append(empty('没有匹配记录', '换一个关键词，或先导入聊天归档。', 'search'));
          for (const item of items) results.append(button(`${item.session_title} · ${item.kind === 'text' ? '' : '['+item.kind+'] '}${String(item.text || '').slice(0,160)}`, 'chat', async () => {
            await rpc('select_search_result', { session_id: item.session_id, message_id: item.id }); await sync(); dialog.close(); go('workspace');
          }, 'search-hit'));
          if (page.has_more) results.append(button('加载更多匹配', 'chevron', () => search(false), 'small'));
        } catch (err) { if (serial === searchSerial) results.replaceChildren(notice(errorText(err), 'error')); }
        finally { busy = false; }
      }
      input.addEventListener('input', () => { searchSerial++; clearTimeout(searchTimer); searchTimer = setTimeout(() => search(), 250); });
      dialog.addEventListener('close', () => { searchSerial++; clearTimeout(searchTimer); }, { once: true }); input.focus();
    }
    return { renderActions, actionDialog, openSearch };
  }
});
