// chat-header.js — Chat header component
// Renders the current chat name, avatar, message count, and group-info toggle.
// Provides a ChatHeader class extending Component.

class ChatHeader extends Component {
  template(data) {
    if (!data || !data.name) return '';
    const avatarFallbackStyle = `width:36px;height:36px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:white;flex-shrink:0;background:${_avatarColor(data.id || '')}`;
    const avatarFallbackChar = _avatarChar(data.name);
    const avatarImg = data.avatar_url
      ? `<img class="header-avatar" src="${escapeAttr(data.avatar_url)}" alt=""
           onerror="this.onerror=null;this.outerHTML='<div class=\\'header-avatar-fallback\\' style=\\'${avatarFallbackStyle}\\'>${avatarFallbackChar}</div>'"
           style="width:36px;height:36px;border-radius:50%;object-fit:cover" />`
      : `<div class="header-avatar-fallback" style="${avatarFallbackStyle}">${avatarFallbackChar}</div>`;
    const statsText = data.stats || '';
    const giBtn = data.type === 'group'
      ? '<button class="gi-btn" title="群信息">&#x2139;</button>'
      : '';
    // issue #24：批量转写本会话语音。注意必须在这里生成 —— 头部是 JS 重建的，
    // 写在 index.html 静态模板里的按钮会被本次渲染覆盖掉（用户会"找不到"）。
    const transBtn = '<button id="btn-transcribe-all" class="hdr-act" '
      + 'title="批量转写本会话语音（已有文字的会跳过；再点一次可停止）">批量转写</button>';
    return `
      ${avatarImg}
      <div class="header-info">
        <span class="chat-name">${escapeHtml(data.name)}</span>
        <span class="chat-stats">${escapeHtml(statsText)}</span>
      </div>
      ${giBtn}
      ${transBtn}`;
  }

  mount() {
    // Group info toggle button click (delegated to survive re-render)
    this.el.addEventListener('click', (e) => {
      if (e.target.closest('.gi-btn')) {
        Store.emit('group-info-toggle');
        return;
      }
      if (e.target.closest('#btn-transcribe-all')) {
        if (window.VoicePlayer && VoicePlayer.transcribeAll) VoicePlayer.transcribeAll();
      }
    });
  }

  updateStats(statsText) {
    const el = this.el.querySelector('.chat-stats');
    if (el) el.textContent = statsText;
  }
}
