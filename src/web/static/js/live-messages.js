/* Coalesced updates: leave filters, page selection and in-flight actions intact. */
(() => {
  if (typeof EventSource !== 'function') return;
  const stream = new EventSource('/api/sync/events');
  let dirty = false, all = false, chats = new Set(), timer = null;
  function schedule() {
    if (!timer) timer = setTimeout(refresh, 300);
  }
  async function refresh() {
    timer = null;
    if (!dirty) return;
    if (Store.data.loading || Store.data.contactsLoading || api.isBusy()) { schedule(); return; }
    const active = Store.data.activeChat;
    const refreshMessages = active && (all || chats.has(active.id));
    dirty = all = false; chats.clear();
    const list = document.getElementById('message-list');
    const previousScroll = list.scrollTop;
    const wasAtBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    await loadContacts(Store.data.contactsQuery || '');
    if (api.isBusy()) { dirty = all = true; schedule(); return; }
    if (refreshMessages && Store.data.activeChat && Store.data.activeChat.id === active.id) {
      await loadMessages();
      list.scrollTop = wasAtBottom ? list.scrollHeight : previousScroll;
    }
    if (dirty) schedule();
  }
  stream.addEventListener('data.updated', e => {
    try {
      const event = JSON.parse(e.data);
      dirty = true;
      all = all || event.resync || event.type === 'sync.ready';
      for (const id of event.chats || []) chats.add(id);
      schedule();
    } catch (_) { /* Ignore malformed notifications; reconnect reconciles data. */ }
  });
  window.addEventListener('pagehide', () => { stream.close(); clearTimeout(timer); });
})();
