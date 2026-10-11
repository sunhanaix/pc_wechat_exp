// voice-player.js — Voice player singleton and transcription
// Manages audio playback with per-bubble progress bar and seeking.
// Provides VoicePlayer singleton for inline onclick handlers in message-bubble templates.

class VoicePlayerComponent {
  constructor() {
    this.activeMsgId = null;
    this.audio = null;
    this._interval = null;
    this._rate = 2.0;  // default 2x speed
  }

  get rate() { return this._rate; }

  setRate(msgId, rate) {
    this._rate = rate;
    if (this.audio) this.audio.playbackRate = rate;
    this._updateSpeedBtns(msgId, rate);
  }

  _updateSpeedBtns(msgId, rate) {
    const ctrl = document.getElementById('vp-ctrl-' + msgId);
    if (!ctrl) return;
    for (const b of ctrl.querySelectorAll('.vp-speed-btn')) {
      b.classList.toggle('active', parseFloat(b.dataset.rate) === rate);
    }
  }

  stop() {
    if (this._interval) { clearInterval(this._interval); this._interval = null; }
    if (this.audio) { this.audio.pause(); this.audio = null; }
    const prev = this.activeMsgId;
    this.activeMsgId = null;
    if (prev) { this._updateUI(prev, 'pause'); }
  }

  _updateUI(msgId, action) {
    const ctrl = document.getElementById('vp-ctrl-' + msgId);
    if (!ctrl) return;
    const btn = ctrl.querySelector('.vp-play-btn');
    const bar = ctrl.querySelector('.vp-bar-fill');
    const curEl = ctrl.querySelector('.vp-time-cur');
    const durEl = ctrl.querySelector('.vp-time-dur');
    if (action === 'play') {
      if (btn) btn.textContent = '⏸';
    } else if (action === 'pause') {
      if (btn) btn.textContent = '▶';
      if (bar) bar.style.width = '0%';
    } else if (action === 'ended') {
      if (btn) btn.textContent = '▶';
      if (bar) bar.style.width = '0%';
    }
    if (action === 'time' && this.audio) {
      if (bar && this.audio.duration) bar.style.width = (this.audio.currentTime / this.audio.duration * 100) + '%';
      if (curEl) curEl.textContent = this._fmt(this.audio.currentTime);
      if (durEl && this.audio.duration) durEl.textContent = this._fmt(this.audio.duration);
    }
  }

  _fmt(sec) {
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return m > 0 ? m + '′' + String(s).padStart(2, '0') + '″' : s + '″';
  }

  toggle(msgId, url) {
    if (this.activeMsgId === msgId) {
      if (this.audio && !this.audio.paused) {
        this.audio.pause();
        this._updateUI(msgId, 'pause');
      } else if (this.audio) {
        this.audio.play();
        this._updateUI(msgId, 'play');
      }
      return;
    }
    this.stop();
    this.activeMsgId = msgId;
    const a = new Audio(url);
    this.audio = a;
    a.preload = 'auto';
    a.playbackRate = this._rate;
    this._updateSpeedBtns(msgId, this._rate);
    const self = this;
    a.addEventListener('loadedmetadata', function() { self._updateUI(msgId, 'time'); });
    a.addEventListener('timeupdate', function() { self._updateUI(msgId, 'time'); });
    a.addEventListener('ended', function() { self.stop(); });
    a.addEventListener('error', function() {
      const dw = document.getElementById('voice-download');
      if (dw) { dw.href = url; dw.style.display = 'inline-block'; }
      self.stop();
    });
    a.play().then(function() {
      self._updateUI(msgId, 'play');
    }).catch(function() {
      const dw = document.getElementById('voice-download');
      if (dw) { dw.href = url; dw.style.display = 'inline-block'; }
      self.stop();
    });
  }

