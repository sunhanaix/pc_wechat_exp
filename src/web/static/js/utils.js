// Pure helper functions — no DOM dependencies, no state references
// Loaded before components.js

const MSG_TYPE_LABELS = { 1:'文本', 3:'图片', 6:'文件', 34:'语音', 42:'名片', 43:'视频', 47:'表情', 48:'位置', 49:'链接', 50:'通话', 10000:'系统', 10002:'系统' };
const MSG_TYPE_CSS = { 3:'image', 34:'voice', 43:'video', 48:'location', 49:'link', 42:'card', 6:'file', 47:'emoji', 50:'call', 10000:'system', 10002:'system' };

const formatTime = (ts) => {
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

const formatDateStr = (dateStr) => {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  const days = ['日','一','二','三','四','五','六'];
  return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日 星期${days[d.getDay()]}`;
};

const formatSize = (bytes) => {
  if (!bytes || bytes <= 0) return '';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + 'MB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(0) + 'KB';
  return bytes + 'B';
};

const _AVATAR_COLORS = ['#e94560','#569cd6','#56d364','#f0883e','#c084fc','#ffd54f','#58a6ff'];

const _avatarColor = (id) => {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = ((h << 5) - h) + id.charCodeAt(i);
  return _AVATAR_COLORS[Math.abs(h) % _AVATAR_COLORS.length];
};

const _avatarChar = (name) => {
  if (!name) return '?';
  for (let c of name) {
    if (/[一-鿿぀-ヿ가-힯\w]/.test(c)) return c;
  }
  return '?';
};

const _sysMsgIcon = (text) => {
  if (!text) return '';
  const t = String(text);
  if (/红包/.test(t)) return '🧧 ';
  if (/邀请.*加入/.test(t)) return '👋 ';
  if (/修改了群公告/.test(t) || /发布了群公告/.test(t)) return '📝 ';
  if (/改群名为/.test(t)) return '✏️ ';
  if (/移出群聊/.test(t) || /被移除/.test(t)) return '🚫 ';
  if (/退出了群聊/.test(t)) return '🚶 ';
  return '';
};

// Convert GCJ-02 (Mars coordinate system, used by WeChat location / Amap) to BD-09 (Baidu Maps)
// WeChat stores location coordinates in GCJ-02. Amap uses GCJ-02 natively; Baidu Maps requires BD-09.
const gcj02ToBd09 = (lng, lat) => {
  const X_PI = Math.PI * 3000.0 / 180.0;
  const z = Math.sqrt(lng * lng + lat * lat) + 0.00002 * Math.sin(lat * X_PI);
  const theta = Math.atan2(lat, lng) + 0.000003 * Math.cos(lng * X_PI);
  return {
    lng: z * Math.cos(theta) + 0.0065,
    lat: z * Math.sin(theta) + 0.006
  };
};

const escapeHtml = (s) => {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
};

const escapeAttr = (s) => {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
};

const jsQuote = (s) => JSON.stringify(s);

const showLoading = () => { document.getElementById('message-list').innerHTML = '<div class="loading"><div class="loading-icon">&#x23f3;</div>加载消息中...</div>'; };

const showError = (msg) => { document.getElementById('message-list').innerHTML = `<div class="error-msg">&#x26a0; ${escapeHtml(msg)}</div>`; };

const openLightbox = (url) => {
  const lightbox = document.getElementById('lightbox');
  lightbox.classList.remove('zoomed');
  lightbox.scrollTop = lightbox.scrollLeft = 0;
  document.getElementById('lightbox-img').src = url;
  lightbox.style.display = 'flex';
};

const showChatUI = (show) => {
  const d = show ? '' : 'none';
  document.getElementById('welcome').style.display = show ? 'none' : '';
  ['chat-header','filter-bar','message-list','pagination-bar'].forEach(id => document.getElementById(id).style.display = d);
};
