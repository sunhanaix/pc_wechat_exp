(() => {
  const source = document.getElementById('sync-source');
  const output = document.getElementById('sync-output');
  const start = document.getElementById('sync-start');
  const stop = document.getElementById('sync-stop');
  const error = document.getElementById('sync-error');
  const state = document.getElementById('sync-state');
  let busy = false;
  async function refresh() {
    try {
      const response = await fetch('/api/sync/status');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!source.value && data.source_dir) source.value = data.source_dir;
      start.disabled = busy || data.running;
      stop.disabled = busy || !data.running;
      source.disabled = output.disabled = busy || data.running;
      state.replaceChildren();
      const entries = [
        ['状态', data.running ? (data.ready ? '同步中' : '正在准备 / 重试') : '已停止'],
        ['查看器数据', data.viewer_active ? '实时数据' : '原数据'],
        ['实时目录', data.output_dir || '启动后自动创建'],
        ['已同步数据库', data.synced_files || 0],
        ['新增消息（本次运行）', data.new_messages || 0],
        ['最近同步', data.last_sync ? new Date(data.last_sync * 1000).toLocaleString() : '尚未同步'],
        ['最近错误', data.last_error || '无']
      ];
      for (const [label, value] of entries) {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = String(value);
        dd.style.margin = '0';
        state.append(dt, dd);
      }
    } catch (e) { error.textContent = e.message; }
  }
  async function control(action) {
    busy = true; start.disabled = stop.disabled = true; error.textContent = '';
    try {
      const response = await fetch(`/api/sync/${action}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(action === 'start' ? {db_dir: source.value.trim(), output_dir: output.value.trim()} : {})
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '服务已运行，请先停止再切换');
    } catch (e) { error.textContent = e.message; }
    finally { busy = false; await refresh(); }
  }
  start.addEventListener('click', () => control('start'));
  stop.addEventListener('click', () => control('stop'));
  refresh();
  setInterval(refresh, 2000);
})();