  seek(msgId, evt) {
    if (this.activeMsgId !== msgId || !this.audio || !this.audio.duration) return;
    const rect = evt.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (evt.clientX - rect.left) / rect.width));
    this.audio.currentTime = ratio * this.audio.duration;
  }

  playVoice(url) {
    const a = new Audio(url);
    a.play().catch(function() {});
  }

  async transcribeVoice(msgId, voiceUrl) {
    const resultEl = document.getElementById('vp-result-' + msgId);
    const btn = document.getElementById('vp-trans-btn-' + msgId);
    if (!resultEl || !btn) return;
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = '识别中...';
    resultEl.style.display = '';
    resultEl.textContent = '';
    try {
      const transcriber = window.WhisperTranscriber;
      if (!transcriber) {
        throw new Error('Whisper 模块未加载，请刷新页面后重试');
      }
      // 设置里选了商业 ASR（百度）时，走服务端调用，不再用本地模型
      let cfg = null;
      try { cfg = await transcriber.settings(); } catch (e) { cfg = null; }
      if (cfg && cfg.engine === 'baidu') {
        resultEl.textContent = '正在识别（百度语音识别）…';
        // 把语音地址里的 path / chat / create_time / local_id 原样回传：这些参数是
        // 后端定位并提取音频的唯一依据（尤其是尚未播放过、缓存还没生成的语音）。
        // 以前固定传 create_time=0 / local_id=0，会覆盖掉真实值，导致「先点转文字
        // 报音频文件不存在，先播放一次就正常」。
        const q = new URLSearchParams((voiceUrl || '').split('?')[1] || '');
        const payload = { voice_url: voiceUrl };
        ['path', 'chat', 'create_time', 'local_id'].forEach(function (k) {
          const v = q.get(k);
          if (v) payload[k] = v;
        });
        const resp = await fetch('/api/asr/commercial', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const d = await resp.json().catch(function () { return {}; });
        if (!resp.ok || d.error) {
          const msg2 = d.message || ('HTTP ' + resp.status);
          if (!cfg.has_baidu_key || /未配置百度/.test(msg2)) {
            resultEl.className = 'vp-trans-result error';
            resultEl.innerHTML = '百度语音识别未配置：请到 <a href="/settings" target="_blank" ' +
              'style="color:#58a6ff">设置</a> 页填写 API Key / Secret Key，或把引擎切回「本地模型」。';
            return;
          }
          throw new Error(msg2);
        }
        resultEl.textContent = d.text || '未识别到语音内容';
        resultEl.className = 'vp-trans-result ' + (d.text ? 'success' : 'empty');
        return;
      }
      // 模型没下载全时不要硬加载（transformers.js 会抛出难懂的错），直接引导下载
      if (!transcriber.loaded) {
        const st = await transcriber.status();
        if (st && st.complete === false) {
          this.showModelPrompt(msgId, voiceUrl, st);
          return;
        }
      }
      const text = await transcriber.transcribe(voiceUrl, function(progress) {
        resultEl.textContent = progress;
      });
      if (text) {
        resultEl.textContent = text;
        resultEl.className = 'vp-trans-result success';
        saveTranscriptToServer(voiceUrl, text);
      } else {
        resultEl.textContent = '未识别到语音内容';
        resultEl.className = 'vp-trans-result empty';
      }
    } catch (e) {
      const msg = (e && e.message) || String(e);
      if (/Unsupported model type|model_file_not_found|404|Failed to fetch|NetworkError/i.test(msg)) {
        let st = null;
        try {
          st = window.WhisperTranscriber && window.WhisperTranscriber.status
             ? await window.WhisperTranscriber.status() : null;
        } catch (err) { st = null; }
        this.showModelPrompt(msgId, voiceUrl, st || {});
      } else {
        resultEl.textContent = '识别失败: ' + msg;
        resultEl.className = 'vp-trans-result error';
      }
    } finally {
      btn.disabled = false;
      btn.textContent = 'T';
    }
  }

  // issue #24 反馈③：改为"滚动到才加载"——只给进入视口的语音查已存文字，
  // 每条只查一次（data-asr-asked 标记），按会话分组、每批最多 200 条。
  // 浏览器不支持 IntersectionObserver 时回退到整体加载（loadSavedTranscripts）。
  transcriptsOnScroll() {
    if (typeof IntersectionObserver === 'undefined') { this.loadSavedTranscripts(); return; }
    const self = this;
    if (!this._asrObserver) {
      this._asrObserver = new IntersectionObserver(function (entries) {
        const batch = [];
        entries.forEach(function (e) {
          if (!e.isIntersecting) return;
          const el = e.target;
          self._asrObserver.unobserve(el);
          if (el.dataset.asrAsked) return;
          el.dataset.asrAsked = '1';
          const msgId = el.id.replace('vp-result-', '');
          const holder = el.closest('.msg-bubble') || el.parentElement || document;
          const play = holder.querySelector('[onclick*="VoicePlayer.toggle(' + msgId + ',"]');
          const m = play && /VoicePlayer\.toggle\(\d+,'([^']+)'/.exec(play.getAttribute('onclick') || '');
          if (!m) return;
          const q = new URLSearchParams((m[1].split('?')[1] || ''));
          const chat = q.get('chat'), ct = q.get('create_time'), lid = q.get('local_id');
          if (!chat || !ct || !lid) return;
          batch.push({ msgId: msgId, chat: chat, key: ct + ':' + lid });
        });
        self._fetchTranscripts(batch);
      }, { rootMargin: '300px 0px' });
    }
    document.querySelectorAll('[id^="vp-result-"]:not([data-asr-asked])').forEach(function (el) {
      self._asrObserver.observe(el);
    });
  }

  // 按会话分组批量取回并填充（一个会话一次请求；每批上限 200 条）
  _fetchTranscripts(batch) {
    if (!batch || !batch.length) return;
    const groups = {};
    const where = {};
    batch.slice(0, 200).forEach(function (it) {
      (groups[it.chat] = groups[it.chat] || []).push(it.key);
      (where[it.chat + '|' + it.key] = where[it.chat + '|' + it.key] || []).push(it.msgId);
    });
    Object.keys(groups).forEach(function (chat) {
      fetch('/api/voice/transcripts?chat=' + encodeURIComponent(chat) +
            '&items=' + encodeURIComponent(groups[chat].join(',')))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          const rows = (d && d.transcripts) || {};
          Object.keys(rows).forEach(function (key) {
            const text = rows[key] && rows[key].text;
            if (!text) return;
            (where[chat + '|' + key] || []).forEach(function (msgId) {
              const resultEl = document.getElementById('vp-result-' + msgId);
              const btn = document.getElementById('vp-trans-btn-' + msgId);
              if (resultEl && !resultEl.textContent) {
                resultEl.textContent = text;
                resultEl.className = 'vp-trans-result success';
                resultEl.style.display = '';
              }
              if (btn && !btn.disabled) {
                btn.textContent = '重新识别';
                btn.title = '重新识别（会覆盖已保存的文字）';
              }
            });
          });
        })
        .catch(function (e) { console.warn('读取已存转写失败（不影响其它功能）', e); });
    });
  }

  // issue #24：载入会话时把**已经转写并保存过**的文字自动带出来（只读，不触发识别）。
  // 语音消息的播放按钮上带着 voiceUrl（含 chat/create_time/local_id），从 DOM 直接读，
  // 按会话分组各发一次请求，避免每条语音一个请求。
  loadSavedTranscripts() {
    try {
      const groups = {};       // chat -> [ 'ct:lid', ... ]
      const targets = {};      // 'chat|ct:lid' -> [msgId, ...]
      document.querySelectorAll('[id^="vp-result-"]').forEach(function (el) {
        const msgId = el.id.slice('vp-result-'.length);
        const holder = el.closest('.msg-bubble') || el.parentElement || document;
        const btn = holder.querySelector('[onclick*="VoicePlayer.toggle(' + msgId + ',"]');
        if (!btn) return;
        const m = /VoicePlayer\.toggle\(\d+,'([^']+)'/.exec(btn.getAttribute('onclick') || '');
        if (!m) return;
        const q = new URLSearchParams((m[1].split('?')[1] || ''));
        const chat = q.get('chat'), ct = q.get('create_time'), lid = q.get('local_id');
        if (!chat || !ct || !lid) return;
        if (Object.keys(targets).length > 400) return;   // issue #24 反馈③：设上限，避免大会话卡页面
        const key = ct + ':' + lid;
        (groups[chat] = groups[chat] || []).push(key);
        (targets[chat + '|' + key] = targets[chat + '|' + key] || []).push(msgId);
      });
      Object.keys(groups).forEach(function (chat) {
        fetch('/api/voice/transcripts?chat=' + encodeURIComponent(chat) +
              '&items=' + encodeURIComponent(groups[chat].join(',')))
          .then(function (r) { return r.json(); })
          .then(function (d) {
            const rows = (d && d.transcripts) || {};
            Object.keys(rows).forEach(function (key) {
              const text = rows[key] && rows[key].text;
              if (!text) return;
              (targets[chat + '|' + key] || []).forEach(function (msgId) {
                const resultEl = document.getElementById('vp-result-' + msgId);
                const btn = document.getElementById('vp-trans-btn-' + msgId);
                if (resultEl && !resultEl.textContent) {
                  resultEl.textContent = text;
                  resultEl.className = 'vp-trans-result success';
                  resultEl.style.display = '';
                }
                if (btn && !btn.disabled) {
                  btn.textContent = '重新识别';
                  btn.title = '重新识别（会覆盖已保存的文字）';
                }
              });
            });
          })
          .catch(function (e) { console.warn('读取已存转写失败（不影响其它功能）', e); });
      });
    } catch (e) {
      console.warn('读取已存转写失败（不影响其它功能）', e);
    }
  }

  // issue #24 反馈第 3 点：一键把本会话里**还没转写**的语音顺序转一遍。
  // 复用既有 transcribeVoice（本地模型在浏览器里跑）；再点一次按钮 = 停止。
  async transcribeAll() {
    const btn = document.getElementById('btn-transcribe-all');
    if (this._batchRunning) {                 // 再点一次 → 请求停止
      this._batchCancel = true;
      if (btn) btn.textContent = '停止中…';
      return;
    }
    this._batchRunning = true;
    this._batchCancel = false;
    const targets = [];
    document.querySelectorAll('.vp-trans-btn').forEach(function (b) {
      const msgId = b.id.replace('vp-trans-btn-', '');
      const resultEl = document.getElementById('vp-result-' + msgId);
      const holder = b.closest('.msg-bubble') || b.parentElement || document;
      const play = holder.querySelector('[onclick*="VoicePlayer.toggle(' + msgId + ',"]');
      const m = play && /VoicePlayer\.toggle\(\d+,'([^']+)'/.exec(play.getAttribute('onclick') || '');
      if (!m) return;
      if (resultEl && resultEl.textContent.trim()) return;   // 已有文字 ⇒ 跳过
      targets.push({ msgId: msgId, url: m[1] });
    });
    let done = 0;
    try {
      for (let i = 0; i < targets.length; i++) {
        if (this._batchCancel) break;
        if (btn) btn.textContent = 'T ' + (i + 1) + '/' + targets.length;
        try {
          await this.transcribeVoice(targets[i].msgId, targets[i].url);
          done++;
        } catch (e) { /* 单条失败不中断整批 */ }
      }
    } finally {
      this._batchRunning = false;
      this._batchCancel = false;
      if (btn) {
        btn.textContent = 'T';
        btn.title = '批量转写本会话语音（本次完成 ' + done + '/' + targets.length + '）';
      }
    }
  }

  // 语音识别模型缺失时的引导（首次使用需下载约 42MB，之后完全离线可用）
  showModelPrompt(msgId, voiceUrl, st) {
    const resultEl = document.getElementById('vp-result-' + msgId);
    if (!resultEl) return;
    const size = (st && st.sizeMb) || 76;
    const current = (window.WhisperTranscriber && window.WhisperTranscriber.modelId) || '';
    const list = (st && st.available) || [];
    let sel = '<select id="asr-model-sel-' + msgId + '" style="font-size:11px;padding:2px 6px;' +
              'background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;margin-left:4px">';
    if (!list.length) {
      sel += '<option value="' + current + '">' + current + '</option>';
    } else {
      list.forEach(function(m) {
        const chosen = (m.model === current) ? ' selected' : '';
        const rec = m.recommended ? '（推荐）' : '';
        sel += '<option value="' + m.model + '"' + chosen + '>' + m.label + rec +
               ' · ' + m.sizeMb + 'MB</option>';
      });
    }
    sel += '</select>';
    resultEl.className = 'vp-trans-result error';
    resultEl.style.display = '';
    resultEl.innerHTML =
      '<div style="margin-bottom:4px">🧠 语音识别模型未就绪（首次需下载，之后可离线使用）</div>' +
      '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">' + sel +
      '<button class="btn" style="padding:2px 10px;font-size:11px" onclick="VoicePlayer.installModel(' +
      msgId + ', \'' + voiceUrl + '\')">⬇ 下载并使用</button></div>' +
      '<div class="asr-progress" style="display:none;margin-top:6px">' +
      '  <div style="height:6px;background:#21262d;border-radius:3px;overflow:hidden">' +
      '    <div class="asr-bar" style="height:100%;width:0%;background:#1f6feb;transition:width .2s"></div></div>' +
      '  <div class="asr-text" style="font-size:11px;color:#8b949e;margin-top:4px"></div></div>';
  }

  _asrProgress(msgId, pct, text) {
    const box = document.querySelector('#vp-result-' + msgId + ' .asr-progress');
    if (!box) return;
    box.style.display = 'block';
    const bar = box.querySelector('.asr-bar');
    if (bar && typeof pct === 'number') bar.style.width = Math.max(2, Math.min(100, Math.round(pct * 100))) + '%';
    const txt = box.querySelector('.asr-text');
    if (txt && text) txt.textContent = text;
  }

  async installModel(msgId, voiceUrl) {
    const resultEl = document.getElementById('vp-result-' + msgId);
    const t = window.WhisperTranscriber;
    if (!t) return;
    // 用户在弹窗里选了哪个模型就用哪个
    const sel = document.getElementById('asr-model-sel-' + msgId);
    const model = (sel && sel.value) ? sel.value : t.modelId;
    if (t.setModel) t.setModel(model);
    const label = sel && sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : model;
    this._asrProgress(msgId, 0, '准备下载 ' + label + ' …');
    try {
      await t.install(function(msg, pct) { VoicePlayer._asrProgress(msgId, pct, msg); });
      this._asrProgress(msgId, 1, '✅ 模型已就绪，正在识别语音…');
      // 模型文件已在服务端就绪：清掉内存里失败的 pipeline，直接继续识别（无需刷新页面）
      if (t.reset) t.reset();
      setTimeout(function() { VoicePlayer.transcribeVoice(msgId, voiceUrl); }, 300);
    } catch (e) {
      const emsg = (e && e.message) || e;
      this._asrProgress(msgId, 0, '❌ 下载失败: ' + emsg);
      try {
        sessionStorage.setItem('asr_pending', JSON.stringify({ msgId: msgId, url: voiceUrl }));
      } catch (err) { /* ignore */ }
    }
  }

  // 下载完模型刷新页面后，自动重试之前那次转写
  tryAutoRetry() {
    let raw = null;
    try { raw = sessionStorage.getItem('asr_pending'); } catch (e) { return; }
    if (!raw) return;
    try { sessionStorage.removeItem('asr_pending'); } catch (e) { /* ignore */ }
    let info = null;
    try { info = JSON.parse(raw); } catch (e) { return; }
    if (!info || !info.msgId) return;
    setTimeout(function() {
      if (document.getElementById('vp-trans-btn-' + info.msgId)) {
        VoicePlayer.transcribeVoice(info.msgId, info.url);
      }
    }, 2500);
  }
}

