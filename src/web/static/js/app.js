// Component instances
const contactList = new ContactList(document.getElementById('contact-list'));
const messageList = new MessageBubble(document.getElementById('message-list'));
const pagination = new Pagination(document.getElementById('pagination-bar'));
const chatHeader = new ChatHeader(document.getElementById('chat-header'));
const filterBar = new FilterBar(document.getElementById('filter-bar'));
const groupInfo = new GroupInfo(document.getElementById('group-info-content'));

// ===========================================================================
// 深链定位（Task 18 / `.docs/agent-context/08-risks/known-issues.md` #29）
//
// 搜索结果条目的链接形如
//     /chat?open=<chat_id>&focus=<local_id>&ft=<create_time>
// 点进去要**定位到那条消息**，而不是只选中会话。
//
// 分工：**页号由后端算**（`/api/messages` 的 `focused.page`，见
// `engine/services/message/__init__.py::query_messages` 的 focus 支持），前端只做
// 四件事：① 请求那一页；② 把目标气泡滚动到可见区域；③ 加高亮 class；
// ④ 没定位到时给**明确提示**（绝不静默地落在会话里）。
//
// 为什么定位键必须同时带 `create_time`：`local_id` 在不同分片之间会重号
// （`msg_meta` 的主键必须含 `create_time`，见 ADR-0012）。只用 `local_id` 会高亮错行。
// ===========================================================================
const FOCUS_HIGHLIGHT_CLASS = 'msg-focused';   // 样式在 `css/app.css`
const FOCUS_NOTICE_ID = 'msg-focus-notice';    // 提示元素（`index.html` 里不存在，由这里建）
// 未找到时的兜底文案。后端一般会给 `focused.message`，这里只是**兜底**（老服务端/字段缺失）。
const FOCUS_NOT_FOUND_TEXT = '未能定位到该消息：它可能已被删除，或不属于当前账号';
// URL 里的定位参数自己不合法（非整数 / 只给一个）时的文案。
// 前端**不猜**：不把半截参数发给后端（那会拿到 400），也不静默地落回第 1 页。
const FOCUS_BAD_PARAM_TEXT = '定位参数无效（需要 focus=<local_id>&ft=<create_time> 两个整数），未执行定位';

/** 严格非负整数（与后端 `/api/messages` 的校验同口径）。非整数 → null。 */
function _focusIntOrNull(raw) {
  if (raw === null || raw === undefined) return null;
  const s = String(raw).trim();
  if (!/^[0-9]+$/.test(s)) return null;
  const n = parseInt(s, 10);
  return isFinite(n) ? n : null;
}

/**
 * 解析 `?focus=` / `?ft=`（默认取 `window.location.search`）。
 *
 * @returns {{focus: ({localId:number,createTime:number}|null), error: (string|null)}}
 *   `focus` 有值 → 本次加载要定位；`error` 有值 → 参数不合法，要用明确文案提示。
 */
function parseFocusParams(search) {
  const raw = (search === undefined) ? window.location.search : search;
  let qp;
  try { qp = new URLSearchParams(raw || ''); } catch (e) { return { focus: null, error: null }; }
  const rawId = qp.get('focus');
  const rawTs = qp.get('ft');
  if (rawId === null && rawTs === null) return { focus: null, error: null };
  if (rawId === null || rawTs === null) return { focus: null, error: FOCUS_BAD_PARAM_TEXT };
  const localId = _focusIntOrNull(rawId);
  const createTime = _focusIntOrNull(rawTs);
  if (localId === null || createTime === null) return { focus: null, error: FOCUS_BAD_PARAM_TEXT };
  return { focus: { localId: localId, createTime: createTime }, error: null };
}

/** 定位提示元素（`#msg-focus-notice`）：存在于列表**上方**，与列表一起滚动不动。 */
function _focusNoticeEl() {
  let el = document.getElementById(FOCUS_NOTICE_ID);
  if (el) return el;
  const listEl = document.getElementById('message-list');
  if (!listEl || !document.createElement) return null;
  el = document.createElement('div');
  el.id = FOCUS_NOTICE_ID;
  el.className = 'msg-focus-notice';
  if (listEl.parentNode && listEl.parentNode.insertBefore) {
    listEl.parentNode.insertBefore(el, listEl);
  } else {
    return null;
  }
  return el;
}

function showFocusNotice(text) {
  const el = _focusNoticeEl();
  if (!el) return;
  el.textContent = text;
  el.style.display = 'block';
}

function clearFocusNotice() {
  const el = document.getElementById(FOCUS_NOTICE_ID);
  if (!el) return;
  el.textContent = '';
  el.style.display = 'none';
}

