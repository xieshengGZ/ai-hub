/* AI 中枢 · 前端 */
let AGENTS = [], CUR = null, LAST_ID = 0, JOB = null, MODE = localStorage.getItem('mode') || 'full';
let POLL = null, STAGED = [], RENDERED = new Set();
let CHAT_GEN = 0;   // 切换会话即递增；迟到的响应据此丢弃，避免画进别的会话

const $ = (id) => document.getElementById(id);
const esc = (s) => (s || '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const escAttr = esc;

function hhmm(ts){
  if(!ts) return '';
  const d = new Date(ts * 1000), n = new Date();
  const same = d.toDateString() === n.toDateString();
  const p = (x) => String(x).padStart(2, '0');
  return same ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${p(d.getMonth()+1)}-${p(d.getDate())}`;
}

async function api(path, opt){
  const r = await fetch(path, Object.assign({credentials:'same-origin'}, opt||{}));
  if(r.status === 401){ show('s-login'); throw new Error('401'); }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if(!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
  return data;
}

const DESK = () => window.matchMedia('(min-width:900px)').matches;
let SCREEN = 's-list';

function show(id){
  document.body.classList.toggle('login-mode', id === 's-login');
  ['s-login','s-list','s-chat','s-new'].forEach(s=>{
    const el = $(s);
    let on = (s === id);
    if(DESK() && id !== 's-login' && id !== 's-new' && (s === 's-list' || s === 's-chat')) on = true;
    if(DESK() && s === 's-new') on = (id === 's-new');
    el.classList.toggle('hide', !on);
    el.style.display = on ? 'flex' : 'none';
  });
  if(DESK() && !CUR && id !== 's-new' && id !== 's-login') placeholder();
  if(id === 's-new' && SCREEN !== 's-new'){ try{ history.pushState({neu: 1}, '', '#new'); }catch(e){} }
  SCREEN = id;
  syncTabIndex(id);
  if(id === 's-login') nxBootRun();
}

function placeholder(){
  $('chatName').textContent = '会话';
  $('chatSub').textContent = '从左侧选一个 agent';
  $('chat').innerHTML = '<div class="empty">选一个 agent 开始</div>';
  JOB = null;
}

/* ---------- 登录 ---------- */
let NX_DONE = false;
function nxBootRun(){
  const box = $('nxBoot'); if(!box || NX_DONE) return;
  NX_DONE = true;
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const lines = [
    ['> 初始化核心 ', 'OK'],
    ['> 载入 agent 注册表 ', 'OK'],
    ['> 建立安全通道 ', 'OK'],
    ['> 等待身份验证', '']
  ];
  if(reduce){
    box.innerHTML = lines.map(l => '<div class="dim">' + esc(l[0]) +
      (l[1] ? ' <span class="ok">' + l[1] + '</span>' : '') + '</div>').join('');
    return;
  }
  let li = 0, ci = 0; const out = [];
  const tick = () => {
    if(li >= lines.length){
      if(out.length) out[out.length - 1] = out[out.length - 1].replace('</div>', '<span class="cur"></span></div>');
      box.innerHTML = out.join('');
      return;
    }
    const [text, tail] = lines[li];
    ci++;
    box.innerHTML = out.join('') + '<div>' + esc(text.slice(0, ci)) + '<span class="cur"></span></div>';
    if(ci >= text.length){
      out.push('<div>' + esc(text) + (tail ? ' <span class="ok">' + tail + '</span>' : '') + '</div>');
      li++; ci = 0;
      setTimeout(tick, 240);
    }else setTimeout(tick, 16);
  };
  tick();
}

async function loginPrefetch(){
  try{
    const r = await fetch('api/authinfo', {credentials:'same-origin'});
    const d = await r.json();
    if(d && d.totp){ const w = $('otpWrap'); if(w) w.style.display = ''; }
  }catch(e){}
}

async function login(){
  const pwd = $('pwd').value;
  const codeEl = $('otpCode');
  const code = codeEl ? codeEl.value.trim() : '';
  $('loginErr').textContent = '';
  try{
    await api('api/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd, code})});
    document.body.classList.remove('login-mode');
    boot();
  }catch(e){
    if(e.message === '需要动态验证码'){
      const w = $('otpWrap');
      if(w){ w.style.display = ''; if(codeEl) codeEl.focus(); }
      $('loginErr').textContent = code ? '动态码不对，请重试' : '请输入认证器上的 6 位动态码';
      return;
    }
    $('loginErr').textContent = e.message === '401' ? '口令不对，请重试' : e.message;
    const nx = document.querySelector('.nx');
    if(nx){ nx.classList.add('shake'); setTimeout(() => nx.classList.remove('shake'), 420); }
  }
}

/* ---------- 安全 / 两步验证 ---------- */
async function loadSec(){
  const box = $('secBody'); if(!box) return;
  let st = {enabled:false, backup_left:0};
  try{ st = await api('api/totp/state'); }catch(e){}
  const lbl = $('secLabel'); if(lbl) lbl.textContent = st.enabled ? '已开启' : '未开启';
  if(st.enabled){
    box.innerHTML = `<div class="hint">两步验证已开启，备用码剩余 <b>${st.backup_left}</b> 个。</div>
      <div style="padding:8px 16px;display:flex;flex-direction:column;gap:8px">
        <input id="secPwd" type="password" class="mdin" placeholder="输入登录口令以关闭两步验证">
        <button class="pick" onclick="disableTotp()">关闭两步验证</button>
      </div>`;
    return;
  }
  box.innerHTML = `<div style="padding:8px 16px"><button class="pick" style="width:100%" onclick="startTotp()">开启两步验证</button></div>
    <div id="totpSetup"></div>`;
}
async function startTotp(){
  try{
    const d = await api('api/totp/setup', {method:'POST'});
    const box = $('totpSetup');
    box.innerHTML = `<div class="hint">1）把下面这串密钥加到认证器（Google Authenticator / 1Password 等）：</div>
      <div style="padding:6px 16px"><code style="font-size:13px;word-break:break-all">${esc(d.secret)}</code></div>
      <div class="hint" style="padding:2px 16px">也可以直接点这个链接（手机打开）：<br>
        <span style="word-break:break-all;font-size:11px;color:#0F5B63">${esc(d.uri)}</span></div>
      <div class="hint">2）输入认证器显示的 6 位动态码完成绑定：</div>
      <div style="padding:6px 16px;display:flex;gap:8px">
        <input id="totpCode" class="mdin" inputmode="numeric" maxlength="6" placeholder="6 位动态码">
        <button class="pick" style="flex:none;padding:10px 16px" onclick="enableTotp()">确认开启</button>
      </div>`;
  }catch(e){ alert(e.message); }
}
async function enableTotp(){
  try{
    const d = await api('api/totp/enable', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({code: $('totpCode').value.trim()})});
    const box = $('totpSetup');
    box.innerHTML = `<div class="hint">已开启。下面这些<strong>备用码只显示这一次</strong>，请立刻保存（每张只能用一次）：</div>
      <div style="padding:6px 16px;font-family:ui-monospace,monospace;font-size:13px;line-height:1.9">
        ${(d.backup_codes || []).map(c => esc(c)).join('<br>')}
      </div>
      <div style="padding:8px 16px"><button class="pick" onclick="loadSec()">完成</button></div>`;
    toast('两步验证已开启');
  }catch(e){ alert(e.message); }
}
async function disableTotp(){
  try{
    await api('api/totp/disable', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: $('secPwd').value})});
    toast('已关闭两步验证'); loadSec();
  }catch(e){ alert(e.message); }
}
function logout(){ api('api/logout', {method:'POST'}).finally(()=>{ location.reload(); }); }

/* ---------- 列表 ---------- */
async function boot(){
  CHAT_CACHE.clear();   // 内存缓存不跨启动沿用，避免旧版本留下的脏数据
  try{
    const d = await api('api/bootstrap');
    AGENTS = d.agents || [];
    renderList();
    loadModels();
    show('s-list');
    if(DESK() && !CUR){ placeholder(); }
    const h = location.hash.replace('#','');
    if(h && AGENTS.some(a=>a.id===h)) openAgent(h);
  }catch(e){ if(e.message !== '401') console.error(e); }
}

function renderList(){
  const row = (a) => {
    const state = a.state === 'busy' ? 'busy' : (a.state === 'on' ? 'on' : '');
    let tag = a.id === 'main' ? '<span class="tag">调度</span>' : '';
    if(a.pinned) tag += '<span class="tag">置顶</span>';
    if(a.always_on) tag += '<span class="tag on">常驻</span>';
    return `<div class="row" onclick="openAgent('${a.id}')">
      <div class="body">
        <div class="top"><span class="sdot ${state}"></span><span class="name">${esc(a.name)}</span>${tag}
          <span class="time">${a.ts?hhmm(a.ts):''}</span></div>
        <div class="last">${esc(a.last || (a.state==='off'?'未运行':'在线'))}</div>
      </div>
      ${a.unread ? `<span class="unread">${a.unread}</span>` : ''}
    </div>`;
  };
  const pinned = AGENTS.filter(a => a.pinned).sort((x, y) => y.pinned - x.pinned);
  const hosts = {};
  AGENTS.filter(a => !a.pinned).forEach(a => { (hosts[a.host] = hosts[a.host] || []).push(a); });
  let html = '';
  if(pinned.length) html += '<div class="grouphd">置顶</div>' + pinned.map(row).join('');
  Object.keys(hosts).forEach(h => {
    html += `<div class="grouphd">${esc(h)}</div>` + hosts[h].map(row).join('');
  });
  $('agentList').innerHTML = html || '<div class="hint">还没有 agent，先在中枢里创建。</div>';
}

async function refreshList(){
  try{ const d = await api('api/bootstrap'); AGENTS = d.agents || []; renderList(); }catch(e){}
}

/* 服务端推变更：一台设备删了 agent，另一台立刻知道 */
let LAST_REV = -1, ES = null;
function startEvents(){
  if(!('EventSource' in window)) return;
  try{ ES = new EventSource('api/events'); }catch(e){ return; }
  ES.onmessage = (e) => {
    const rev = parseInt(e.data, 10);
    if(isNaN(rev) || rev === LAST_REV) return;
    LAST_REV = rev;
    if(!$('s-list').classList.contains('hide') || DESK()) refreshList();
    if(CUR && !$('s-chat').classList.contains('hide')) pull(false).catch(err => {
      if(String(err.message).indexOf('没有这个 agent') >= 0) back();
    });
  };
}

/* 不再定时轮询：进入拉一次，回到前台拉一次，其余靠手动 */
document.addEventListener('visibilitychange', () => {
  if(document.hidden) return;
  if(!$('s-list').classList.contains('hide')) refreshList();
  if(CUR && !$('s-chat').classList.contains('hide')) pull(false).catch(()=>{});
});

/* 下拉刷新 */
(function(){
  let startY = null, pulling = false;
  const chat = () => $('chat');
  document.addEventListener('touchstart', e => {
    if($('s-chat').classList.contains('hide') || !CUR) return;
    const c = chat();
    if(c.scrollTop <= 0) startY = e.touches[0].clientY;
  }, {passive:true});
  document.addEventListener('touchmove', e => {
    if(startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if(dy > 60 && !pulling){ pulling = true; toast('松手刷新'); }
  }, {passive:true});
  document.addEventListener('touchend', () => {
    if(pulling){ pulling = false; refreshChat(); }
    startY = null;
  });
})();

/* ---------- 对话 ---------- */
async function openAgent(id){
  const known = AGENTS.find(a => a.id === id);
  if(!known){
    CHAT_GEN++;
    CUR = null;
    try{ history.replaceState(null, '', location.pathname); }catch(e){}
    show('s-list');
    await refreshList();
    toast('这个会话已经不存在了，已回到列表');
    return;
  }
  CHAT_GEN++;
  const gen = CHAT_GEN;
  clearTimers();
  CUR = known;
  LAST_ID = 0; RENDERED.clear(); JOB = null; window.STICK = true; WIN = 50; HAS_MORE = false; FIRST_ID = 0;
  $('chatName').textContent = CUR.name;
  $('chatSub').textContent = '加载中…';
  $('chat').innerHTML = '';
  show('s-chat');
  document.body.classList.remove('login-mode');
  const cached = CHAT_CACHE.get(id);
  if(cached && cached.msgs.size){
    /* 先秒开：用内存里的缓存渲染，再去服务端补增量 */
    WIN = cached.win || 50; HAS_MORE = !!cached.hasMore;
    FIRST_ID = cached.firstId; LAST_ID = cached.lastId;
    cacheSorted(cached).forEach(m => draw(m));
    renderLoadMore();
    $('chat').scrollTop = $('chat').scrollHeight;
    window.STICK = true;
    try{ await pull(false); }catch(e){ if(gen === CHAT_GEN) $('chatSub').textContent = '同步失败：' + e.message; }
  }else{
    try{ await pull(true); }catch(e){ if(gen === CHAT_GEN) $('chatSub').textContent = '加载失败：' + e.message; }
  }
  if(gen !== CHAT_GEN) return;
  loadSkillCache(); loadFileCache();
  try{ history.pushState({chat: id}, '', '#' + id); }catch(e){}
}

let WIN = 50, HAS_MORE = false, FIRST_ID = 0;
const CHAT_CACHE = new Map();   // agent -> {msgs: Map(id->msg), lastId, firstId, hasMore, win}
function cacheOf(id){
  let c = CHAT_CACHE.get(id);
  if(!c){ c = { msgs: new Map(), lastId: 0, firstId: 0, hasMore: false, win: 50 }; CHAT_CACHE.set(id, c); }
  return c;
}
function cachePut(c, m){
  c.msgs.set(m.id, m);
  if(m.id > c.lastId) c.lastId = m.id;
  if(!c.firstId || m.id < c.firstId) c.firstId = m.id;
}
function cacheSorted(c){ return [...c.msgs.values()].sort((a, b) => a.id - b.id); }
function cacheRebuild(agent, msgs, hasMore, win){
  const c = cacheOf(agent);
  c.msgs = new Map(); c.lastId = 0; c.firstId = 0;
  (msgs || []).forEach(m => cachePut(c, m));
  c.hasMore = !!hasMore; c.win = win;
  return c;
}
async function pull(reset, keepAnchor){
  if(!CUR) return;
  const aid = CUR.id, gen = CHAT_GEN;
  let d;
  if(reset){
    d = await api(`api/msg?agent=${encodeURIComponent(aid)}&limit=${WIN}`);
    if(gen !== CHAT_GEN || !CUR || CUR.id !== aid) return;
    if(d.agent && d.agent.id !== aid) return;
    const box = $('chat');
    const keepTop = keepAnchor ? (firstVisibleId() || FIRST_ID) : 0;
    window.STICK = !keepAnchor;
    box.innerHTML = '';
    RENDERED.clear();
    LAST_ID = 0; FIRST_ID = 0;
    (d.messages || []).forEach(m => { LAST_ID = Math.max(LAST_ID, m.id); if(!FIRST_ID) FIRST_ID = m.id; draw(m); });
    HAS_MORE = !!d.has_more;
    const cc = cacheRebuild(aid, d.messages || [], HAS_MORE, WIN);
    renderLoadMore();
    if(keepTop){
      const el = box.querySelector(`[data-id="${keepTop}"]`);
      if(el) box.scrollTop = el.offsetTop - 8;
      window.STICK = nearBottom();
    }else{
      box.scrollTop = box.scrollHeight;
      window.STICK = true;
    }
  }else{
    const since = Math.max(0, LAST_ID - 1);
    d = await api(`api/msg?agent=${encodeURIComponent(aid)}&after=${since}`);
    if(gen !== CHAT_GEN || !CUR || CUR.id !== aid) return;
    if(d.agent && d.agent.id !== aid) return;
    const cc2 = cacheOf(aid);
    (d.messages || []).forEach(m => { LAST_ID = Math.max(LAST_ID, m.id); if(!FIRST_ID) FIRST_ID = m.id; draw(m); cachePut(cc2, m); });
  }
  JOB = d.job;
  renderBrief();
  const st = d.agent.state === 'busy' ? '正在执行任务'
           : (d.agent.state === 'on' ? '在线' : '未运行（按需）');
  const mn = modelName(CUR && CUR.model);
  $('chatSub').textContent = st + (d.agent.always_on ? ' · 常驻' : '') + ' · ' + mn;
  renderJobLine();
  paintCurModel();
}

let TIMERS = [];
function clearTimers(){ TIMERS.forEach(clearTimeout); TIMERS = []; }

/* 发完只追几次：1.5s / 4s / 8s / 15s / 25s，之后靠手动刷新 */
function followUp(){
  clearTimers();
  [1500, 4000, 8000, 15000, 25000].forEach((d, i) => {
    TIMERS.push(setTimeout(() => {
      if(!CUR) return;
      const running = JOB && (JOB.status === 'running' || JOB.status === 'queued');
      if(i < 3 || running) pull(false).catch(()=>{});
    }, d));
  });
}

async function refreshChat(){
  if(!CUR) return;
  try{ await pull(false); toast('已刷新'); }catch(e){ toast('刷新失败：' + e.message); }
}

function back(){
  clearTimers();
  closeSlash(); closeAt();
  if(history.state && history.state.chat){
    try{ history.back(); return; }catch(e){}
  }
  leaveChat();
}
function leaveChat(){
  clearTimers();
  CHAT_GEN++;
  CUR = null;
  try{ history.replaceState(null,'', location.pathname); }catch(e){}
  if(DESK()){ placeholder(); return; }
  show('s-list'); boot();
}
window.addEventListener('hashchange', () => {
  const h = (location.hash || '').replace('#', '');
  if(h && AGENTS.some(a => a.id === h) && (!CUR || CUR.id !== h)){ openAgent(h); return; }
  if(!h && CUR){ back(); }
});
window.addEventListener('popstate', () => {
  if(SHEET_CLOSING){ SHEET_CLOSING = false; return; }
  if(document.querySelector('.sheet.on')){ closeSheet(true); return; }
  if(PV_CLOSING){ PV_CLOSING = false; return; }
  if(document.getElementById('pv')){ closePreview(true); return; }
  if(SCREEN === 's-chat' || SCREEN === 's-new'){
    if(SCREEN === 's-new'){ show(CUR ? 's-chat' : 's-list'); return; }
    leaveChat();
  }
});

/* 抽屉（文件列表、技能、主机…）：左滑关闭 */
(function(){
  let el = null, sx = 0, sy = 0;
  document.addEventListener('touchstart', (e) => {
    const t = e.touches && e.touches[0];
    const sh = e.target && e.target.closest ? e.target.closest('.sheet.on') : null;
    if(!t || !sh){ el = null; return; }
    el = sh; sx = t.clientX; sy = t.clientY;
  }, {passive:true});
  document.addEventListener('touchend', (e) => {
    if(!el) return;
    const sh = el; el = null;
    const t = (e.changedTouches && e.changedTouches[0]) || null;
    if(!t) return;
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if(dx < -60 && Math.abs(dx) > Math.abs(dy) * 1.5) closeSheet();
  }, {passive:true});
})();

/* 手势返回：聊天页从左边缘右滑；图片灯箱 / 文件预览打开时，任意位置左右滑都能退出 */
(function(){
  let sx = 0, sy = 0, tracking = false, decided = false, overlay = false;
  const EDGE = 46;
  const overlayOpen = () => !!(document.getElementById('pv') || document.getElementById('lb'));
  document.addEventListener('touchstart', (e) => {
    if(!e.touches || e.touches.length !== 1) return;
    overlay = overlayOpen();
    if(!overlay && SCREEN !== 's-chat') return;
    const t = e.touches[0];
    sx = t.clientX; sy = t.clientY;
    tracking = overlay ? true : (sx <= EDGE);
    decided = false;
  }, {passive:true});
  document.addEventListener('touchmove', (e) => {
    if(!tracking || !e.touches || !e.touches.length) return;
    const t = e.touches[0];
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if(!decided && (Math.abs(dx) > 10 || Math.abs(dy) > 10)){
      decided = true;
      if(!overlay && dy > Math.abs(dx)) tracking = false;
    }
  }, {passive:true});
  document.addEventListener('touchend', (e) => {
    if(!tracking) return;
    tracking = false;
    const t = (e.changedTouches && e.changedTouches[0]) || null;
    if(!t) return;
    const dx = t.clientX - sx, dy = t.clientY - sy;
    if(overlay){
      // 灯箱/预览：左右任一方向、水平位移超过 60px 就退出，竖向优先当作滚动
      if(Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.2){
        const pv = document.getElementById('pv');
        if(pv){ closePreview(); return; }
        const lb = document.getElementById('lb');
        if(lb) lb.remove();
      }
      return;
    }
    if(dx > 72 && Math.abs(dx) > Math.abs(dy) * 1.6) back();
  }, {passive:true});
})();

function fmtSize(n){
  n = n || 0;
  return n >= 1048576 ? (n/1048576).toFixed(1) + ' MB' : n >= 1024 ? Math.round(n/1024) + ' KB' : n + ' B';
}
function attHtml(files){
  if(!files || !files.length) return '';
  const imgs = files.filter(f => f.kind === 'image');
  const rest = files.filter(f => f.kind !== 'image');
  return '<div class="atts">' +
    imgs.map(f => `<img class="att-img" src="${f.url}" alt="${esc(f.name)}" onclick="openPreview('${f.id}','${escAttr(f.name)}','${escAttr(f.mime || '')}')" onload="if(window.STICK){const c=document.getElementById('chat');if(c)c.scrollTop=c.scrollHeight}">`).join('') +
    rest.map(f => {
      const isPdf = /\.pdf$/i.test(f.name) || (f.mime || '') === 'application/pdf';
      if(isPdf){
        return `<a class="att-file" href="${f.url}" target="_blank" rel="noopener">` +
               `<span>📄 ${esc(f.name)}</span><i>${fmtSize(f.size)}</i></a>`;
      }
      return `<button class="att-file" onclick="openPreview('${f.id}','${escAttr(f.name)}','${escAttr(f.mime || '')}')">` +
             `<span>${f.kind === 'video' ? '▶' : '📄'} ${esc(f.name)}</span><i>${fmtSize(f.size)}</i></button>`;
    }).join('') +
    '</div>';
}
const TEXT_EXT = /\.(txt|md|markdown|log|json|jsonl|csv|tsv|ya?ml|ini|conf|cfg|env|xml|html?|css|js|ts|jsx|tsx|py|sh|bash|sql|go|rs|java|c|h|cpp|hpp|toml|gitignore)$/i;
async function readText(url, limit){
  const r = await fetch(url, {credentials:'same-origin'});
  if(!r.ok) throw new Error('读取失败 HTTP ' + r.status);
  const buf = await r.arrayBuffer();
  const slice = buf.slice(0, limit || 1024 * 1024);
  let txt = new TextDecoder('utf-8').decode(slice);
  if(txt.includes('\ufffd')){
    try{ txt = new TextDecoder('gb18030').decode(slice); }catch(e){}
  }
  return {txt, truncated: buf.byteLength > slice.byteLength};
}
let PV_CLOSING = false;
function closePreview(fromHistory){
  const o = document.getElementById('pv');
  if(o) o.remove();
  document.body.classList.remove('pv-open');
  if(!fromHistory && history.state && history.state.pv){
    PV_CLOSING = true;
    try{ history.back(); }catch(e){ PV_CLOSING = false; }
  }
}
async function openPreview(id, name, mime){
  closePreview();
  const url = 'api/file?id=' + encodeURIComponent(id);
  const o = document.createElement('div');
  o.id = 'pv'; o.className = 'pv';
  document.body.classList.add('pv-open');
  try{ history.pushState({pv: id}, '', location.pathname + location.hash); }catch(e){}
  window.PV_TEXT = '';
  o.innerHTML = `<div class="pvhd"><b>${esc(name)}</b>` +
    `<span class="pvact"><span id="pvCopy" class="pvcopy" onclick="pvCopyText()" style="display:none">复制全文</span>` +
    `<span class="pvcopy" onclick="dlFile('${url}','${escAttr(name)}','${escAttr(mime || '')}')">下载</span>` +
    `<span class="pvx" onclick="closePreview()">关闭</span></span></div>` +
    `<div class="pvbody"><div class="hint">加载中…</div></div>`;
  document.body.appendChild(o);
  const body = o.querySelector('.pvbody');
  const m = (mime || '').toLowerCase(), n = (name || '').toLowerCase();
  try{
    if(m.startsWith('image/') || /\.(png|jpe?g|gif|webp|heic|bmp)$/.test(n)){
      body.innerHTML = `<img src="${url}" alt="${escAttr(name)}">`;
    }else if(m === 'application/pdf' || n.endsWith('.pdf')){
      body.innerHTML = `<iframe src="${url}" title="${escAttr(name)}"></iframe>`;
    }else if(m.startsWith('video/') || /\.(mp4|mov|m4v|webm)$/.test(n)){
      body.innerHTML = `<video src="${url}" controls playsinline></video>`;
    }else if(m.startsWith('audio/') || /\.(mp3|wav|m4a|ogg|flac)$/.test(n)){
      body.innerHTML = `<audio src="${url}" controls></audio>`;
    }else if(m.startsWith('text/') || m === 'application/json' || TEXT_EXT.test(n)){
      const {txt, truncated} = await readText(url, 1024 * 1024);
      const isMd = /\.(md|markdown)$/i.test(n);
      const cb = document.getElementById('pvCopy'); if(cb) cb.style.display = '';
      window.PV_TEXT = txt;
      const copyBtn = document.getElementById('pvCopy');
      body.innerHTML = `<div class="pvtxt${isMd ? ' md' : ''}">${isMd ? md(txt) : '<pre>' + esc(txt) + '</pre>'}` +
        (truncated ? '<div class="hint">文件较大，只显示前 1 MB，完整内容请下载。</div>' : '') + '</div>';
    }else{
      body.innerHTML = '<div class="hint">这种格式浏览器打不开，点右上角「下载」用本地应用打开。</div>';
    }
  }catch(e){
    body.innerHTML = '<div class="hint">预览失败：' + esc(e.message) + '</div>';
  }
}

function pvCopyText(){
  const t = window.PV_TEXT || '';
  if(!t) return;
  navigator.clipboard.writeText(t).then(() => toast('已复制全文'), () => toast('复制失败'));
}
async function dlFile(url, name, mime){
  const href = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'dl=1';
  // iOS 的 PWA 里 <a download> 经常没反应，先试系统分享面板（会给出「存储图像/存储到文件」）
  try{
    const r = await fetch(href, {credentials:'same-origin'});
    if(r.ok){
      const b = await r.blob();
      const f = new File([b], name || 'file', {type: mime || b.type || 'application/octet-stream'});
      if(navigator.canShare && navigator.canShare({files: [f]})){
        await navigator.share({files: [f], title: name || ''});
        return;
      }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(b); a.download = name || 'file';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      return;
    }
  }catch(e){ /* 用户取消分享也会走到这里，下面兜底 */ }
  window.open(href, '_blank');
}

function viewImg(u){
  let o = document.getElementById('lb');
  if(!o){
    o = document.createElement('div'); o.id = 'lb'; o.className = 'lightbox';
    o.onclick = () => o.remove();
    o.innerHTML = '<img alt="">';
    document.body.appendChild(o);
  }
  o.querySelector('img').src = u;
}

function draw(m){
  const box = $('chat');
  if(m.agent && (!CUR || m.agent !== CUR.id)) return;   // 不是这个会话的消息，一律不画
  if(m.role === 'me'){
    let el = box.querySelector(`[data-id="${m.id}"]`);
    if(!el){
      el = document.createElement('div');
      el.className = 'msg me'; el.dataset.id = m.id;
      box.appendChild(el);
    }
    el.innerHTML = mdInline(m.text) + attHtml(m.files);
  }else{
    let el = box.querySelector(`[data-id="${m.id}"]`);
    if(!el){
      el = document.createElement('div');
      el.className = 'msg ai'; el.dataset.id = m.id;
      el.innerHTML = `<div class="who"><b>${esc(CUR.name)}</b></div><div class="txt md"></div>`;
      box.appendChild(el);
    }
    const txt = el.querySelector('.txt');
    const running = m.status === 'running';
    el.classList.toggle('streaming', running);
    if(MODE === 'result' && running){ txt.textContent = ''; }
    else { txt.innerHTML = md(m.text || ''); }
    const atts = attHtml(m.files);
    let holder = el.querySelector('.atts');
    if(atts){ if(!holder){ holder = document.createElement('div'); el.appendChild(holder); } holder.outerHTML = atts; }
    else if(holder) holder.remove();
    const meta = el.querySelector('.meta');
    const stamp = `${hhmm(m.ts)}${m.status === 'error' ? ' · 失败' : m.status === 'canceled' ? ' · 已中止' : ''}`;
    if(!meta){ const t = document.createElement('div'); t.className = 'meta'; t.textContent = stamp; el.appendChild(t); }
    else meta.textContent = stamp;
  }
  renderBrief();
  if(window.STICK) box.scrollTop = box.scrollHeight;
}

function firstVisibleId(){
  const box = $('chat');
  if(!box) return 0;
  const top = box.scrollTop;
  for(const el of box.querySelectorAll('.msg')){
    if(el.offsetTop + el.offsetHeight > top + 4) return Number(el.dataset.id) || 0;
  }
  return 0;
}
function renderLoadMore(){
  const box = $('chat');
  if(!box) return;
  const old = box.querySelector('.loadmore');
  if(old) old.remove();
  if(!HAS_MORE) return;
  const b = document.createElement('button');
  b.className = 'loadmore';
  b.textContent = '加载更早的消息（还有更多）';
  b.onclick = loadEarlier;
  box.insertBefore(b, box.firstChild);
}
async function loadEarlier(){
  if(!CUR || !HAS_MORE) return;
  WIN = Math.min(400, WIN * 2);
  await pull(true, true);
}

function renderBrief(){
  const box = $('chat');
  let b = box.querySelector('.brief');
  const job = JOB;
  if(!job || job.status !== 'running'){ if(b) b.remove(); return; }
  if(!b){ b = document.createElement('div'); b.className = 'brief'; box.appendChild(b); }
  const secs = Math.max(0, Math.round((Date.now()/1000) - (job.started || Date.now()/1000)));
  b.innerHTML = `<span class="spin"></span>${MODE === 'result' ? '正在生成' : '进行中'} · ${secs}s`;
}

function renderJobLine(){
  const j = JOB;
  $('jobLabel').textContent = !j ? '无' : (j.status === 'running' ? '进行中' :
    j.status === 'done' ? '已完成' : j.status === 'error' ? '失败' : j.status === 'canceled' ? '已中止' : j.status);
}

/* 极简 markdown：代码块、行内代码、加粗 */
function linkify(t){
  return t.replace(/(https?:\/\/[^\s<>"']+)/g, (m) => {
    let url = m, tail = '';
    const mm = /[.,;:!?)\]}>。，、；：！？）】》」』”’…]+$/.exec(url);
    if(mm){ tail = mm[0]; url = url.slice(0, -tail.length); }
    return '<a href="' + url + '" target="_blank" rel="noopener">' + url + '</a>' + tail;
  });
}
function mdInline(s){
  let t = esc(s);
  const keep = [];
  const stash = (html) => { keep.push(html); return '\u0000' + (keep.length - 1) + '\u0000'; };
  /* 先保住代码与显式链接，避免被后续的自动链接和强调规则二次处理 */
  t = t.replace(/`([^`]+)`/g, (m, c) => stash('<code>' + c + '</code>'));
  t = t.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g,
                (m, alt, u) => stash('<img src="' + u + '" alt="' + alt + '" loading="lazy">'));
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
                (m, txt, u) => stash('<a href="' + u + '" target="_blank" rel="noopener">' + txt + '</a>'));
  t = t.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  t = t.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  t = t.replace(/~~([^~]+)~~/g, '<s>$1</s>');
  t = linkify(t);
  t = t.replace(/\u0000(\d+)\u0000/g, (m, i) => keep[Number(i)]);
  return t;
}
function md(src){
  const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0, para = [];
  const flush = () => {
    if(!para.length) return;
    out.push('<p>' + mdInline(para.join('\n')).replace(/\n/g, '<br>') + '</p>');
    para = [];
  };
  while(i < lines.length){
    const L = lines[i];
    if(/^```/.test(L)){
      flush();
      const lang = L.slice(3).trim();
      const body = [];
      i++;
      while(i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      i++;
      out.push('<pre class="code">' + (lang ? '<span class="lang">' + esc(lang) + '</span>' : '') +
               '<code>' + esc(body.join('\n')) + '</code></pre>');
      continue;
    }
    if(/^\s*$/.test(L)){ flush(); i++; continue; }
    const hm = /^(#{1,6})\s+(.*)$/.exec(L);
    if(hm){ flush(); const n = hm[1].length; out.push('<h' + n + '>' + mdInline(hm[2]) + '</h' + n + '>'); i++; continue; }
    if(/^\s*([-*_])\1{2,}\s*$/.test(L)){ flush(); out.push('<hr>'); i++; continue; }
    if(/^\s*>\s?/.test(L)){
      flush();
      const buf = [];
      while(i < lines.length && /^\s*>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push('<blockquote>' + md(buf.join('\n')) + '</blockquote>');
      continue;
    }
    if(/\|/.test(L) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1]) && /-/.test(lines[i + 1])){
      flush();
      const rows = [L]; i += 2;
      while(i < lines.length && /\|/.test(lines[i]) && !/^\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = r => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
      out.push('<table><thead><tr>' + cells(rows[0]).map(c => '<th>' + mdInline(c) + '</th>').join('') +
               '</tr></thead><tbody>' +
               rows.slice(1).map(r => '<tr>' + cells(r).map(c => '<td>' + mdInline(c) + '</td>').join('') + '</tr>').join('') +
               '</tbody></table>');
      continue;
    }
    const isUl = /^\s*[-*+]\s+/.test(L), isOl = /^\s*\d+[.)]\s+/.test(L);
    if(isUl || isOl){
      flush();
      const tag = isUl ? 'ul' : 'ol', items = [];
      const re = isUl ? /^\s*[-*+]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/;
      while(i < lines.length){
        const m = re.exec(lines[i]);
        if(!m) break;
        items.push('<li>' + mdInline(m[1]) + '</li>');
        i++;
      }
      out.push('<' + tag + '>' + items.join('') + '</' + tag + '>');
      continue;
    }
    para.push(L); i++;
  }
  flush();
  return out.join('');
}

function inpText(){
  const el = $('inp');
  return (el.innerText || '').replace(/\u00a0/g, ' ');
}
function inpClear(){
  const el = $('inp');
  closeSlash(); closeAt();
  el.textContent = '';
  el.style.height = '';
  el.classList.add('is-empty');
  $('btnSend').disabled = true;
}

function normalizeSkill(text){
  if(!text || text.startsWith('/skill:')) return text;
  const m = /^\/([a-zA-Z0-9_-]+)(?=\s|$)/.exec(text);
  if(!m) return text;
  return SKILLS.some(s => s.id === m[1]) ? '/skill:' + text.slice(1) : text;
}

async function send(){
  const text = normalizeSkill(inpText().trim());
  if(!text || !CUR) return;
  inpClear();
  try{
    await api('api/send', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, text, mode: MODE})});
    await pull(false);
    setTimeout(loadStaged, 1500);
    followUp();
  }catch(e){ alert(e.message); }
}

async function cancelJob(){
  if(!CUR) return;
  try{ await api('api/cancel', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({agent: CUR.id})}); await pull(false); }catch(e){ alert(e.message); }
  closeSheet();
}

/* ---------- 输出方式 ---------- */
function setMode(m){ MODE = m; localStorage.setItem('mode', m); paintMode(); closeSheet(); if(CUR) pull(false); }
function paintMode(){
  $('modeLabel').textContent = {full:'完整', brief:'精简', result:'仅结果'}[MODE];
  document.querySelectorAll('[data-m]').forEach(el => el.textContent = el.dataset.m === MODE ? '当前' : (el.dataset.m==='brief'?'只看阶段':el.dataset.m==='result'?'跑完一次性出':''));
  document.querySelectorAll('[data-m]').forEach(el => el.classList.toggle('cur', el.dataset.m === MODE));
}

/* ---------- 附件 ---------- */
/* ---------- 模型配置 ---------- */
let MODELS = [];
function modelName(id){
  if(!id) return '未指定';
  const m = MODELS.find(x => x.id === id);
  return m ? (m.name || m.model_id) : id;
}
async function loadModels(){
  try{ const d = await api('api/models'); MODELS = d.models || []; }
  catch(e){ MODELS = []; }
  const mc = $('modelCount'); if(mc) mc.textContent = MODELS.length ? MODELS.length + ' 个' : '';
  renderModelList(); fillModelSelects();
}
function renderModelList(){
  const box = $('modelList'); if(!box) return;
  if(!MODELS.length){ box.innerHTML = '<div class="hint">还没有配置模型。下面填好保存即可。</div>'; return; }
  box.innerHTML = MODELS.map(m =>
    `<div class="item"><span class="nm">${esc(m.name)}<br>` +
    `<span style="font-size:12px;color:#8B949A">${esc(m.model_id)} · ${esc(m.base_url)} · sk ${esc(m.sk_tail || '—')}` +
    `${m.kind === 'kimi' ? ' · kimi 适配' : ''}</span></span>` +
    `<span class="r">${m.agents || 0} 个 agent</span>` +
    `<span class="r" style="color:#0F5B63;margin-left:10px" onclick="editModel('${m.id}')">编辑</span>` +
    `<span class="r" style="color:#A33A2B;margin-left:10px" onclick="deleteModel('${m.id}')">删除</span></div>`).join('');
}
function editModel(id){
  const m = MODELS.find(x => x.id === id); if(!m) return;
  $('mdName').value = m.name || '';
  $('mdBase').value = m.base_url || '';
  $('mdId').value = m.model_id || '';
  $('mdSk').value = '';
  $('mdName').dataset.editId = id;
  toast('已填入表单，sk 留空则不改');
}
async function saveModel(){
  const body = {
    id: $('mdName').dataset.editId || '',
    name: $('mdName').value.trim(),
    base_url: $('mdBase').value.trim(),
    model_id: $('mdId').value.trim(),
    sk: $('mdSk').value.trim()
  };
  try{
    const d = await api('api/model/save', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)});
    toast('已保存'); delete $('mdName').dataset.editId;
    ['mdName','mdBase','mdId','mdSk'].forEach(k => { $(k).value = ''; });
    loadModels(); refreshList();
  }catch(e){ alert(e.message); }
}
async function deleteModel(id){
  if(!confirm('删除这个模型配置？还有 agent 在用时会被拦住。')) return;
  try{
    await api('api/model/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id})});
    toast('已删除'); loadModels();
  }catch(e){ alert(e.message); }
}
function fillModelSelects(){
  const sel = $('naModel');
  if(sel){
    const cur = sel.value;
    sel.innerHTML = MODELS.length
      ? MODELS.map(m => `<option value="${escAttr(m.id)}">${esc(m.name)}</option>`).join('')
      : '<option value="">（暂无模型配置，先用机器默认）</option>';
    if(cur) sel.value = cur;
  }
}
function paintCurModel(){
  const el = $('curModelLabel');
  if(!el) return;
  el.textContent = CUR ? modelName(CUR.model) : '—';
}
function openModelPick(){
  if(!CUR) return;
  const box = $('mpickList'); if(!box) return;
  const row = (id, name, sub) =>
    `<div class="item" onclick="pickModel('${escAttr(id)}')"><span class="nm">${esc(name)}<br>` +
    `<span style="font-size:12px;color:#8B949A">${esc(sub || '')}</span></span>` +
    (CUR.model === id ? '<span class="r" style="color:#0F5B63">当前</span>' : '') + '</div>';
  box.innerHTML = MODELS.length
    ? MODELS.map(m => row(m.id, m.name, m.model_id + ' · ' + m.base_url)).join('')
    : '<div class="hint">还没有模型配置，先到「模型配置」里加一条。</div>';
  openSheet('mpick');
}
async function pickModel(id){
  if(!CUR) return;
  try{
    await api('api/agent/model', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, model: id})});
    CUR.model = id;
    closeSheet();
    toast(id ? ('已切到 ' + modelName(id) + '，下一轮生效') : '已切回本机默认，下一轮生效');
    pull(false).catch(()=>{});
  }catch(e){ alert(e.message); }
}

/* ---------- 斜杠技能 ---------- */
let SKILLS = [], SKILL_TOTAL = 0, SLASH = [], SLASH_IDX = 0, COMPOSING = false;
function paintSkillCount(){
  const el = $('skillCount');
  if(el) el.textContent = SKILL_TOTAL ? SKILLS.length + '/' + SKILL_TOTAL : '';
}
async function loadSkillCache(){
  if(!CUR){ SKILLS = []; SKILL_TOTAL = 0; paintSkillCount(); return; }
  try{
    const d = await api('api/skills?agent=' + encodeURIComponent(CUR.id));
    const ss = d.skills || [];
    SKILL_TOTAL = ss.length;
    SKILLS = ss.filter(s => s.enabled);
  }catch(e){ SKILLS = []; SKILL_TOTAL = 0; }
  paintSkillCount();
}
function closeSlash(){ SLASH = []; SLASH_IDX = 0; const m = $('slashMenu'); if(m){ m.classList.remove('on'); m.innerHTML = ''; } }
function slashPaint(){
  const m = $('slashMenu');
  if(!m || !SLASH.length){ closeSlash(); return; }
  m.innerHTML = '<div class="hd">技能 · ↑↓ 选择，回车使用，Esc 取消</div>' +
    SLASH.map((s, i) => `<div class="it${i === SLASH_IDX ? ' cur' : ''}" onclick="useSkill(${i})">` +
      `<b>/skill:${esc(s.id)}</b><span>${esc(s.summary || s.name || '')}</span></div>`).join('');
  m.classList.add('on');
}
function slashCheck(){
  const t = inpText();
  const m = /^\/(?:skill:)?([a-zA-Z0-9_-]*)$/.exec(t);
  if(!m || !SKILLS.length){ closeSlash(); return; }
  const q = m[1].toLowerCase();
  SLASH = SKILLS.filter(s => (s.id || '').toLowerCase().includes(q) ||
                             (s.name || '').toLowerCase().includes(q)).slice(0, 8);
  if(!SLASH.length){ closeSlash(); return; }
  if(SLASH_IDX >= SLASH.length) SLASH_IDX = 0;
  slashPaint();
}
function useSkill(i){
  const s = SLASH[i || 0];
  if(!s) return;
  const el = $('inp');
  el.textContent = '/skill:' + s.id + ' ';
  el.classList.remove('is-empty');
  $('btnSend').disabled = false;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 110) + 'px';
  closeSlash();
  el.focus();
  try{ const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
       const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r); }catch(e){}
}

/* ---------- 拖拽 / 粘贴上传 ---------- */
(function(){
  let dragDepth = 0;
  const hint = () => {
    let h = document.getElementById('dropHint');
    if(!h){
      h = document.createElement('div');
      h.id = 'dropHint';
      h.textContent = '松开即可上传到当前会话';
      document.body.appendChild(h);
    }
    return h;
  };
  const visible = () => CUR && !$('s-chat').classList.contains('hide');
  document.addEventListener('dragenter', (e) => {
    if(!visible() || !e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
    e.preventDefault(); dragDepth++; hint().classList.add('on');
  });
  document.addEventListener('dragover', (e) => {
    if(!visible()) return;
    if(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')){ e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
  });
  document.addEventListener('dragleave', (e) => {
    if(!visible()) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if(!dragDepth){ const h = document.getElementById('dropHint'); if(h) h.classList.remove('on'); }
  });
  document.addEventListener('drop', (e) => {
    if(!visible() || !e.dataTransfer) return;
    const files = Array.from(e.dataTransfer.files || []);
    if(!files.length) return;
    e.preventDefault();
    dragDepth = 0;
    const h = document.getElementById('dropHint'); if(h) h.classList.remove('on');
    uploadFiles(files);
  });
  document.addEventListener('paste', (e) => {
    if(!visible() || !e.clipboardData) return;
    const files = Array.from(e.clipboardData.files || []);
    if(files.length){ e.preventDefault(); uploadFiles(files); }
  });
})();

/* ---------- @ 文件 ---------- */
let FILES = [], AT = [], AT_IDX = 0;
async function loadFileCache(){
  if(!CUR){ FILES = []; return; }
  try{
    const d = await api('api/files?agent=' + encodeURIComponent(CUR.id));
    FILES = (d.files || []).slice(0, 30);
  }catch(e){ FILES = []; }
}
function closeAt(){ AT = []; AT_IDX = 0; const m = $('atMenu'); if(m){ m.classList.remove('on'); m.innerHTML = ''; } }
function atPaint(){
  const m = $('atMenu');
  if(!m || !AT.length){ closeAt(); return; }
  m.innerHTML = '<div class="hd">文件 · ↑↓ 选择，回车插入，Esc 取消</div>' +
    AT.map((f, i) => `<div class="it${i === AT_IDX ? ' cur' : ''}" onclick="useFile(${i})">` +
      `<b>@${esc(f.name)}</b><span>${f.msg ? '已发过' : '待发'} · ${(f.size/1024).toFixed(0)} KB</span></div>`).join('');
  m.classList.add('on');
}
function atQuery(){
  const t = inpText();
  const m = /(?:^|\s)@([^\s@]*)$/.exec(t);
  return m ? m[1] : null;
}
function atCheck(){
  const q = atQuery();
  if(q === null || !FILES.length){ closeAt(); return; }
  const ql = q.toLowerCase();
  AT = FILES.filter(f => (f.name || '').toLowerCase().includes(ql)).slice(0, 8);
  if(!AT.length){ closeAt(); return; }
  if(AT_IDX >= AT.length) AT_IDX = 0;
  atPaint();
}
function useFile(i){
  const f = AT[i || 0];
  if(!f) return;
  const el = $('inp');
  const t = inpText();
  const m = /(^|[\s])@[^\s@]*$/.exec(t);
  const head = m ? t.slice(0, m.index + m[1].length) : t + ' ';
  el.textContent = head + '@' + f.name + ' ';
  el.classList.remove('is-empty');
  $('btnSend').disabled = false;
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 110) + 'px';
  closeAt();
  el.focus();
  try{ const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
       const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r); }catch(e){}
}

function pick(kind){
  const inp = $('fileInput');
  inp.disabled = false;
  if(kind === 'camera'){
    inp.accept = 'image/*';
    inp.setAttribute('capture', 'environment');
  }else if(kind === 'image'){
    inp.accept = 'image/*';
    inp.removeAttribute('capture');
  }else{
    /* 文件入口只要文档/压缩包/代码，别让系统弹相机、录音 */
    inp.accept = 'application/*,text/*';
    inp.removeAttribute('capture');
  }
  inp.click();
  /* 用户在系统选择器里点了取消：也要把文件框收回去，避免长期多出一个“可用字段” */
  const restore = () => {
    setTimeout(() => { try{ inp.disabled = true; }catch(e){} }, 800);
    window.removeEventListener('focus', restore);
  };
  setTimeout(() => window.addEventListener('focus', restore, {once:true}), 300);
}
async function uploadPicked(inp){
  const files = [...(inp.files || [])];
  inp.value = '';
  setTimeout(() => { try{ inp.disabled = true; }catch(e){} }, 0);
  await uploadFiles(files);
}
async function uploadFiles(files){
  if(!files || !files.length || !CUR) return;
  let ok = 0, bad = [];
  for(let i = 0; i < files.length; i++){
    const f = files[i];
    if(f.size > 100*1024*1024){ bad.push(f.name + '（超过 100 MB）'); continue; }
    const state = $('uploadState');
    if(state) state.textContent = `正在上传 ${i+1}/${files.length}：${f.name}`;
    try{
      const r = await fetch(`api/upload?agent=${encodeURIComponent(CUR.id)}`, {
        method:'POST', credentials:'same-origin',
        headers:{'X-Filename': encodeURIComponent(f.name)},
        body: f
      });
      let d = {};
      try{ d = await r.json(); }catch(e){}
      if(!r.ok){
        if(r.status === 401){ show('s-login'); return; }
        if(r.status === 410){ alert(d.error || '这个会话已经不存在了'); location.hash=''; location.reload(); return; }
        bad.push(f.name + '（' + (d.error || ('HTTP ' + r.status)) + '）');
        continue;
      }
      ok++;
    }catch(e){ bad.push(f.name + '（' + e.message + '）'); }
  }
  const state = $('uploadState');
  if(state) state.textContent = '';
  if(ok) toast(files.length > 1 ? `已上传 ${ok} 个，进暂存区` : '已上传：' + files[0].name);
  if(bad.length) alert('这些没传上去：\n' + bad.join('\n'));
  loadStaged();
}
async function loadStaged(){
  if(!CUR){ paintAttachBar([]); return; }
  try{
    const d = await api('api/files?agent=' + encodeURIComponent(CUR.id));
    paintAttachBar((d.files || []).filter(f => !f.msg));
  }catch(e){ paintAttachBar([]); }
}

function paintAttachBar(pending){
  const bar = $('attachBar');
  if(!bar) return;
  bar.innerHTML = (pending || []).map(f => {
    const pick = `onclick="openPreview('${f.id}','${escAttr(f.name)}','${escAttr(f.mime || '')}')"`;
    const thumb = f.kind === 'image'
      ? `<img src="${f.url}" alt="" ${pick}>`
      : `<span class="fi" ${pick}>${f.kind === 'video' ? '▶' : '📄'}</span>`;
    return `<div class="att-chip">${thumb}<b>${esc(f.name)}</b>` +
           `<span class="x" onclick="deleteFile('${f.id}')">×</span></div>`;
  }).join('');
  bar.classList.toggle('on', !!(pending && pending.length));
}

function downloadFile(id, name){
  const a = document.createElement('a');
  a.href = 'api/file?id=' + encodeURIComponent(id) + '&dl=1';
  a.download = name || '';
  document.body.appendChild(a); a.click(); a.remove();
}

function previewFile(id){
  window.open('api/file?id=' + encodeURIComponent(id), '_blank');
}

async function deleteFile(id){
  if(!confirm('删除这个附件？')) return;
  try{
    await api('api/file/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id})});
    toast('已删除'); loadStaged();
  }catch(e){ toast('删除失败：' + e.message); }
}

/* ---------- 技能 ---------- */
async function loadSkills(){
  if(!CUR){ $('skillList').innerHTML = '<div class="hint">先打开一个会话。</div>'; return; }
  const d = await api('api/skills?agent=' + encodeURIComponent(CUR.id));
  const ss = d.skills || [];
  const on = ss.filter(s => s.enabled);
  const off = ss.filter(s => !s.enabled);
  SKILLS = on; SKILL_TOTAL = ss.length; paintSkillCount();
  const row = (s, right) =>
    `<div class="item"><span class="nm">/${esc(s.id)} · ${esc(s.name)}<br>` +
    `<span style="font-size:12px;color:#8B949A">${esc(s.summary || '')}</span></span>${right}</div>`;
  const sec = (title, arr) => arr.length
    ? `<div class="hint" style="padding:10px 16px 4px">${title}（${arr.length}）</div>` + arr.join('')
    : '';
  const installed = sec('已安装到本会话', on.map(s => row(s,
    `<span class="r" style="color:#A33A2B" onclick="skillOp('uninstall','${s.id}')">卸载</span>`)));
  const available = sec('这台机器上有，本会话未装', off.map(s => row(s,
    `<span class="r" style="color:#0F5B63" onclick="skillOp('install','${s.id}')">安装</span>` +
    (s.installed_count ? '' :
      `<span class="r" style="color:#A33A2B;margin-left:12px" onclick="skillOp('delete','${s.id}')">删除</span>`))));
  $('skillList').innerHTML = (installed + available) ||
    '<div class="hint">这台机器的技能库是空的。可以让某个 agent 写一个技能：把 SKILL.md 放到 ~/aihub/skills/<id>/。</div>';
}

async function skillOp(op, id){
  if(op === 'delete' && !confirm('从本机技能库里删除 /' + id + '？\n只在没有任何 agent 装着时才会成功。')) return;
  try{
    await api('api/skill/op', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({op, agent: CUR.id, skill: id})});
    toast(op === 'install' ? '正在安装…' : op === 'uninstall' ? '正在卸载…' : '正在删除…');
    setTimeout(loadSkills, 2500);
  }catch(e){ alert(e.message); }
}

async function deleteSkill(id){
  if(!confirm('全局删除这个技能？所有 agent 上的绑定也会一起删。\n（如果只想让当前 agent 不用它，请点「已启用」改成停用）')) return;
  await api('api/skill/delete', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({skill: id})});
  toast('已删除'); loadSkills();
}

function importSkillPick(){ const el = $('skFile'); if(el) el.click(); }
async function importSkill(inp){
  const f = (inp.files || [])[0]; if(!f) return;
  inp.value = '';
  const txt = await f.text();
  let id = (f.name || 'skill').replace(/\.(md|markdown)$/i, '').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
  let name = id, summary = '', when = '';
  for(const line of txt.split('\n').slice(0, 30)){
    const t = line.trim();
    if(t.startsWith('name:') && t.length > 5) name = t.slice(5).trim();
    if(t.startsWith('description:') && t.length > 12) summary = t.slice(12).trim();
    if(t.startsWith('whenToUse:') && t.length > 10) when = t.slice(10).trim();
  }
  if(!id){ toast('文件名不能当技能 id，请改成英文名'); return; }
  try{
    await api('api/skill/create', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, name, summary, when, body: txt})});
    await api('api/skill/bind', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, skill: id, on: true})});
    toast('已导入并启用：' + id);
    loadSkills(); loadSkillCache();
  }catch(e){ alert('导入失败：' + e.message); }
}

async function newSkill(){
  const name = $('skName').value.trim();
  if(!name){ toast('先填技能名'); return; }
  const summary = prompt('一句话说明这个技能是做什么的：') || '';
  const when = prompt('什么时候该用它（写给模型判断用）：') || '';
  const body = prompt('具体步骤（可以长，粘贴进来）：') || '';
  try{
    const d = await api('api/skill/create', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, summary, when, body})});
    await api('api/skill/bind', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, skill: d.id, on: true})});
    $('skName').value = '';
    toast('已创建并启用');
    loadSkills();
  }catch(e){ toast('创建失败：' + e.message); }
}

/* ---------- 主机 ---------- */
async function loadHosts(){
  const d = await api('api/hosts');
  const hs = d.hosts || [];
  $('hostCount').textContent = hs.filter(h => h.online).length + '/' + hs.length;
  $('hostList').innerHTML = hs.length ? hs.map(h =>
    `<div class="item"><span class="nm">${esc(h.name)}</span>
      <span class="r" style="color:${h.online ? '#0F5B63' : '#9AA39D'}">${h.online ? '在线' : '离线'} · ${h.agents} 个 agent</span>
      <span class="r" style="color:#0F5B63;margin-left:12px" onclick="showInstall('${escAttr(h.name)}')">安装命令</span>
      <span class="r" style="color:#B07C33;margin-left:10px" onclick="rotateHost('${escAttr(h.name)}')">换 token</span>
      <span class="r" style="color:#A33A2B;margin-left:10px" onclick="deleteHost('${escAttr(h.name)}')">删除</span>
    </div>`).join('') : '<div class="hint">还没有主机。</div>';
}

async function createHost(){
  const name = $('hostName').value.trim();
  if(!name){ toast('先填主机名'); return; }
  try{
    await api('api/host/create', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})});
    $('hostName').value = '';
    await loadHosts(); refreshList();
    toast('已添加，点「安装命令」拿那条命令');
  }catch(e){ toast('添加失败：' + e.message); }
}

async function showInstall(name){
  try{
    const d = await api('api/host/install_token', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})});
    const it = d.it;
    const mins = Math.max(1, Math.round((d.expires - Date.now()/1000) / 60));
    const lin = `curl -fsSL -H "Authorization: Bearer ${it}" https://nykj.store/hub/api/host/install.sh | bash`;
    const win = `$h=@{Authorization="Bearer ${it}"}; iwr -useb -Headers $h https://nykj.store/hub/api/host/install.ps1 | iex`;
    const box = document.createElement('div');
    box.style.cssText = 'position:fixed;inset:0;background:rgba(20,24,26,.4);z-index:100;display:flex;align-items:flex-end;justify-content:center;padding:0';
    box.className = 'cmdbox';
    box.innerHTML = `<div style="background:#fff;border-radius:16px 16px 0 0;padding:18px 18px calc(18px + env(safe-area-inset-bottom));max-width:560px;width:100%;margin:0 auto;max-height:88vh;overflow:auto">
      <div style="font-size:15px;font-weight:600;margin-bottom:6px">在「${esc(name)}」上执行</div>
      <div style="font-size:12px;color:#6E7873;margin-bottom:10px">需要 python3 与已登录的 kimi。这条命令用的是<b>一次性令牌</b>，${mins} 分钟内有效、只能用一次；长期凭据在机器上就地生成，不经过命令行。</div>
      <div style="font-size:12px;color:#6E7873;margin:8px 0 4px">Linux / macOS</div>
      <textarea readonly spellcheck="false" style="width:100%;height:64px;font:11.5px/1.5 ui-monospace,Menlo,monospace;padding:10px;border:1px solid #DCE1DD;border-radius:8px;background:#FAFBFA;word-break:break-all;resize:none">${esc(lin)}</textarea>
      <div style="font-size:12px;color:#6E7873;margin:8px 0 4px">Windows (PowerShell)</div>
      <textarea readonly spellcheck="false" style="width:100%;height:64px;font:11.5px/1.5 ui-monospace,Menlo,monospace;padding:10px;border:1px solid #DCE1DD;border-radius:8px;background:#FAFBFA;word-break:break-all;resize:none">${esc(win)}</textarea>
      <div style="display:flex;gap:10px;margin-top:12px;justify-content:flex-end">
        <button class="pick" style="padding:10px 14px" data-c="${escAttr(lin)}" onclick="navigator.clipboard.writeText(this.dataset.c);toast('已复制 Linux 命令')">复制 Linux</button>
        <button class="pick" style="padding:10px 14px" data-c="${escAttr(win)}" onclick="navigator.clipboard.writeText(this.dataset.c);toast('已复制 Windows 命令')">复制 Windows</button>
        <button class="pick" style="padding:10px 14px" onclick="this.closest('div[style]').remove()">关闭</button>
      </div></div>`;
    document.body.appendChild(box);
    box.addEventListener('click', e => { if(e.target === box) box.remove(); });
  }catch(e){ toast('生成安装命令失败：' + e.message); }
}

async function rotateHost(name){
  if(!confirm(`给「${name}」换 token？旧 token 立刻失效，那台机器上的 Daemon 要重装。`)) return;
  try{
    const d = await api('api/host/rotate', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})});
    await loadHosts(); showInstall(name);
  }catch(e){ toast('换 token 失败：' + e.message); }
}

