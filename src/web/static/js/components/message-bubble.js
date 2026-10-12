// message-bubble.js — Message bubble list component
// Renders full message list with date dividers, bubbles, and expanded detail.
// Click delegation emits Store 'message-expanded' for expansion.

class MessageBubble extends Component {
  template(data) {
    const { messages, expandedMsg } = data;
    if (!messages || !messages.length) {
      return '<div class="empty-msg">未找到匹配的消息</div>';
    }
    let html = '', lastDate = '';
    messages.forEach((m) => {
      const dateStr = new Date(m.create_time * 1000).toISOString().split('T')[0];
      if (dateStr !== lastDate) {
        lastDate = dateStr;
        html += `<div class="date-divider"><span>${formatDateStr(dateStr)}</span></div>`;
      }
      html += this._renderBubble(m);
      if (m.id === expandedMsg) html += this._renderDetail(m);
    });
    return html;
  }

  mount() {
    this.el.addEventListener('click', (e) => {
      const row = e.target.closest('.msg-row');
      if (row) {
        const msgId = parseInt(row.dataset.msgId);
        Store.emit('message-expanded', msgId);
      }
    });
  }

  _renderBubble(m) {
    const sideClass = m.is_sender ? 'me' : 'other';
    // For type 49 (appmsg), check render_type to show correct tag
    const isType49File = m.msg_type === 49 && m.xml_parsed && m.xml_parsed.render_type === 'file';
    const effectiveType = isType49File ? 6 : m.msg_type;
    const tag = MSG_TYPE_LABELS[effectiveType];
    const cssCls = MSG_TYPE_CSS[effectiveType] || '';
    const tagHtml = tag && effectiveType !== 1 ? `<span class="msg-type-tag ${cssCls}">${tag}</span>` : '';
    const isSystem = m.msg_type === 10000 || m.msg_type === 10002;
    let content;
    if (m.msg_type === 1) {
      content = escapeHtml(m.content || '');
    } else if (m.msg_type === 34 && ((m.xml_parsed && m.xml_parsed.duration) || (m.media_info && m.media_info.duration))) {
      // 优先用 XML 里的真实时长（秒）；media_info.duration 是 packed_info 的字段，不可靠
      const dur = (m.xml_parsed && m.xml_parsed.duration) || m.media_info.duration;
      const durStr = dur >= 60 ? `${Math.floor(dur/60)}′${dur%60}″` : `${dur}″`;
      content = `<span class="msg-type-tag voice">语音</span> ${durStr}`;
    } else if ((m.msg_type === 3 || m.msg_type === 43 || m.msg_type === 6) && m.media_info) {
      const mi = m.media_info;
      const labels = {3:'图片', 43:'视频', 6:'文件'};
      const label = labels[m.msg_type] || '';
      const name = mi.file_name ? ' ' + escapeHtml(mi.file_name) : '';
      let sizeStr = '';
      if (mi.file_size && mi.file_size > 0) {
        sizeStr = ' ' + formatSize(mi.file_size);
      }
      content = `<span class="msg-type-tag ${MSG_TYPE_CSS[m.msg_type] || ''}">${label}</span>${name}${sizeStr}`;
    } else if (m.msg_type === 50 && m.xml_parsed && m.xml_parsed.call_msg) {
      content = tagHtml + escapeHtml(m.xml_parsed.call_msg);
    } else if (isType49File) {
      // type 49 file share — show as file with name and size
      const fTitle = (m.xml_parsed.title || '').replace(/^\[文件\]\s*/, '');
      const fSize = m.xml_parsed.size || 0;
      let fSizeStr = '';
      if (fSize > 0) {
        fSizeStr = ' ' + (fSize >= 1048576 ? (fSize/1048576).toFixed(1) + ' MB' : (fSize/1024).toFixed(0) + ' KB');
      }
      content = tagHtml + ' ' + escapeHtml(fTitle) + fSizeStr;
    } else if (m.msg_type === 10000 || m.msg_type === 10002) {
      const sysText = (m.xml_parsed && m.xml_parsed.text) || m.content || '';
      content = _sysMsgIcon(sysText) + escapeHtml(sysText);
    } else if (m.xml_parsed && (m.xml_parsed.text || m.xml_parsed.title)) {
      content = tagHtml + escapeHtml(m.xml_parsed.text || m.xml_parsed.title);
    } else {
      content = tagHtml || escapeHtml((m.content || '').substring(0, 200));
    }
    // Show avatar for non-self group messages
    let avatarHtml = '';
    if (m.sender_wxid && !m.is_sender) {
      avatarHtml = `<div class="msg-avatar"><img src="/api/avatar/${escapeAttr(m.sender_wxid)}" alt="" onerror="this.onerror=null;this.outerHTML='<div class=\\'msg-avatar-fallback\\' style=\\'width:32px;height:32px;border-radius:4px;display:inline-flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;color:white;flex-shrink:0;background:'+_avatarColor('${m.sender_wxid.replace(/'/g,"\\'")}')+'\\'>'+_avatarChar('${(m.sender_name||'').replace(/'/g,"\\'")}')+'</div>'" loading="lazy" /></div>`;
    }
    return `<div class="msg-row ${sideClass}" data-msg-id="${m.id}">
      ${sideClass === 'other' ? avatarHtml : ''}
      <div class="msg-bubble">
        <div class="msg-meta">
          <span class="msg-sender">${escapeHtml(m.sender_name)}</span>
          <span class="msg-time">${formatTime(m.create_time)}</span>
        </div>
        <div class="msg-text">${content}</div>
      </div>
      ${sideClass === 'me' ? avatarHtml : ''}
    </div>`;
  }

