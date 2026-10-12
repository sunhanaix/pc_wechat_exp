const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const code = fs.readFileSync(process.argv[2], 'utf8');

function fixture() {
  const listeners = {}, timers = [], calls = [], page = {};
  let busy = false;
  const list = {scrollTop: 80, scrollHeight: 1000, clientHeight: 100};
  const data = {activeChat: {id: 'active'}, loading: false, contactsLoading: false,
    contactsQuery: 'search', pagination: {page: 3}, filters: {keyword: 'keep'}};
  vm.runInNewContext(code, {
    EventSource: class { addEventListener(type, fn) { listeners[type] = fn; } close() { calls.push('close'); } },
    Store: {data}, api: {isBusy: () => busy},
    document: {getElementById: () => list},
    window: {addEventListener: (type, fn) => { page[type] = fn; }},
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
    loadContacts: async q => { calls.push(['contacts', q]); },
    loadMessages: async () => { calls.push('messages'); list.scrollTop = 0; }
  });
  return {data, calls, list, timers, page, setBusy: b => { busy = b; },
    emit: e => listeners['data.updated']({data: JSON.stringify(e)}),
    tick: async () => { const fn = timers.shift(); if (fn) await fn(); }};
}

(async () => {
  const f = fixture();
  f.setBusy(true);
  for (let i = 0; i < 20; i++) f.emit({chats: ['active']});
  assert.equal(f.timers.length, 1);
  await f.tick();
  assert.equal(f.calls.length, 0, 'push must not cancel an in-flight user request');
  f.setBusy(false);
  await f.tick();
  assert.deepEqual(f.calls, [['contacts', 'search'], 'messages']);
  assert.equal(f.data.pagination.page, 3);
  assert.equal(f.data.filters.keyword, 'keep');
  assert.equal(f.list.scrollTop, 80);

  const other = fixture();
  other.emit({chats: ['other']}); await other.tick();
  assert.deepEqual(other.calls, [['contacts', 'search']]);
  other.emit({resync: true, chats: []}); await other.tick();
  assert.equal(other.calls.filter(c => c === 'messages').length, 1);
  other.page.pagehide();
  assert.equal(other.calls.at(-1), 'close');
  console.log('Live update guards passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