async function deleteHost(name){
  if(!confirm(`删除主机「${name}」？先确保它上面没有 agent。`)) return;
  try{
    await api('api/host/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})});
    await loadHosts(); refreshList(); toast('已删除');
  }catch(e){ toast('删除失败：' + e.message); }
}

/* ---------- 搜索 ---------- */
let Q_TIMER = null;
async function doSearch(){
  const q = $('qInput').value.trim();
  if(!q){ $('qHits').innerHTML = ''; return; }
  const d = await api('api/search?q=' + encodeURIComponent(q));
  const hits = d.hits || [], files = d.files || [];
  let html = '';
  if(hits.length) html += hits.map(h =>
    `<button class="item" onclick="jumpTo('${h.agent}', ${h.id})">
      <span class="nm">${esc(h.agent_name || h.agent)} · ${esc(h.snip || '')}</span>
      <span class="r">${hhmm(h.ts)}</span></button>`).join('');
  if(files.length){
    html += '<div class="hint">文件</div>' + files.map(f =>
      `<button class="item" onclick="jumpTo('${f.agent}', 0)">
        <span class="nm">${esc(f.name)}</span><span class="r">${hhmm(f.ts)}</span></button>`).join('');
  }
  $('qHits').innerHTML = html || '<div class="hint">没有匹配。</div>';
}

function jumpTo(agent, msgId){
  closeSheet();
  openAgent(agent).then(() => {
    if(!msgId) return;
    setTimeout(() => {
      const el = document.querySelector(`#chat [data-id="${msgId}"]`);
      if(el){ el.scrollIntoView({block:'center'}); el.style.transition='background .3s';
              el.style.background='#FBF3DA'; setTimeout(()=>el.style.background='', 1500); }
    }, 700);
  });
}

/* ---------- 新建 agent ---------- */
async function newAgent(){
  closeSheet();
  $('naName').value = ''; $('naCwd').value = ''; $('naPrompt').value = ''; $('naOn').checked = true;
  show('s-new');
  try{
    const d = await api('api/hosts');
    const hs = (d.hosts || []);
    $('naHost').innerHTML = hs.map(h =>
      `<option value="${escAttr(h.name)}">${esc(h.name)} · ${h.online ? '在线' : '离线'} · ${h.agents} 个 agent</option>`).join('')
      || '<option value="中枢机">中枢机 · 离线</option>';
    const first = hs.find(h => h.online) || hs[0];
    if(first) $('naHost').value = first.name;
  }catch(e){ $('naHost').innerHTML = '<option value="中枢机">中枢机</option>'; }
  if(DESK()) setTimeout(()=>$('naName').focus(), 120);
}
function backToList(){
  if(SCREEN === 's-new' && history.state && history.state.chat){ try{ history.back(); return; }catch(e){} }
  show(CUR ? 's-chat' : 's-list'); if(!CUR) refreshList();
}

async function createAgent(){
  const name = $('naName').value.trim();
  if(!name){ toast('先填名字'); return; }
  const model = $('naModel').value;
  if(!model){ toast('先到「模型配置」里加一个模型'); return; }
  try{
    const d = await api('api/agent/create', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, model, host: $('naHost').value.trim() || '中枢机',
        cwd: $('naCwd').value.trim(), prompt: $('naPrompt').value.trim(),
        always_on: $('naOn').checked})});
    toast('已创建');
    await refreshList();
    const a = AGENTS.find(x => x.id === d.agent);
    if(a) openAgent(a.id); else show('s-list');
  }catch(e){ toast('创建失败：' + e.message); }
}