  _renderDetail(m) {
    const p = m.xml_parsed || {};
    const hasData = p && Object.keys(p).length > 0 && (p.image_path || p.voice_path || p.title || p.lat || p.nickname || p.text || p.file_path);
    const cls = m.is_sender ? 'own' : 'other';
    let body = '';
    switch (m.msg_type) {
    case 1:
      body = `<div style="white-space:pre-wrap">${escapeHtml(p.text || m.content || '')}</div>`;
      break;
    case 3: {
      const mi = m.media_info;
      const hlUrl = api.hardlinkMediaUrl(mi, 3, m.id);
      const imgPath = p.image_path || p.thumb_path || '';
      const imgUrl = hlUrl || (imgPath ? api.mediaUrl(imgPath) : '');
      // Lightbox: strip _t/_h suffix to request full-size image
      let lightboxUrl = imgUrl;
      if (mi && mi.md5 && hlUrl) {
        const stripSuffix = (s) => (s && (s.endsWith('_t') || s.endsWith('_h'))) ? s.slice(0, -2) : s;
        const stripPathSuffix = (s) => (s || '').replace(/_(t|h)(?=\.\w+$)/, '');
        const fullMd5 = stripSuffix(mi.md5);
        const fullPath = stripPathSuffix(mi.local_path || '');
        const fullName = stripPathSuffix(mi.file_name || '');
        if (fullMd5 !== mi.md5 || fullPath !== (mi.local_path || '') || fullName !== (mi.file_name || '')) {
          lightboxUrl = api.hardlinkMediaUrl({...mi, md5: fullMd5, local_path: fullPath, file_name: fullName}, 3, m.id) || imgUrl;
        }
      }
      if (lightboxUrl) lightboxUrl += '&quality=original';
      const isWx4 = mi && mi.file_name && mi.file_name.endsWith('.dat') && !mi.file_name.endsWith('_W.dat');
      if (imgUrl) {
        const fallbackMsg = isWx4 ? 'V2加密图片无法解码 — 请在微信中查看该图片后刷新重试' : '图片已过期或不可访问';
        const imgId = `img-${m.id}`;
        body = `<div style="text-align:center"><img id="${imgId}" class="bubble-image" src="${imgUrl}" alt="图片" onclick="event.stopPropagation();openLightbox('${lightboxUrl}')" onerror="ImageFallback.handle(this)"><div class="img-fallback" style="color:#8b949e;padding:16px;display:none">${fallbackMsg}</div>${mi&&mi.file_name?`<div style="color:#8b949e;font-size:10px;margin-top:4px">${escapeHtml(mi.file_name)} ${mi.file_size?((mi.file_size/1024).toFixed(1)+' KB'):''}</div>`:''}</div>`;
      } else if (mi && mi.md5) {
        body = `<div class="nontext-placeholder image-plc"><span>🖼</span><span>图片</span><div style="color:#484f58;font-size:10px;margin-top:4px">MD5: ${escapeHtml(mi.md5.substring(0,16))}... 文件未找到</div></div>`;
      } else {
        body = `<div class="nontext-placeholder image-plc"><span>🖼</span><span>图片</span><div style="color:#484f58;font-size:10px;margin-top:4px">媒体文件未找到</div></div>`;
      }
      break;
    }
    case 34: {
      const dur = p.duration || (m.media_info && m.media_info.duration) || 0;
      let durText = '';
      if (dur > 0) {
        durText = dur >= 60 ? `${Math.floor(dur/60)}′${dur%60}″` : `${dur}″`;
      }
      const vp = (m.media_info && m.media_info.voice_path) ? m.media_info : null;
      const voicePath = vp ? vp.voice_path : (p.voice_path || '');
      const ct = m.create_time || 0;
      const lid = m.id || 0;
      const voiceUrl = vp ? api.voiceUrl(vp, ct, lid) : (p.voice_path ? `/api/voice?path=${encodeURIComponent(p.voice_path)}&create_time=${ct}&local_id=${lid}` : '');
      if (voiceUrl) {
        body = `<div class="bubble-voice" id="vp-ctrl-${m.id}" onclick="event.stopPropagation()">
          <button class="vp-play-btn" onclick="VoicePlayer.toggle(${m.id},'${voiceUrl}')" title="播放/暂停">▶</button>
          <div class="vp-info">
            <div class="vp-time-row">
              <span class="vp-time-cur">0″</span>
              <span class="vp-time-sep">/</span>
              <span class="vp-time-dur">${durText||'?'}</span>
            </div>
            <div class="vp-bar-track" onclick="VoicePlayer.seek(${m.id},event)" title="拖动调整进度">
              <div class="vp-bar-fill" style="width:0%"></div>
            </div>
          </div>
          <div class="vp-speed-row">
            <button class="vp-speed-btn" data-rate="1" onclick="event.stopPropagation();VoicePlayer.setRate(${m.id},1)" title="1倍速">1×</button>
            <button class="vp-speed-btn active" data-rate="2" onclick="event.stopPropagation();VoicePlayer.setRate(${m.id},2)" title="2倍速">2×</button>
            <button class="vp-speed-btn" data-rate="3" onclick="event.stopPropagation();VoicePlayer.setRate(${m.id},3)" title="3倍速">3×</button>
          </div>
          <button class="vp-trans-btn" id="vp-trans-btn-${m.id}" onclick="transcribeVoice(${m.id},'${voiceUrl}')" title="语音转文字">T</button>
          <a href="${voiceUrl}" download class="vp-dl-btn" title="下载语音">⬇</a>
        </div>
        <div class="vp-trans-result" id="vp-result-${m.id}" style="display:none"></div>`;
      } else {
        body = `<div class="nontext-placeholder voice-plc"><span>🔊</span><span>语音${durText ? ' ' + durText : ''}</span><div style="color:#484f58;font-size:10px;margin-top:4px">音频文件未找到</div></div>`;
      }
      break;
    }
    case 43: {
      const vmi = m.media_info;
      const vDur = (vmi && vmi.duration) || p.duration || 0;
      let vDurText = '';
      if (vDur > 0) {
        vDurText = `${Math.floor(vDur/60)}:${String(vDur%60).padStart(2,'0')}`;
      }
      let vDimsText = '';
      if (vmi && vmi.width && vmi.height) {
        vDimsText = `${vmi.width}×${vmi.height}`;
      }
      const vName = (vmi && vmi.file_name) || '';
      let vSize = '';
      if (vmi && vmi.file_size && vmi.file_size > 0) {
        vSize = vmi.file_size >= 1048576 ? `${(vmi.file_size/1048576).toFixed(1)}MB` : `${(vmi.file_size/1024).toFixed(0)}KB`;
      }
      const videoUrl = api.hardlinkMediaUrl(vmi, 43);
      const vidMetaHtml = `<div style="padding:6px 10px;display:flex;justify-content:space-between;align-items:center;font-size:11px"><span style="color:#c9d1d9">${vName ? escapeHtml(vName) : '视频'}</span><span>${vDimsText ? `<span style="color:#58a6ff;margin-left:8px">${vDimsText}</span>` : ''}${vDurText ? `<span style="color:#e94560;margin-left:8px">▶ ${vDurText}</span>` : ''}${vSize ? `<span style="color:#484f58;margin-left:8px">${vSize}</span>` : ''}</span></div>`;
      if (videoUrl) {
        body = `<div style="background:#0d1117;border:1px solid #30363d;border-radius:4px;overflow:hidden"><video src="${videoUrl}" controls preload="metadata" style="width:100%;max-height:400px;background:#000;display:block" onerror="this.style.display='none';this.nextElementSibling.style.display='block'" playsinline></video><div style="display:none;background:#161b22;padding:24px 20px;text-align:center;font-size:36px">🎬</div>${vidMetaHtml}<div style="padding:6px 10px;border-top:1px solid #21262d;display:flex;gap:12px;font-size:11px"><a href="${videoUrl}" download target="_blank" style="color:#58a6ff;text-decoration:none" onclick="event.stopPropagation()">⬇ 下载视频</a></div></div>`;
      } else {
        body = `<div style="background:#0d1117;border:1px solid #30363d;border-radius:4px;overflow:hidden"><div style="background:#161b22;padding:20px;text-align:center;font-size:32px">🎬</div>${vidMetaHtml}<div style="padding:6px 10px;border-top:1px solid #21262d;color:#484f58;font-size:11px">视频文件未找到</div></div>`;
      }
      break;
    }
    case 48:
      if (p.lat && p.lng) {
        // WeChat stores location in GCJ-02 (Mars coordinate system).
        // Amap (高德) uses GCJ-02 natively — no conversion needed.
        // Baidu Maps (百度) uses BD-09 — must convert from GCJ-02.
        const locName = encodeURIComponent(p.poiname || p.label || '');
        const amapUrl = `https://uri.amap.com/marker?position=${p.lng},${p.lat}&name=${locName}&callnative=1`;
        const bd = gcj02ToBd09(p.lng, p.lat);
        const baiduUrl = `https://api.map.baidu.com/marker?location=${bd.lat},${bd.lng}&title=${locName}&content=${locName}&output=html`;
        body = `<div class="bubble-location"><div class="map-placeholder">📍</div><div class="addr-info"><div style="color:#c9d1d9;font-weight:600">${escapeHtml(p.poiname||p.label||'位置')}</div><div style="color:#8b949e;font-size:11px">${escapeHtml(p.label||'')}</div><div style="color:#484f58;font-size:10px">${p.lat.toFixed(6)}, ${p.lng.toFixed(6)}</div></div></div><div style="padding:6px 10px;display:flex;gap:8px;font-size:11px"><a href="${amapUrl}" target="_blank" rel="noopener" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:6px 12px;color:#58a6ff;text-decoration:none;flex:1;text-align:center;cursor:pointer" onclick="event.stopPropagation()">🗺 高德地图</a><a href="${baiduUrl}" target="_blank" rel="noopener" style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:6px 12px;color:#f0883e;text-decoration:none;flex:1;text-align:center;cursor:pointer" onclick="event.stopPropagation()">📍 百度地图</a></div>`;
      } else {
        body = `<div class="nontext-placeholder location-plc"><span>📍</span><span>位置</span></div>`;
      }
      break;
    case 49: {
      const rt = p.render_type || '';
      if (rt === 'pat') {
        body = `<div class="bubble-system"><span>${escapeHtml(p.text || '[拍一拍]')}</span></div>`;
      } else if (rt === 'chat_history' || rt === 'forward') {
        const records = p.records || [];
        const isForward = rt === 'forward';
        const headerIcon = isForward ? '📋' : '📋';
        const headerLabel = isForward ? '转发消息' : '聊天记录';
        const renderRecordItem = (r) => {
          let recBody = '';
          const dt = r.datatype || 0;
          if (dt === 1) {
            recBody = `<div style="color:#c9d1d9;font-size:12px;line-height:1.5;white-space:pre-wrap">${escapeHtml(r.datadesc||'')}</div>`;
          } else if (dt === 2) {
            const imgMi = { md5: r.fullmd5 || '', file_name: '', media_type: 3 };
            const imgUrl = api.hardlinkMediaUrl(imgMi) || '';
            if (imgUrl) {
              recBody = `<img src="${imgUrl}" style="max-width:200px;max-height:150px;border-radius:4px;margin:4px 0" onerror="this.style.display='none';this.nextElementSibling.style.display=''" loading="lazy" alt="" /><div style="color:#8b949e;font-size:10px;display:none">🖼 ${escapeHtml(r.datafmt||'图片')}${r.thumbsize?' '+formatSize(r.thumbsize):''}</div>`;
            } else {
              const fmt = r.datafmt ? r.datafmt.toUpperCase() : '';
              const sz = r.datasize || r.thumbsize;
              const szStr = sz ? formatSize(sz) : '';
              recBody = `<div style="color:#8b949e;font-size:11px">🖼 ${fmt||'图片'} ${szStr}${r.fullmd5?' ('+r.fullmd5.substring(0,8)+'...)':''}</div>`;
            }
          } else if (dt === 4 || dt === 5) {
            const vfmt = r.datafmt || '';
            const vdur = r.duration ? (r.duration >= 60 ? `${Math.floor(r.duration/60)}′${r.duration%60}″` : `${r.duration}″`) : '';
            const vsz = r.datasize ? formatSize(r.datasize) : '';
            recBody = `<div style="color:#c9d1d9;font-size:12px">🎬 ${escapeHtml(r.datatitle||vfmt||'视频')}${vdur?' '+vdur:''}${vsz?' '+vsz:''}</div>`;
          } else if (dt === 6) {
            const fsz = r.datasize ? formatSize(r.datasize) : '';
            recBody = `<div style="color:#c9d1d9;font-size:12px">📎 ${escapeHtml(r.datatitle||r.datafmt||'文件')}${fsz?' '+fsz:''}</div>`;
          } else if (dt === 8) {
            const fsz = r.datasize ? formatSize(r.datasize) : '';
            recBody = `<div style="color:#c9d1d9;font-size:12px">📄 ${escapeHtml(r.datatitle||r.datafmt||'文件')}${fsz?' '+fsz:''}</div>`;
          } else if (dt === 17) {
            recBody = `<div style="color:#c9d1d9;font-size:12px">📍 ${escapeHtml(r.datadesc||r.datatitle||'位置共享')}</div>`;
          } else if (dt === 19) {
            recBody = `<div style="color:#f0883e;font-size:11px">📋 ${escapeHtml(r.datadesc||'聊天记录')}</div>`;
          } else if (dt === 22 || dt === 36 || dt === 37) {
            recBody = `<div style="color:#8b949e;font-size:11px">${escapeHtml(r.datadesc||r.datatitle||'[消息]')}</div>`;
          } else {
            const desc = r.datadesc || r.datatitle || '';
            if (desc) {
              recBody = `<div style="color:#c9d1d9;font-size:12px;line-height:1.5;white-space:pre-wrap">${escapeHtml(desc)}</div>`;
            } else {
              recBody = `<div style="color:#484f58;font-size:11px">消息</div>`;
            }
          }
          let nestedHtml = '';
          if (r.nested_records && r.nested_records.length) {
            nestedHtml = `<div style="margin:4px 0 0 0;padding:4px 0 0 8px;border-left:2px solid #30363d">${r.nested_records.map(renderRecordItem).join('')}</div>`;
          }
          return `<div style="display:flex;gap:8px;padding:6px 10px;border-bottom:1px solid #21262d;align-items:flex-start">
            <img src="${escapeAttr(r.sourceheadurl||'')}" style="width:28px;height:28px;border-radius:4px;flex-shrink:0" onerror="this.style.display='none'" loading="lazy" alt="" />
            <div style="flex:1;min-width:0">
              <div style="display:flex;gap:6px;align-items:baseline;margin-bottom:2px">
                <span style="color:#58a6ff;font-size:11px;font-weight:600">${escapeHtml(r.sourcename||'未知')}</span>
                <span style="color:#484f58;font-size:10px">${escapeHtml(r.sourcetime||'')}</span>
              </div>
              ${recBody}${nestedHtml}
            </div>
          </div>`;
        };
        let recsHtml = records.length ? records.map(renderRecordItem).join('') : `<div style="padding:8px 10px;color:#8b949e;font-size:11px">无法解析消息内容</div>`;
        body = `<div class="bubble-link" style="cursor:default">
          <div style="padding:8px 12px;border-bottom:1px solid #21262d;display:flex;align-items:center;gap:8px">
            <span style="font-size:16px">${headerIcon}</span>
            <div style="flex:1"><div style="color:#c9d1d9;font-weight:600;font-size:13px">${escapeHtml(p.title||headerLabel)}</div></div>
            ${records.length?`<span style="color:#8b949e;font-size:10px">${records.length}条消息</span>`:''}
          </div>
          <div style="max-height:400px;overflow-y:auto">${recsHtml}</div>
        </div>`;
      } else if (rt === 'mini_program') {
        const mpThumb = p.thumburl || '';
        const miniUrl = p.url || '';
        const isMiniWeb = miniUrl && (miniUrl.startsWith('http://') || miniUrl.startsWith('https://') || miniUrl.startsWith('//'));
        const miniTarget = isMiniWeb ? (miniUrl.startsWith('//') ? 'https:' + miniUrl : miniUrl) : '';
        const miniTag = isMiniWeb ? `<a href="${escapeAttr(miniTarget)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">` : '';
        const miniEnd = isMiniWeb ? '</a>' : '';
        body = `<div class="bubble-link" onclick="event.stopPropagation()">${miniTag}
          <div style="display:flex;align-items:center;gap:10px;padding:10px 12px">
            <div style="width:44px;height:44px;border-radius:10px;background:linear-gradient(135deg,#56d364,#569cd6);display:flex;align-items:center;justify-content:center;font-size:22px;flex-shrink:0">🧩</div>
            <div style="flex:1;min-width:0">
              <div style="color:#c9d1d9;font-weight:600;font-size:13px">${escapeHtml(p.title||'小程序')}</div>
              ${p.des?`<div style="color:#8b949e;font-size:11px;margin-top:2px">${escapeHtml(p.des)}</div>`:''}
              <div style="margin-top:4px"><span style="background:rgba(86,211,100,0.13);color:#56d364;padding:1px 6px;border-radius:3px;font-size:10px">🧩 小程序</span>${p.appname?`<span style="color:#8b949e;font-size:10px;margin-left:6px">${escapeHtml(p.appname)}</span>`:''}</div>
            </div>
          </div>${miniEnd}</div>`;
      } else if (rt === 'finder') {
        const finderThumb = p.thumburl || p.coverurl || '';
        const finderUrl = p.url || '';
        const isFinderWeb = finderUrl && (finderUrl.startsWith('http://') || finderUrl.startsWith('https://') || finderUrl.startsWith('//'));
        const finderTarget = isFinderWeb ? (finderUrl.startsWith('//') ? 'https:' + finderUrl : finderUrl) : '';
        const finderTag = isFinderWeb ? `<a href="${escapeAttr(finderTarget)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none;display:block">` : '';
        const finderEnd = isFinderWeb ? '</a>' : '';
        body = `<div class="bubble-link" onclick="event.stopPropagation()">${finderTag}
          ${finderThumb?`<img src="${escapeAttr(finderThumb)}" style="width:100%;max-height:180px;object-fit:cover;border-radius:8px 8px 0 0" onerror="this.style.display='none'" loading="lazy" alt="" />`:''}
          <div style="padding:10px 12px">
            <div style="color:#c9d1d9;font-weight:600;font-size:13px">🎬 ${escapeHtml(p.title||'视频号')}</div>
            ${p.des?`<div style="color:#8b949e;font-size:11px;margin-top:2px">${escapeHtml(p.des)}</div>`:''}
            <div style="margin-top:4px;display:flex;align-items:center;gap:6px">
              <span style="background:rgba(233,69,96,0.13);color:#e94560;padding:1px 6px;border-radius:3px;font-size:10px">🎬 视频号</span>
              ${p.appname?`<span style="color:#8b949e;font-size:10px">@${escapeHtml(p.appname)}</span>`:''}
            </div>
          </div>${finderEnd}</div>`;
      } else if (rt === 'file') {
        const fileSize = p.size ? (p.size >= 1048576 ? `${(p.size/1048576).toFixed(1)} MB` : `${(p.size/1024).toFixed(0)} KB`) : '';
        const fileUrl = api.hardlinkMediaUrl(m.media_info, 6) || p.file_path || '';
        const canDownload = fileUrl && (fileUrl.startsWith('/api/') || fileUrl.startsWith('http'));
        if (canDownload) {
          const isExternal = fileUrl.startsWith('http');
          body = `<a href="${fileUrl}" ${isExternal ? 'target="_blank" rel="noopener"' : 'download'} class="nontext-placeholder file-plc" style="text-decoration:none;color:inherit;cursor:pointer;display:block" onclick="event.stopPropagation()"><span>📎</span><span>${escapeHtml(p.title||'文件')}</span>${fileSize?`<div style="color:#484f58;font-size:10px;margin-top:4px">${fileSize}</div>`:''}${isExternal?`<div style="color:#58a6ff;font-size:10px;margin-top:2px">⬇ 点击下载 (外部链接)</div>`:''}</a>`;
        } else {
          body = `<div class="nontext-placeholder file-plc"><span>📎</span><span>${escapeHtml(p.title||'文件')}</span>${fileSize?`<div style="color:#484f58;font-size:10px;margin-top:4px">${fileSize}</div>`:''}<div style="color:#484f58;font-size:10px;margin-top:4px">文件未找到</div></div>`;
        }
      } else if (rt === 'quote') {
        body = `<div class="bubble-link"><div class="link-title">💬 ${escapeHtml(p.title||'引用消息')}</div>${p.quote_content?`<div style="margin:4px 10px;padding:6px 8px;background:#0d1117;border-left:2px solid #569cd6;border-radius:0 4px 4px 0;font-size:11px;color:#8b949e">${escapeHtml(p.quote_content)}</div>`:''}<div style="padding:0 12px 8px;font-size:10px"><span style="background:rgba(86,156,214,0.13);color:#569cd6;padding:1px 8px;border-radius:3px">💬 引用</span></div></div>`;
      } else if (rt === 'location_share') {
        body = `<div class="nontext-placeholder location-plc"><span>📍</span><span>${escapeHtml(p.text||p.title||'位置共享')}</span></div>`;
      } else if (p.is_forward) {
        body = `<div class="bubble-link"><div class="link-title">📋 ${escapeHtml(p.title||'转发')}</div>${p.des?`<div class="link-desc">${escapeHtml(p.des)}</div>`:''}<div style="padding:0 12px 8px;display:flex;gap:8px;align-items:center;font-size:10px"><span style="background:rgba(255,213,79,0.13);color:#ffd54f;padding:1px 8px;border-radius:3px">📎 转发</span>${p.forward_msg_count?`<span style="color:#8b949e">${p.forward_msg_count}条消息</span>`:''}</div></div>`;
      } else if (p.title || p.url) {
        const safeUrl = p.url || '';
        const isSafelink = safeUrl && (safeUrl.startsWith('http://') || safeUrl.startsWith('https://') || safeUrl.startsWith('//'));
        const linkTarget = isSafelink ? (safeUrl.startsWith('//') ? 'https:' + safeUrl : safeUrl) : '';
        const linkTag = isSafelink ? `<a href="${escapeAttr(linkTarget)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">` : '';
        const linkEnd = isSafelink ? '</a>' : '';
        body = `<div class="bubble-link" onclick="event.stopPropagation()">${linkTag}<div class="link-title">${escapeHtml(p.title||'链接')}</div><div class="link-desc">${escapeHtml(p.des||'')}</div><div style="padding:0 12px 8px;display:flex;gap:8px;align-items:center;font-size:10px"><span style="background:rgba(255,213,79,0.13);color:#ffd54f;padding:1px 8px;border-radius:3px">🔗 链接</span>${p.appname?`<span style="color:#8b949e">${escapeHtml(p.appname)}</span>`:''}</div>${linkEnd}</div>`;
      } else if (m.content && !m.content.startsWith('<')) {
        body = `<div class="nontext-placeholder link-plc"><span>🔗</span><span>${escapeHtml(m.content.substring(0,200))}</span></div>`;
      } else {
        body = `<div class="nontext-placeholder link-plc"><span>🔗</span><span>链接/应用消息</span></div>`;
      }
      break;
    }
    case 42: {
      const cardAvatar = p.bigheadimgurl || p.smallheadimgurl || '';
      const cardAvatarHtml = cardAvatar
        ? `<img src="${escapeAttr(cardAvatar)}" style="width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0" onerror="this.style.display='none'" loading="lazy" alt="" />`
        : '';
      const fallbackAvatar = cardAvatarHtml ? '' : '<div style="width:48px;height:48px;background:#30363d;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0">👤</div>';
      if (p.nickname || p.username) {
        let detailLines = '';
        if (p.alias) detailLines += `<div style="color:#8b949e;font-size:11px">微信号: ${escapeHtml(p.alias)}</div>`;
        else if (p.username && !p.username.startsWith('v3_')) detailLines += `<div style="color:#484f58;font-size:10px">${escapeHtml(p.username)}</div>`;
        if (p.province || p.city) detailLines += `<div style="color:#484f58;font-size:10px">${[p.province,p.city].filter(Boolean).join(' ')}</div>`;
        if (p.sign) detailLines += `<div style="color:#6e7681;font-size:10px;margin-top:2px;font-style:italic">${escapeHtml(p.sign)}</div>`;
        body = `<div class="bubble-contact">${cardAvatarHtml}${fallbackAvatar}<div><div style="color:#c9d1d9;font-weight:600">${escapeHtml(p.nickname||'联系人')}</div>${detailLines}</div></div>`;
      } else {
        body = `<div class="nontext-placeholder card-plc"><span>👤</span><span>名片</span></div>`;
      }
      break;
    }
    case 47: {
      const emojiThumb = p.thumb_url || p.emoji_url || '';
      const emojiMd5 = p.md5 || '';
      if (emojiThumb) {
        body = `<div style="text-align:center"><img src="${escapeAttr(emojiThumb)}" style="max-width:120px;max-height:120px;border-radius:4px" onerror="this.style.display='none'" loading="lazy" alt="表情" />${emojiMd5 ? `<div style="color:#484f58;font-size:9px;margin-top:2px">MD5: ${escapeHtml(emojiMd5.substring(0,16))}...</div>` : ''}</div>`;
      } else if (emojiMd5) {
        body = `<div class="nontext-placeholder emoji-plc"><span>😊</span><span>表情</span><div style="color:#484f58;font-size:10px;margin-top:4px">MD5: ${escapeHtml(emojiMd5.substring(0,16))}...</div></div>`;
      } else {
        body = `<div class="nontext-placeholder emoji-plc"><span>😊</span><span>表情</span></div>`;
      }
      break;
    }
    case 6: {
      const fmi = m.media_info;
      const fTitle = p.title || (fmi && fmi.file_name) || '';
      const fSize = p.size || (fmi && fmi.file_size) || 0;
      let fSizeStr = '';
      if (fSize > 0) {
        fSizeStr = fSize >= 1048576 ? `${(fSize/1048576).toFixed(1)} MB` : `${(fSize/1024).toFixed(0)} KB`;
      }
      const fileUrl = api.hardlinkMediaUrl(fmi, 6);
      if (fTitle && fileUrl) {
        body = `<a href="${fileUrl}" download target="_blank" class="nontext-placeholder file-plc" style="text-decoration:none;color:inherit;cursor:pointer;display:block" onclick="event.stopPropagation()"><span>📎</span><span>${escapeHtml(fTitle)}</span>${fSizeStr ? `<div style="color:#484f58;font-size:10px;margin-top:4px">${fSizeStr}</div>` : ''}</a>`;
      } else if (fTitle) {
        body = `<div class="nontext-placeholder file-plc"><span>📎</span><span>${escapeHtml(fTitle)}</span>${fSizeStr ? `<div style="color:#484f58;font-size:10px;margin-top:4px">${fSizeStr}</div>` : ''}<div style="color:#484f58;font-size:10px;margin-top:4px">文件未找到</div></div>`;
      } else {
        body = `<div class="nontext-placeholder file-plc"><span>📎</span><span>文件${fSizeStr ? ' ' + fSizeStr : ''}</span></div>`;
      }
      break;
    }
    case 50: {
      const callMsg = p.call_msg || '';
      const callDur = p.duration || 0;
      let callDurStr = '';
      if (callDur > 0) {
        callDurStr = `${Math.floor(callDur/60)}:${String(callDur%60).padStart(2,'0')}`;
      }
      if (callMsg) {
        body = `<div class="nontext-placeholder call-plc"><span>📞</span><span>${escapeHtml(callMsg)}</span>${callDurStr ? `<div style="color:#484f58;font-size:10px;margin-top:4px">通话时长 ${callDurStr}</div>` : ''}</div>`;
      } else if (callDur > 0) {
        body = `<div class="nontext-placeholder call-plc"><span>📞</span><span>通话时长 ${callDurStr}</span></div>`;
      } else {
        body = `<div class="nontext-placeholder call-plc"><span>📞</span><span>[网络电话]</span></div>`;
      }
      break;
    }
    case 10000: case 10002:
      body = `<div class="bubble-system"><span>${_sysMsgIcon(p.text || m.content || '') + escapeHtml(p.text || m.content || '')}</span></div>`;
      break;
    default:
      body = `<div style="white-space:pre-wrap;color:#8b949e">${escapeHtml(m.content || '')}</div>`;
    }
    return `<div class="msg-bubble-detail ${cls}">${body}</div>`;
  }
}