/**
 * 给目标气泡加高亮 class，并返回那一行（没找到 → null）。
 *
 * 为什么按**位置**对应而不是按 `create_time` 查 DOM：气泡行上没有时间戳属性
 * （`message-bubble.js` 不归本任务改）。DOM 里的 `.msg-row` 与 `messages` 数组一一对应，
 * 所以取"数组里同 id 消息中的第几个"对应的那一行 —— 同页出现重号 `local_id` 时
 * 也不会高亮错行。
 */
function applyFocusHighlight(messages, focus) {
  const listEl = document.getElementById('message-list');
  if (!listEl || !focus) return null;
  const msgs = messages || [];
  let idx = -1;
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i];
    if (m && m.id === focus.localId && m.create_time === focus.createTime) { idx = i; break; }
  }
  if (idx < 0) return null;
  let nth = 0;
  for (let j = 0; j < idx; j++) {
    if (msgs[j] && msgs[j].id === focus.localId) nth++;
  }
  const rows = [];
  const kids = listEl.children || [];
  for (let k = 0; k < kids.length; k++) {
    const el = kids[k];
    if (el && el.classList && typeof el.getAttribute === 'function'
        && el.classList.contains('msg-row')
        && String(el.getAttribute('data-msg-id')) === String(focus.localId)) {
      rows.push(el);
    }
  }
  const row = rows[nth] || null;
  if (!row) return null;
  row.classList.add(FOCUS_HIGHLIGHT_CLASS);
  return row;
}

/** 把目标行滚到可见区域（居中）。老浏览器没有 `scrollIntoView` 时静默跳过。 */
function scrollFocusedRowIntoView(row) {
  if (row && typeof row.scrollIntoView === 'function') {
    row.scrollIntoView({ block: 'center' });
  }
}

async function initApp() {
  // Mount all components (event delegation survives re-renders)
  contactList.mount();
  messageList.mount();
  pagination.mount();
  chatHeader.mount();
  filterBar.mount();
  groupInfo.mount();

  // Initialize filter-bar DOM (replaces static HTML with component elements)
  filterBar.render({ filters: Store.data.filters });

  // Wire Store events
  Store.on('contact-selected', (chatId) => selectContact(chatId));
  Store.on('contact-search', (q) => loadContacts(q));
  Store.on('filter-changed', () => { Store.data.pagination.page = 1; loadMessages(); });
  Store.on('message-expanded', handleMessageExpanded);
  Store.on('group-info-toggle', () => openGroupInfo());
  Store.on('pagination-action', (action) => {
    const pg = Store.data.pagination;
    const map = { first: 1, prev: pg.page - 1, next: pg.page + 1, last: pg.total_pages };
    const page = map[action];
    if (page >= 1 && page <= (pg.total_pages || 0)) { Store.data.pagination.page = page; loadMessages(); }
  });
  Store.on('pagination-go', (page) => {
    if (page >= 1 && page <= (Store.data.pagination.total_pages || 0)) { Store.data.pagination.page = page; loadMessages(); }
  });
  Store.on('pagination-jump-date', (val) => {
    Store.data.filters.dateStart = val;
    Store.data.filters.dateEnd = val;
    Store.data.pagination.page = 1;
    const ds = document.querySelector('#filter-bar .filter-date-start');
    const de = document.querySelector('#filter-bar .filter-date-end');
    if (ds) ds.value = val;
    if (de) de.value = val;
    loadMessages();
  });

  // Show loading and fetch contacts
  contactList.render({ contacts: [], loading: true });
  await loadContacts();

  // 深链定位参数（`?focus=<local_id>&ft=<create_time>`）：只消费**一次**，
  // 之后的手动翻页/改筛选不再重新定位（见 loadMessages）。
  const focusParsed = parseFocusParams(window.location.search);
  Store.data.focus = focusParsed.focus;
  Store.data.focusError = focusParsed.error;

  // Auto-select contact from URL parameter (?contact=NAME)
  const qp = new URLSearchParams(window.location.search);
  const contactParam = qp.get('contact');
  if (contactParam) {
    const target = _findContactByName(contactParam);
    if (target) {
      await selectContact(target.id);
    }
  }
  // Auto-select contact from URL parameter (?open=wxid) — used by contacts page
  const openParam = qp.get('open');
  if (openParam && !contactParam) {
    const target = Store.data.contacts.find(c => c.id === openParam);
    if (target) {
      await selectContact(target.id);
    }
  }
}

function _findContactByName(name) {
  if (!name || !Store.data.contacts.length) return null;
  const q = name.toLowerCase();
  // Exact match first
  let c = Store.data.contacts.find(c => c.name === name);
  if (c) return c;
  // Prefix match (e.g., "张" matches "张三")
  c = Store.data.contacts.find(c => c.name.startsWith(name));
  if (c) return c;
  // Substring match
  c = Store.data.contacts.find(c => c.name.includes(name));
  if (c) return c;
  // Case-insensitive
  return Store.data.contacts.find(c => c.name.toLowerCase().includes(q)) || null;
}