/* ---------- 已删除 ---------- */
async function loadArchived(){
  const d = await api('api/agents/archived');
  const list = d.agents || [];
  $('archCount').textContent = list.length ? list.length + ' 个' : '';
  $('archList').innerHTML = list.length ? list.map(a =>
    `<div class="item"><span class="nm">${esc(a.name)}</span>
      <span class="r" style="color:#0F5B63" onclick="restoreAgent('${a.id}')">恢复</span>
      <span class="r" style="color:#A33A2B;margin-left:14px" onclick="purgeAgent('${a.id}','${esc(a.name)}')">彻底清除</span>
    </div>`).join('')
    : '<div class="hint">没有已删除的 agent。</div>';
}

async function restoreAgent(id){
  await api('api/agent/restore', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({agent: id})});
  toast('已恢复'); await loadArchived(); refreshList();
}

async function purgeAgent(id, name){
  const typed = prompt(`彻底清除「${name}」会删掉它的所有消息、任务、产出记录，以及机器上的工作目录，且不可恢复。\n\n请输入它的名字确认：`);
  if(typed === null) return;
  if(typed.trim() !== name){ toast('名字不一致，已取消'); return; }
  try{
    await api('api/agent/purge', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: id, name})});
    toast('已彻底清除'); await loadArchived(); refreshList();
  }catch(e){ toast('清除失败：' + e.message); }
}

