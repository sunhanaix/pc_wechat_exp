let currentController = null;
let pendingRequests = 0;

function cancelPending() {
  if (currentController) { currentController.abort(); }
  currentController = new AbortController();
  return currentController.signal;
}

async function fetchJSON(url, signal) {
  pendingRequests++;
  try {
    const resp = await fetch(url, { signal });
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try {
        const body = await resp.json();
        if (body && body.error) msg = body.error;
      } catch (_) { /* use default status message */ }
      throw new Error(msg);
    }
    return await resp.json();
  } finally {
    pendingRequests--;
  }
}

const api = {
  isBusy() { return pendingRequests > 0; },
  contacts(q, limit, offset) {
    const signal = cancelPending();
    const qs = new URLSearchParams();
    if (q) qs.set('q', q);
    if (limit) qs.set('limit', limit);           // issue #27：分页（不传 ⇒ 后端返回全部）
    if (offset) qs.set('offset', offset);
    const s = qs.toString();
    return fetchJSON(`/api/contacts${s ? '?' + s : ''}`, signal);
  },
  messages(params) {
    const signal = cancelPending();
    const qs = new URLSearchParams();
    qs.set('chat_id', params.chat_id);
    if (params.page) qs.set('page', params.page);
    if (params.per_page) qs.set('per_page', params.per_page);
    if (params.start_date) qs.set('start_date', params.start_date);
    if (params.end_date) qs.set('end_date', params.end_date);
    if (params.type) qs.set('type', params.type);
    if (params.sender) qs.set('sender', params.sender);
    if (params.keyword) qs.set('keyword', params.keyword);
    // T19：深链定位参数（`/chat?open=..&focus=<local_id>&ft=<create_time>`，见
    // `app.js` 与 `.docs/agent-context/03-interfaces/api.md` 的 A1.2）。
    // URL 的组装**只有这一处** —— Task 18 因为这份白名单漏了这两项，在页面里复制了
    // 一份 URL 组装来绕开，本轮收掉。
    // ⚠️ 判据**不是**真值判断：`focus_local_id=0` / `focus_create_time=0` 是**合法**取值
    // （页面的 `_focusIntOrNull` 接受 0），用真值判断会把它们丢掉 ⇒ 后端只收到一半参数
    // → 400，而页面会把"参数错"显示成"加载消息失败"。
    if (params.focus_local_id !== undefined && params.focus_local_id !== null
        && params.focus_local_id !== '') {
      qs.set('focus_local_id', params.focus_local_id);
    }
    if (params.focus_create_time !== undefined && params.focus_create_time !== null
        && params.focus_create_time !== '') {
      qs.set('focus_create_time', params.focus_create_time);
    }
    return fetchJSON(`/api/messages?${qs.toString()}`, signal);
  },
  messageDetail(id, chatId) { const signal = cancelPending(); return fetchJSON(`/api/messages/${id}?chat_id=${encodeURIComponent(chatId||'')}`, signal); },
  chatStats(chatId) { const signal = cancelPending(); return fetchJSON(`/api/chat/${encodeURIComponent(chatId)}/stats`, signal); },
  chatDates(chatId) { const signal = cancelPending(); return fetchJSON(`/api/chat/${encodeURIComponent(chatId)}/dates`, signal); },
  groupInfo(chatId) { const signal = cancelPending(); return fetchJSON(`/api/chat/${encodeURIComponent(chatId)}/group-info`, signal); },
  mediaUrl(path) { return path ? `/api/media?path=${encodeURIComponent(path)}` : null; },
  hardlinkMediaUrl(mediaInfo, fallbackType, localId) {
    if (!mediaInfo || (!mediaInfo.local_path && !mediaInfo.md5)) return null;
    const md5 = encodeURIComponent(mediaInfo.md5 || '');
    const path = encodeURIComponent(mediaInfo.local_path || '');
    const fname = encodeURIComponent(mediaInfo.file_name || '');
    const mtype = mediaInfo.media_type || fallbackType || 0;
    let url = `/api/hardlink-media?md5=${md5}&path=${path}&type=${mtype}&file_name=${fname}`;
    if (localId) url += `&local_id=${localId}`;
    return url;
  },
  voiceUrl(mediaInfo, createTime, localId) {
    const path = (mediaInfo && mediaInfo.voice_path) || '';
    if (!path) return null;
    let url = `/api/voice?path=${encodeURIComponent(path)}`;
    if (createTime) url += `&create_time=${createTime}`;
    if (localId) url += `&local_id=${localId}`;
    // 带上当前会话，便于后端在多个 media 分片里精确匹配同 local_id 的语音
    try {
      if (typeof Store !== 'undefined' && Store.data && Store.data.activeChat) {
        url += `&chat=${encodeURIComponent(Store.data.activeChat.id)}`;
      }
    } catch (e) { /* ignore */ }
    return url;
  },
  addressBook(params) {
    var signal = cancelPending();
    var qs = '';
    if (params) {
      var parts = [];
      Object.keys(params).forEach(function(k) {
        if (params[k] !== undefined && params[k] !== null && params[k] !== '') {
          parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
      });
      if (parts.length) qs = '?' + parts.join('&');
    }
    return fetchJSON('/api/address-book' + qs, signal);
  },
  addressBookGroups() { const signal = cancelPending(); return fetchJSON('/api/address-book/groups', signal); },
  addressBookLabels() { const signal = cancelPending(); return fetchJSON('/api/address-book/labels', signal); },
  search(q, params) {
    const signal = cancelPending();
    const parts = ['q=' + encodeURIComponent(q || '')];
    Object.keys(params || {}).forEach(function(k) {
      if (params[k] !== undefined && params[k] !== null && params[k] !== '') {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
      }
    });
    return fetchJSON('/api/search?' + parts.join('&'), signal);
  },
  // 索引状态**不**走 `cancelPending()`（T9-A2）。
  // `cancelPending()` 会 abort 上一个请求，而"上一个请求"很可能正是**用户刚发起的
  // 搜索**：状态条刷新一下就把搜索静默取消了（页面只看到一句莫名错误）。
  // 状态有自己的生命周期，永不取消别的请求；它自己也不需要被取消（实测热路径
  // 3.0–3.7ms，而且页面从不 await 它）。页面对失败/挂住的处理是 `.catch` 隐藏状态条。
  searchStatus() { return fetchJSON('/api/search/status', new AbortController().signal); },
  // 构建**不得**带 `cancelPending()` 的 signal：任何一次搜索都会把构建流 abort 掉，
  // 而构建是一次写库操作，中途断开会留下半成品索引。
  searchBuild(body) {
    return fetch('/api/search/index', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  },
  addressBookExportUrl(params) {
    var qs = '';
    if (params) {
      var parts = [];
      Object.keys(params).forEach(function(k) {
        if (params[k] !== undefined && params[k] !== null && params[k] !== '') {
          parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
      });
      if (parts.length) qs = '?' + parts.join('&');
    }
    return '/api/address-book/export' + qs;
  },
  cleanupAnalyze() { const signal = cancelPending(); return fetchJSON('/api/cleanup/analyze', signal); },
  cleanupPreview(params) {
    const signal = cancelPending();
    return fetch('/api/cleanup/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
      signal: signal,
    }).then(function(resp) {
      if (!resp.ok) { return resp.json().then(function(b) { throw new Error(b.error || 'HTTP ' + resp.status); }); }
      return resp.json();
    });
  },
  cleanupExecute(params) {
    const signal = cancelPending();
    return fetch('/api/cleanup/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
      signal: signal,
    }).then(function(resp) {
      if (!resp.ok) { return resp.json().then(function(b) { throw new Error(b.error || 'HTTP ' + resp.status); }); }
      return resp.json();
    });
  },
};