// issue #27：会话很多时（实测 2915 个、471KB）不再一次性拉全量，
// 首次只取一页，滚动到底再追加下一页；总数由后端 total 给出。
const CONTACTS_PAGE_SIZE = 200;

function _renderContacts(loading, q) {
  contactList.render({
    contacts: Store.data.contacts || [],
    activeId: Store.data.activeChat ? Store.data.activeChat.id : null,
    loading: !!loading,
    searchQuery: q || ''
  });
}

function _ensureContactsScrollHook() {
  const el = document.getElementById('contact-list');
  if (!el || el.__pagingHook) return;
  el.__pagingHook = true;
  el.addEventListener('scroll', () => {
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 150) loadMoreContacts();
  });
}

async function loadContacts(q) {
  Store.data.contactsLoading = true;
  Store.data.contactsQuery = q || '';
  Store.data.contactsOffset = 0;
  Store.data.contactsTotal = 0;
  contactList.render({ contacts: [], loading: true, searchQuery: q || '' });
  try {
    const data = await api.contacts(q || '', CONTACTS_PAGE_SIZE, 0);
    Store.data.contacts = data.contacts || [];
    Store.data.contactsTotal = data.total || Store.data.contacts.length;
    Store.data.contactsOffset = Store.data.contacts.length;
  } catch (e) { if (e.name !== 'AbortError') console.error(e); }
  finally {
    Store.data.contactsLoading = false;
    _renderContacts(false, q);
    _ensureContactsScrollHook();
  }
}

async function loadMoreContacts() {
  if (Store.data.contactsLoading) return;
  const q = Store.data.contactsQuery || '';
  const loaded = (Store.data.contacts || []).length;
  const total = Store.data.contactsTotal || 0;
  if (!total || loaded >= total) return;          // 已取完
  Store.data.contactsLoading = true;
  try {
    const data = await api.contacts(q, CONTACTS_PAGE_SIZE, loaded);
    const more = data.contacts || [];
    if (more.length) {
      Store.data.contacts = (Store.data.contacts || []).concat(more);
      Store.data.contactsOffset = Store.data.contacts.length;
      Store.data.contactsTotal = data.total || total;
      _renderContacts(false, q);
    }
  } catch (e) { if (e.name !== 'AbortError') console.error(e); }
  finally { Store.data.contactsLoading = false; }
}

async function selectContact(chatId) {
  cancelPending();
  Store.data.activeChat = Store.data.contacts.find(c => c.id === chatId);
  Store.data.pagination.page = 1;
  Store.data.expandedMsg = null;
  if (Store.data.activeChat) {
    chatHeader.render({
      name: Store.data.activeChat.name,
      id: Store.data.activeChat.id,
      avatar_url: Store.data.activeChat.avatar_url,
      type: Store.data.activeChat.type,
      stats: ''
    });
  } else {
    chatHeader.render(null);
  }
  filterBar.resetFilters();
  closeGroupInfo();
  contactList.render({ contacts: Store.data.contacts, activeId: chatId, loading: false });
  showChatUI(true);
  showLoading();
  try {
    const s = await api.chatStats(chatId);
    const ds = s.date_range.start || '?', de = s.date_range.end || '?';
    chatHeader.updateStats(`${s.total_messages.toLocaleString()} 条消息 · ${ds} ~ ${de}`);
    filterBar.populateSenderFilter(s.sender_distribution);
  } catch (e) {
    if (e.name !== 'AbortError') {
      console.error(e);
      chatHeader.updateStats('统计加载失败');
      const se = document.querySelector('#filter-bar .filter-sender');
      if (se) se.innerHTML = '<option value="">全部发送者</option>';
    }
  }
  await loadMessages();
}