// Singleton instance for inline onclick handlers in message-bubble templates
const VoicePlayer = new VoicePlayerComponent();
// issue #24：默认引擎的识别跑在浏览器里，结果不会经过服务端 —— 识别成功后回传入库，
// 这样「下次载入自动带出文字」与「导出带文字」才能拿到。失败只 warn，不影响用户看到文字。
function saveTranscriptToServer(voiceUrl, text) {
  try {
    if (!text || !text.trim()) return;
    fetch('/api/voice/transcripts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice_url: voiceUrl, text: text, engine: 'local' }),
    }).catch(function (e) { console.warn('转写结果回传失败（不影响本次显示）', e); });
  } catch (e) {
    console.warn('转写结果回传失败（不影响本次显示）', e);
  }
}

const transcribeVoice = (msgId, voicePath) => VoicePlayer.transcribeVoice(msgId, voicePath);

// 模型下载完成刷新页面后，自动重试之前被中断的转写
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => VoicePlayer.tryAutoRetry());
  document.addEventListener('DOMContentLoaded', () => VoicePlayer.transcriptsOnScroll());
} else {
  setTimeout(() => VoicePlayer.tryAutoRetry(), 1500);
  setTimeout(() => VoicePlayer.transcriptsOnScroll(), 1800);
  // 会话切换/翻页后新渲染的语音同样按需加载
  setInterval(() => VoicePlayer.transcriptsOnScroll(), 3000);
}