/* ---------- 团队 ---------- */
async function loadTeam(){
  if(!CUR) return;
  const d = await api('api/team', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({commander: CUR.id})});
  const mem = d.members || [], cand = d.candidates || [];
  $('teamCount').textContent = mem.length ? mem.length + ' 个' : '';
  let html = mem.length ? mem.map(m =>
    `<div class="item"><span class="nm">${esc(m.name)}</span>
      <span class="r" style="color:#A33A2B" onclick="teamRemove('${m.id}')">移出</span></div>`).join('')
    : '<div class="hint">它名下还没有 agent。</div>';
  if(cand.length){
    html += '<div class="hint">可加入（未归属任何团队）</div>';
    html += cand.map(c =>
      `<div class="item"><span class="nm">${esc(c.name)}</span>
        <span class="r" style="color:#0F5B63" onclick="teamAdd('${c.id}')">加入</span></div>`).join('');
  }
  $('teamList').innerHTML = html;
}

async function teamAdd(id){
  await api('api/team/add', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({commander: CUR.id, member: id})});
  toast('已加入团队'); await loadTeam(); boot();
}
async function teamRemove(id){
  await api('api/team/remove', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({commander: CUR.id, member: id})});
  toast('已移出团队'); await loadTeam(); boot();
}

async function deleteAgent(){
  if(!CUR) return;
  if(!confirm(`删除「${CUR.name}」？\n历史消息与产出会保留，但它不再出现在列表里。`)) return;
  try{
    await api('api/agent/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id})});
    closeSheet(); toast('已删除'); back(); boot();
  }catch(e){ toast('删除失败：' + e.message); }
}

/* ---------- 抽屉 ---------- */
function paintAgentInfo(){
  if(!CUR) { $('mInfo').innerHTML = ''; return; }
  const model = CUR.model ? `<div class="kv"><b>模型</b><span>${esc(CUR.model)}</span></div>` : '';
  $('mInfo').innerHTML =
    `<div class="r1"><span class="host">${esc(CUR.name)} · ${esc(CUR.host || '')}</span></div>
     <div class="kv"><b>目录</b><span title="点击复制" onclick="copyPath(this)">${esc(CUR.cwd || '—')}</span></div>
     <div class="kv"><b>本轮在</b><span style="color:#0F5B63" onclick="setWorkdir()">${esc(CUR.workdir || '（默认目录）')} · 点击切换</span></div>${model}`;
}

async function setWorkdir(){
  if(!CUR) return;
  const cur = CUR.workdir || '';
  const v = prompt('这个会话在哪个目录里干活？\n填绝对路径；留空表示回到 agent 的默认目录。\n\n' +
                   '例如 /home/ubuntu/work/项目名 或 C:\\work\\项目名', cur);
  if(v === null) return;
  try{
    const d = await api('api/agent/workdir', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, path: v.trim()})});
    CUR.workdir = d.workdir || '';
    const a = AGENTS.find(x => x.id === CUR.id);
    if(a) a.workdir = CUR.workdir;
    paintAgentInfo();
    toast(CUR.workdir ? ('已切到 ' + CUR.workdir + '，下一轮生效') : '已回到默认目录，下一轮生效');
  }catch(e){ alert(e.message); }
}