async function loadMessages() {
  if (!Store.data.activeChat) return;
  Store.data.loading = true;
  showLoading();
  // 深链定位**只消费一次**：切页/切会话/改筛选后的请求不再带 focus，
  // 高亮与提示也就不会粘住（每次 render 都会重建列表 DOM）。
  const focusReq = Store.data.focus;
  const focusError = Store.data.focusError;
  Store.data.focus = null;
  Store.data.focusError = null;
  try {
    const params = {
      chat_id: Store.data.activeChat.id,
      page: Store.data.pagination.page,
      per_page: Store.data.pagination.perPage,
      start_date: Store.data.filters.dateStart || undefined,
      end_date: Store.data.filters.dateEnd || undefined,
      type: Store.data.filters.msgTypes || undefined,
      sender: Store.data.filters.sender || undefined,
      keyword: Store.data.filters.keyword || undefined,
    };
    if (focusReq) {
      // 后端据此返回**包含该消息的那一页**并忽略 page
      params.focus_local_id = focusReq.localId;
      params.focus_create_time = focusReq.createTime;
    }
    // `/api/messages` 的 URL 组装**只有 `api.js::messages()` 一处**（Task 19 收掉了
    // Task 18 在这里复制的那份：当时 `api.js` 的参数白名单里没有 `focus_*`，
    // 只能绕开它自己拼 URL）。取消语义（`cancelPending()`）也照旧由它负责。
    const data = await api.messages(params);
    if (data.error) throw new Error(data.error);
    Store.data.messages = data.messages;
    Store.data.pagination = data.pagination;
    Store.data.expandedMsg = null;
    messageList.render({ messages: Store.data.messages, expandedMsg: Store.data.expandedMsg });
    pagination.render(Store.data.pagination);
    clearFocusNotice();
    let focusedRow = null;
    if (focusReq && data.focused && data.focused.found) {
      focusedRow = applyFocusHighlight(Store.data.messages, focusReq);
    }
    if (focusedRow) {
      scrollFocusedRowIntoView(focusedRow);
    } else {
      document.getElementById('message-list').scrollTop = 0;
    }
    // 「没定位到」的任何一种形态都必须说清楚：参数非法 / 消息不在（后端 reason）
    // / 后端说 found 但页里没有那一行（契约不一致）。绝不静默。
    if (focusReq && !focusedRow) {
      showFocusNotice((data.focused && data.focused.message) || FOCUS_NOT_FOUND_TEXT);
    } else if (focusError) {
      showFocusNotice(focusError);
    }
    const rc = document.querySelector('#filter-bar .filter-result-count');
    if (rc) rc.textContent = `找到 ${data.pagination.total.toLocaleString()} 条`;
  } catch (e) {
    if (e.name !== 'AbortError') {
      clearFocusNotice();
      showError('加载消息失败: ' + e.message);
      const rc = document.querySelector('#filter-bar .filter-result-count');
      if (rc) rc.textContent = '';
      pagination.render({ page: 0, per_page: 0, total: 0, total_pages: 0 });
    }
  } finally { Store.data.loading = false; }
}

async function handleMessageExpanded(msgId) {
  if (Store.data.expandedMsg === msgId) { Store.data.expandedMsg = null; }
  else {
    const msg = Store.data.messages.find(m => m.id === msgId);
    if (msg && msg.msg_type !== 1) {
      const needDetail = !msg.xml_parsed || Object.keys(msg.xml_parsed).length === 0;
      const needMedia = !msg.media_info && (msg.msg_type === 3 || msg.msg_type === 43 || msg.msg_type === 6 || msg.msg_type === 49);
      if (needDetail || needMedia) {
        try {
          const detail = await api.messageDetail(msgId, Store.data.activeChat ? Store.data.activeChat.id : '');
          if (detail) {
            if (needDetail) { msg.xml_parsed = detail.xml_parsed; msg.content = detail.content; }
            if (needMedia && detail.media_info) { msg.media_info = detail.media_info; }
          }
        } catch (e) { /* keep existing */ }
      }
    }
    Store.data.expandedMsg = msgId;
  }
  const listEl = document.getElementById('message-list');
  const st = listEl.scrollTop;
  messageList.render({ messages: Store.data.messages, expandedMsg: Store.data.expandedMsg });
  listEl.scrollTop = st;
}

// Global functions for inline onclick handlers in HTML (backdrop close, gi-btn)
async function openGroupInfo() {
  if (!Store.data.activeChat || Store.data.activeChat.type !== 'group') return;
  const panel = document.getElementById('group-info-panel');
  const backdrop = document.getElementById('group-info-backdrop');
  if (!panel || !backdrop) return;
  panel.style.display = 'block';
  backdrop.style.display = 'block';
  groupInfo.el.innerHTML = '<div class="loading"><div class="loading-icon">⏳</div>加载群信息...</div>';
  try {
    const data = await api.groupInfo(Store.data.activeChat.id);
    data.display_name = Store.data.activeChat.name;
    groupInfo.render(data);
  } catch (e) {
    if (e.name !== 'AbortError' && groupInfo.el) {
      groupInfo.el.innerHTML = `<div class="error-msg">加载失败: ${escapeHtml(e.message)}</div>`;
    }
  }
}

function closeGroupInfo() {
  const panel = document.getElementById('group-info-panel');
  const backdrop = document.getElementById('group-info-backdrop');
  if (!panel || !backdrop) return;
  panel.style.display = 'none';
  backdrop.style.display = 'none';
}

document.addEventListener('DOMContentLoaded', initApp);
