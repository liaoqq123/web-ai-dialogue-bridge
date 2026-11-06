(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const cache = {
    get(key) { try { return sessionStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch (_) { /* private browsing */ } },
    remove(key) { try { sessionStorage.removeItem(key); } catch (_) { /* private browsing */ } }
  };
  const tokenKey = 'dialogue-bridge-token';
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (fragment.has('token')) {
    cache.set(tokenKey, fragment.get('token'));
    history.replaceState(null, '', location.pathname + location.search);
  }
  let token = fragment.get('token') || cache.get(tokenKey) || '';
  let selected = cache.get('dialogue-bridge-selected') || '';
  let config = {}, tasks = [], current = null, questionRecord = null;
  let polling = false, initialized = false, changingTask = 0;
  let listSignature = '';
  const drafts = new Map();
  const fieldDrafts = new Map();
  const answeredGroups = new Set();
  const createRequest = { signature: null, key: null };
  const labels = { queued: '等待开始', running: '处理中', needs_input: '待回复', completed: '已完成' };
  const eventLabels = { pending: '等待投递', sending: '正在投递', delivered: '已送达', failed: '投递失败', unknown: '结果待核对', cancelled: '已取消' };
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = String(text);
    return el;
  }
  function key() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  }
  function notice(message) { $('notice').textContent = message || ''; $('notice').hidden = !message; }
  function showAuth(message) {
    $('auth-panel').hidden = false;
    $('workspace').hidden = true;
    if (message) notice(message);
  }
  async function api(path, body, { raw = false } = {}) {
    if (!token) throw new Error('请先填写访问凭证。');
    const headers = { Authorization: 'Bearer ' + token };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers,
        body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', credentials: 'omit', redirect: 'error' });
    } catch (_) { throw new Error('网络连接未完成。可重试本次操作，已提交的相同内容不会重复创建。'); }
    if (response.status === 401 || response.status === 403) {
      initialized = false;
      showAuth('访问凭证无效或已变更，请重新连接。');
    }
    if (!response.ok) {
      let result;
      try { result = await response.json(); } catch (_) { result = {}; }
      const error = new Error(typeof result.error === 'string' ? result.error : result.error?.message || result.message || '请求失败，请稍后重试。');
      error.status = response.status;
      error.code = result.code || result.error?.code;
      throw error;
    }
    return raw ? response : response.json();
  }
  function pendingCount(task) {
    return task.question_group?.status === 'pending' && !answeredGroups.has(task.id + ':' + task.question_group.revision) ? task.question_group.questions.length : 0;
  }
  function renderList() {
    const signature = JSON.stringify([selected, tasks.map(task => [task.id, task.title, task.status, pendingCount(task)])]);
    if (signature === listSignature) return;
    listSignature = signature;
    const list = $('task-list');
    const oldButtons = new Map(Array.from(list.children).map(button => [button.dataset.id, button]));
    for (const task of tasks) {
      let button = oldButtons.get(task.id);
      if (!button) {
        button = node('button', 'task-button'); button.type = 'button'; button.dataset.id = task.id;
        button.addEventListener('click', () => selectTask(task.id));
        button.append(node('span', 'task-title'), node('span', 'badge'));
      }
      const title = button.children[0], count = pendingCount(task);
      title.replaceChildren(node('span', '', task.title), node('small', '', labels[task.status] || task.status));
      button.children[1].textContent = count ? count + ' 待回复' : task.status === 'completed' ? '✓' : '';
      button.children[1].className = 'badge' + (count ? ' pending' : task.status === 'completed' ? ' completed' : '');
      button.children[1].hidden = !count && task.status !== 'completed';
      button.setAttribute('aria-current', String(task.id === selected));
      list.append(button); oldButtons.delete(task.id);
    }
    for (const button of oldButtons.values()) button.remove();
    if (!tasks.length) list.replaceChildren(node('p', 'muted', '还没有任务，上传后会显示在这里。'));
    $('task-count').textContent = String(tasks.length);
  }
  function preserveDraft(record, stale = false) {
    if (!record) return;
    const value = record.questions.map(question => ({ id: question.id, title: question.title, text: record.inputs.get(question.id).value }));
    fieldDrafts.set(record.taskId + ':' + record.revision, value);
    if (stale && value.some(answer => answer.text.trim())) {
      drafts.set(record.taskId, value.map(answer => answer.title + '\n' + answer.text).join('\n\n'));
    }
  }
  function renderDraft(taskId) {
    const host = $('draft-host'); if (!host) return;
    host.replaceChildren();
    const value = drafts.get(taskId); if (!value) return;
    const panel = node('section', 'draft');
    panel.append(node('h3', '', '已保留上一版回复草稿'), node('p', 'muted', '问题已更新，请核对当前问题。以下草稿可复制后继续填写。'));
    const text = node('textarea'); text.readOnly = true; text.value = value; text.setAttribute('aria-label', '上一版回复草稿');
    const remove = node('button', '', '清除草稿'); remove.type = 'button';
    remove.addEventListener('click', () => { drafts.delete(taskId); renderDraft(taskId); });
    panel.append(text, remove); host.append(panel);
  }
  function buildDetail(task) {
    const host = $('task-detail');
    host.replaceChildren();
    const header = node('div', 'detail-heading'), left = node('div');
    left.append(node('h2', '', task.title), node('div', 'task-id', task.id));
    const badge = node('span', 'badge'); badge.id = 'detail-status'; header.append(left, badge);
    const thread = node('div'); thread.id = 'thread-link';
    const dataSection = node('section', 'detail-section');
    dataSection.append(node('h3', '', '提交的数据'));
    const data = node('div', 'text-block'); data.id = 'detail-data'; dataSection.append(data);
    const fileSection = node('section', 'detail-section'); fileSection.id = 'file-section';
    fileSection.append(node('h3', '', '上传文件'));
    const files = node('div', 'files'); files.id = 'detail-files'; fileSection.append(files);
    const question = node('div'); question.id = 'question-host';
    const draft = node('div'); draft.id = 'draft-host';
    const result = node('section', 'detail-section'); result.id = 'result-section';
    result.append(node('h3', '', '处理结果'));
    const resultText = node('div', 'text-block detail-result'); resultText.id = 'detail-result'; result.append(resultText);
    const deliveries = node('section', 'detail-section');
    deliveries.append(node('h3', '', '对话回调'));
    const events = node('div', 'delivery-list'); events.id = 'detail-events'; deliveries.append(events);
    const historySection = node('details', 'history detail-section'); historySection.id = 'reply-history';
    historySection.append(node('summary', '', '回复记录'));
    const historyRows = node('div'); historyRows.id = 'reply-history-rows'; historySection.append(historyRows);
    host.append(header, thread, dataSection, fileSection, question, draft, result, deliveries, historySection);
  }
  function formatBytes(size) {
    if (size < 1024) return size + ' B';
    if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
    return (size / 1024 / 1024).toFixed(1) + ' MB';
  }
  async function downloadFile(taskId, file, button) {
    button.disabled = true;
    try {
      const response = await api('/api/tasks/' + encodeURIComponent(taskId) + '/files/' + encodeURIComponent(file.storage_name), undefined, { raw: true });
      const blob = await response.blob(), url = URL.createObjectURL(blob), link = node('a');
      link.href = url; link.download = file.name; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { notice(error.message); } finally { button.disabled = false; }
  }
  function renderFiles(task) {
    const host = $('detail-files'); host.replaceChildren();
    $('file-section').hidden = !task.files?.length;
    for (const file of task.files || []) {
      const row = node('div', 'file-row'), label = node('span', '', file.name);
      label.append(node('small', '', formatBytes(file.size)));
      const button = node('button', '', '下载'); button.type = 'button';
      button.addEventListener('click', () => downloadFile(task.id, file, button)); row.append(label, button); host.append(row);
    }
  }
  function renderThread(task) {
    const host = $('thread-link'); host.replaceChildren();
    const id = task.thread?.thread_id;
    if (!id) return;
    if (config.adapter === 'codex-desktop' && /^[A-Za-z0-9_-]{1,128}$/.test(id)) {
      const link = node('a', 'thread-link', '打开原 AI 对话'); link.href = 'codex://threads/' + encodeURIComponent(id); host.append(link);
    } else host.append(node('span', 'muted', '对话编号：' + id));
  }
  function renderEvents(task) {
    const host = $('detail-events'); host.replaceChildren();
    for (const event of task.events || []) {
      const row = node('div', 'delivery ' + event.status), description = node('div');
      description.append(node('span', 'label', (event.type === 'dialogue.start' ? '发起对话' : '网页回复') + ' · ' + (eventLabels[event.status] || event.status)));
      if (event.status === 'unknown') description.append(node('span', 'muted', '请求可能已经送达。请管理员核对原对话后处理，不会自动重发或新建对话。'));
      else if (event.error) description.append(node('span', 'muted', event.error));
      row.append(description);
      if (event.status === 'failed' && task.status !== 'completed') {
        const retry = node('button', '', '重试投递'); retry.type = 'button';
        retry.addEventListener('click', async () => {
          retry.disabled = true;
          try { const result = await api('/api/tasks/' + encodeURIComponent(task.id) + '/events/' + encodeURIComponent(event.id) + '/retry', {}); acceptTask(result.task); }
          catch (error) { notice(error.message); retry.disabled = false; }
        }); row.append(retry);
      }
      host.append(row);
    }
  }
  function renderHistory(task) {
    const host = $('reply-history-rows'); host.replaceChildren();
    const groups = [...(task.history || [])];
    if (task.question_group?.status === 'answered') groups.push(task.question_group);
    const seen = new Set();
    for (const group of groups) {
      const groupKey = group.revision;
      if (seen.has(groupKey) || !Array.isArray(group.answers)) continue;
      seen.add(groupKey);
      const titles = new Map((group.questions || []).map(question => [question.id, question.title]));
      for (const answer of group.answers) {
        const entry = node('div', 'history-entry');
        entry.append(node('strong', '', titles.get(answer.question_id) || answer.question_id), node('span', '', answer.text)); host.append(entry);
      }
    }
    $('reply-history').hidden = !host.children.length;
  }
  function renderQuestions(task) {
    const host = $('question-host'), group = task.question_group;
    const pending = group?.status === 'pending' && task.status !== 'completed' && !answeredGroups.has(task.id + ':' + group.revision);
    const same = pending && questionRecord?.taskId === task.id && questionRecord.revision === group.revision;
    if (same) return;
    if (questionRecord && pending && questionRecord.taskId === task.id && questionRecord.revision !== group.revision) preserveDraft(questionRecord, true);
    questionRecord = null; host.replaceChildren();
    if (!pending) { renderDraft(task.id); return; }
    const panel = node('section', 'question-panel'), form = node('form', 'question-form');
    panel.append(node('h3', '', '待补充信息'), node('p', 'question-help', '回复后会回调当前任务的原对话。这里的回复只补充需求，不代表批准执行其他操作。'));
    const record = { taskId: task.id, revision: group.revision, questions: group.questions, inputs: new Map(), sending: false, signature: null, requestKey: null };
    for (const question of group.questions) {
      const field = node('div'), id = 'answer-' + task.id + '-' + group.revision + '-' + question.id;
      const label = node('label', '', question.title); label.htmlFor = id;
      const input = node('textarea'); input.id = id; input.rows = 3; input.maxLength = 12000; input.required = true;
      const saved = fieldDrafts.get(task.id + ':' + group.revision)?.find(answer => answer.id === question.id);
      if (saved) input.value = saved.text;
      record.inputs.set(question.id, input); field.append(label);
      if (question.options?.length) {
        const options = node('div', 'question-options');
        for (const value of question.options) {
          const button = node('button', '', value); button.type = 'button';
          button.addEventListener('click', () => { input.value = value; input.focus(); }); options.append(button);
        } field.append(options);
      }
      field.append(input); form.append(field);
    }
    const error = node('p', 'error'); error.hidden = true; error.setAttribute('role', 'alert');
    const actions = node('div', 'question-actions'), submit = node('button', 'primary', '回复并继续'); submit.type = 'submit';
    actions.append(submit, node('span', 'question-help', '请填写本组全部问题。')); form.append(error, actions);
    record.error = error; record.submit = submit; record.panel = panel;
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (record.sending) return;
      const answers = record.questions.map(question => ({ question_id: question.id, text: record.inputs.get(question.id).value.trim() }));
      if (answers.some(answer => !answer.text)) { error.textContent = '请填写本组全部问题。'; error.hidden = false; return; }
      if (answers.reduce((size, answer) => size + answer.text.length, 0) > 24000) { error.textContent = '本组回复过长，请缩短至 24000 字以内。'; error.hidden = false; return; }
      const signature = JSON.stringify(answers);
      if (record.signature !== signature) { record.signature = signature; record.requestKey = key(); }
      record.sending = true; submit.disabled = true; error.hidden = true;
      try {
        const result = await api('/api/tasks/' + encodeURIComponent(record.taskId) + '/reply', {
          revision: record.revision, answers, request_key: record.requestKey
        });
        answeredGroups.add(record.taskId + ':' + record.revision);
        fieldDrafts.delete(record.taskId + ':' + record.revision);
        drafts.delete(record.taskId);
        if (selected === record.taskId) acceptTask(result.task);
        notice('回复已保存，正在回调原对话。');
      } catch (failure) {
        error.textContent = failure.status === 409 ? '问题版本已变更或已有回复，请核对最新问题。原草稿已保留。' : failure.message;
        error.hidden = false;
        if (failure.status === 409) { preserveDraft(record, true); notice(error.textContent); await refreshDetail(); renderDraft(record.taskId); }
      } finally { record.sending = false; submit.disabled = false; }
    });
    panel.append(form); host.append(panel); questionRecord = record; renderDraft(task.id);
  }
  function acceptTask(task) {
    if (!task || task.id !== selected) return;
    // A poll started before a reply must not bring its already-answered form back.
    if (current?.id === task.id && task.updated_at && current.updated_at && task.updated_at < current.updated_at) return;
    if (current?.id === task.id && JSON.stringify(current) === JSON.stringify(task)) return;
    const first = current?.id !== task.id;
    if (first) { preserveDraft(questionRecord); questionRecord = null; buildDetail(task); }
    current = task;
    const index = tasks.findIndex(item => item.id === task.id);
    if (index >= 0) tasks[index] = task; else tasks.unshift(task);
    const badge = $('detail-status'); badge.textContent = labels[task.status] || task.status;
    badge.className = 'badge' + (task.status === 'completed' ? ' completed' : pendingCount(task) ? ' pending' : '');
    $('detail-data').textContent = typeof task.data?.message === 'string' && Object.keys(task.data).length === 1 ? task.data.message : JSON.stringify(task.data, null, 2);
    renderThread(task); renderFiles(task); renderQuestions(task); renderEvents(task); renderHistory(task);
    $('result-section').hidden = !task.result;
    if (task.result) $('detail-result').textContent = typeof task.result.summary === 'string' && Object.keys(task.result).length === 1 ? task.result.summary : JSON.stringify(task.result, null, 2);
    renderList();
  }
  async function selectTask(taskId) {
    if (selected !== taskId) { preserveDraft(questionRecord); current = null; questionRecord = null; }
    selected = taskId; cache.set('dialogue-bridge-selected', taskId); renderList();
    await refreshDetail();
  }
  async function refreshDetail() {
    if (!selected) return;
    const request = ++changingTask, id = selected;
    try {
      const result = await api('/api/tasks/' + encodeURIComponent(id));
      if (request === changingTask && id === selected) acceptTask(result.task);
    } catch (error) { if (request === changingTask) notice(error.message); }
  }
  async function refresh() {
    if (polling || !initialized || document.hidden) return;
    polling = true;
    try {
      const result = await api('/api/tasks'); tasks = result.tasks; renderList();
      if (!selected && tasks.length) await selectTask(tasks[0].id);
      else if (selected) await refreshDetail();
    } catch (error) { notice(error.message); } finally { polling = false; }
  }
  function encodeFile(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve({ name: file.name, content_base64: String(reader.result).split(',')[1] });
      reader.onerror = () => reject(new Error('读取文件失败：' + file.name));
      reader.readAsDataURL(file);
    });
  }
  $('upload-form').addEventListener('submit', async event => {
    event.preventDefault(); const submit = $('upload-submit'), error = $('upload-error');
    if (submit.disabled) return;
    submit.disabled = true; error.hidden = true;
    try {
      const files = Array.from($('task-files').files), total = files.reduce((size, file) => size + file.size, 0);
      if (files.some(file => file.size > config.max_file_bytes)) throw new Error('单个文件超过允许大小。');
      if (total > config.max_total_bytes) throw new Error('文件总大小超过允许上限。');
      if (config.max_files && files.length > config.max_files) throw new Error('文件数量超过允许上限。');
      const payload = { title: $('task-title').value.trim(), data: { message: $('task-message').value.trim() }, files: await Promise.all(files.map(encodeFile)) };
      if (!payload.title) throw new Error('请填写任务名称。');
      const signature = JSON.stringify(payload);
      if (createRequest.signature !== signature) { createRequest.signature = signature; createRequest.key = key(); }
      const result = await api('/api/tasks', { ...payload, request_key: createRequest.key });
      $('upload-form').reset(); createRequest.signature = null; createRequest.key = null;
      selected = result.task.id; cache.set('dialogue-bridge-selected', selected); acceptTask(result.task);
      notice('数据已上传，正在发起对话。');
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { submit.disabled = false; }
  });
  async function connect() {
    if (!token) { showAuth(); return; }
    try {
      config = await api('/api/config'); initialized = true;
      $('auth-panel').hidden = true; $('workspace').hidden = false; $('demo-notice').hidden = config.adapter !== 'demo'; notice('');
      if (config.max_file_bytes && config.max_total_bytes) $('upload-limits').textContent = '单文件最多 ' + formatBytes(config.max_file_bytes) + '，合计最多 ' + formatBytes(config.max_total_bytes) + '。文件保存在服务所在机器。';
      await refresh();
    } catch (error) { showAuth(error.message); }
  }
  $('auth-form').addEventListener('submit', event => {
    event.preventDefault(); token = $('access-token').value.trim(); cache.set(tokenKey, token); $('access-token').value = ''; connect();
  });
  $('change-token').addEventListener('click', () => {
    initialized = false; token = ''; cache.remove(tokenKey); showAuth(); $('access-token').focus();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(refresh, 3000);
  connect();
})();