function copyPath(el){
  const p = (CUR && CUR.cwd) || el.textContent;
  if(navigator.clipboard){ navigator.clipboard.writeText(p).then(()=>toast('已复制目录')).catch(()=>toast(p)); }
  else toast(p);
}

function paintAlways(){
  const on = !!(CUR && CUR.always_on);
  $('alwaysLabel').textContent = on ? '开' : '关';
  $('alwaysLabel').classList.toggle('cur', on);
}

async function toggleAlways(){
  if(!CUR) return;
  const want = !CUR.always_on;
  try{
    await api('api/agent/always_on', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, on: want})});
    CUR.always_on = want;
    const a = AGENTS.find(x => x.id === CUR.id);
    if(a) a.always_on = want;
    paintAlways(); renderList();
    toast(want ? '已设为常驻，实例会保持在线' : '已改为按需，空闲 30 分钟后回收');
  }catch(e){ toast('设置失败：' + e.message); }
}

async function togglePin(){
  if(!CUR) return;
  const want = !CUR.pinned;
  try{
    const d = await api('api/agent/pin', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id, on: want})});
    const ts = d.ts || 0;
    CUR.pinned = ts;
    const a = AGENTS.find(x => x.id === CUR.id);
    if(a) a.pinned = ts;
    paintPin(); renderList();
    toast(want ? '已置顶，排在最前' : '已取消置顶');
  }catch(e){ toast((want ? '置顶' : '取消置顶') + '失败：' + e.message); }
}

function paintPin(){
  const on = !!(CUR && CUR.pinned);
  $('pinLabel').textContent = on ? '已置顶' : '关';
  $('pinLabel').classList.toggle('cur', on);
}

async function reloadAgent(){
  if(!CUR) return;
  if(!confirm(`重载「${CUR.name}」？\n会重启它的 ACP 进程，正在跑的任务结束后生效；对话历史保留。`)) return;
  try{
    await api('api/agent/reload', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({agent: CUR.id})});
    closeSheet(); toast('已请求重载，空闲后生效');
  }catch(e){ toast('重载失败：' + e.message); }
}

function toast(msg){
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;left:50%;transform:translateX(-50%);bottom:96px;background:rgba(20,24,26,.9);' +
    'color:#fff;padding:9px 14px;border-radius:10px;font-size:13px;z-index:99;max-width:80%;text-align:center';
  document.body.appendChild(t); setTimeout(()=>t.remove(), 2200);
}

let SHEET_CLOSING = false;
function openSheet(k){
  closeSheet();
  $('mask').classList.add('on');
  const el = $('sheet-' + k);
  if(el) el.classList.add('on');
  syncTabIndex(SCREEN);
  try{ history.pushState({sheet: k}, '', location.pathname + location.hash); }catch(e){}
  if(k === 'arch') loadArchived();
  if(k === 'host') loadHosts();
  if(k === 'sk') loadSkills();
  if(k === 'sec') loadSec();
  if(k === 's'){ setTimeout(()=>$('qInput').focus(), 150); }
  if(k === 'team'){ loadTeam(); $('teamCount').textContent = (CUR && CUR.team) ? CUR.team + ' 个' : ''; }
  if(k === 'a') loadStaged();
  if(k === 'm'){ renderJobLine(); paintPin(); paintAlways(); paintAgentInfo(); paintCurModel(); paintSkillCount(); loadSkillCache(); }
}
function closeSheet(fromHistory){
  $('mask').classList.remove('on');
  document.querySelectorAll('.sheet').forEach(el => el.classList.remove('on'));
  syncTabIndex(SCREEN);
  if(!fromHistory && history.state && history.state.sheet){
    SHEET_CLOSING = true;
    try{ history.back(); }catch(e){ SHEET_CLOSING = false; }
  }
}

/* ---------- 输入框 ---------- */
(function showVer(){
  const s = [...document.scripts].map(x => x.src).join(' ');
  const m = /app\.js\?v=(\d+)/.exec(s);
  const el = document.getElementById('verLine');
  if(el) el.textContent = '版本 ' + (m ? 'v' + m[1] : '?');
})();
function bindInput(){
  const ta = $('inp');
  const grow = () => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 110) + 'px';
  };
  ta.classList.add('is-empty');
  ta.addEventListener('input', () => {
    grow();
    const t = inpText();
    ta.classList.toggle('is-empty', !t.trim());
    $('btnSend').disabled = !t.trim();
    if(!COMPOSING){ slashCheck(); atCheck(); }
  });
  ta.addEventListener('paste', (e) => {
    e.preventDefault();
    const t = ((e.clipboardData || window.clipboardData).getData('text/plain') || '');
    document.execCommand('insertText', false, t);
  });
  ta.addEventListener('compositionstart', () => { COMPOSING = true; });
  ta.addEventListener('compositionend', () => { COMPOSING = false; slashCheck(); });
  ta.addEventListener('keydown', (e) => {
    if(AT.length){
      if(e.key === 'ArrowDown'){ e.preventDefault(); AT_IDX = (AT_IDX + 1) % AT.length; atPaint(); return; }
      if(e.key === 'ArrowUp'){ e.preventDefault(); AT_IDX = (AT_IDX - 1 + AT.length) % AT.length; atPaint(); return; }
      if(e.key === 'Enter' || e.key === 'Tab'){ e.preventDefault(); useFile(AT_IDX); return; }
      if(e.key === 'Escape'){ e.preventDefault(); closeAt(); return; }
    }
    if(SLASH.length){
      if(e.key === 'ArrowDown'){ e.preventDefault(); SLASH_IDX = (SLASH_IDX + 1) % SLASH.length; slashPaint(); return; }
      if(e.key === 'ArrowUp'){ e.preventDefault(); SLASH_IDX = (SLASH_IDX - 1 + SLASH.length) % SLASH.length; slashPaint(); return; }
      if(e.key === 'Enter' || e.key === 'Tab'){ e.preventDefault(); useSkill(SLASH_IDX); return; }
      if(e.key === 'Escape'){ e.preventDefault(); closeSlash(); return; }
    }
    if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); send(); }
  });
  $('btnLogin').onclick = login;
  $('pwd').addEventListener('keydown', e => { if(e.key === 'Enter') login(); });
}

window.addEventListener('error', e => {
  const t = document.createElement('div');
  t.textContent = '页面出错：' + (e.message || '未知');
  t.style.cssText = 'position:fixed;left:12px;right:12px;bottom:80px;background:#A33A2B;color:#fff;' +
                    'padding:10px 14px;border-radius:10px;font-size:13px;z-index:99';
  document.body.appendChild(t); setTimeout(()=>t.remove(), 4000);
});
/* 视口高度：键盘弹出、地址栏收放都跟着走，避免输入框被挡住 */
function fitApp(){
  const vv = window.visualViewport;
  if(!vv) return;
  const kb = (window.innerHeight || 0) - vv.height;   /* 键盘弹起时两者差得很多 */
  if(kb > 120){
    document.documentElement.style.setProperty('--app-h', vv.height + 'px');
  }else{
    document.documentElement.style.removeProperty('--app-h');
  }
}
window.STICK = true;
function nearBottom(){
  const b = $('chat');
  return b.scrollHeight - b.scrollTop - b.clientHeight < 160;
}
function scrollChat(force){
  const b = $('chat');
  if(!b) return;
  if(force || nearBottom()) b.scrollTop = b.scrollHeight;
}
function syncTabIndex(activeId){
  document.querySelectorAll('input,textarea,select').forEach(el => {
    const scr = el.closest('section');
    const sheet = el.closest('.sheet');
    const on = scr ? (scr.id === activeId)
                   : (sheet ? sheet.classList.contains('on') : true);
    el.tabIndex = on ? 0 : -1;
    /* 非当前页 / 未展开抽屉里的表单控件直接 disable：
       iOS 会按页面上「可用」的文本字段数量弹「‹ › ✓」辅助条，
       抽屉只是 transform 移动、字段其实还在。 */
    if(el.id !== 'fileInput') el.disabled = (!on && !DESK());
  });
}
if (window.visualViewport){
  window.visualViewport.addEventListener('resize', () => {
    fitApp();
    if(SCREEN === 's-chat' && window.STICK) scrollChat(true);
  });
  window.visualViewport.addEventListener('scroll', fitApp);
}
window.addEventListener('resize', fitApp);
fitApp();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').then(reg => {
    reg.update().catch(()=>{});
    let reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if(reloaded) return;
      reloaded = true;
      location.reload();
    });
  }).catch(()=>{});
}
try{ $('fileInput').disabled = true; }catch(e){}
if($('chat')){
  $('chat').addEventListener('scroll', () => { window.STICK = nearBottom(); }, {passive:true});
}
function checkWebVer(){
  const m = /app\.js\?v=(\d+)/.exec([...document.scripts].map(x => x.src).join(' '));
  const mine = m ? m[1] : '';
  fetch('api/webver', {credentials:'same-origin'}).then(r => r.json()).then(d => {
    if(d && d.v && mine && d.v !== mine){
      let b = document.getElementById('verBar');
      if(!b){
        b = document.createElement('div');
        b.id = 'verBar';
        b.innerHTML = '界面有新版本 <b>点此刷新</b>';
        b.onclick = () => location.reload();
        document.body.appendChild(b);
      }
    }
  }).catch(() => {});
}
setTimeout(checkWebVer, 1500);
paintMode(); bindInput(); boot(); startEvents(); loginPrefetch();
document.addEventListener('input', e => {
  if(e.target && e.target.id === 'qInput'){
    clearTimeout(Q_TIMER); Q_TIMER = setTimeout(doSearch, 300);
  }
});
/* 只有列表/会话两屏需要跟着宽度重排；新建与登录页不要被键盘弹出的 resize 切走 */
window.addEventListener('resize', () => {
  if(SCREEN === 's-new' || SCREEN === 's-login') return;
  if(CUR) show('s-chat'); else show('s-list');
});
setInterval(() => { if(CUR && JOB && JOB.status === 'running') renderBrief(); }, 1000);
