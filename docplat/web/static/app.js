// 校对工作台前端 —— 数据全部来自 /api/*，不做任何本地状态持久化（除左栏宽度）
const $ = s => document.querySelector(s);

const S = {
  doc: '', kind: 'review', limit: 50, offset: 0, total: 0,
  pages: [], cur: null, data: null, editing: false, zoomed: false,
  counts: null, run: null,
};

const LABELS = {
  pending: '未处理', running: '处理中', done: '已识别',
  failed: '失败', review: '待人工',
};

function esc(s) {
  return (s || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
}

function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = 'toast'; }, isErr ? 5200 : 2600);
}

async function api(path, opt) {
  const r = await fetch(path, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || (d.message || `HTTP ${r.status}`));
  return d;
}

// ---------- 文档 ----------

async function loadDocs() {
  const d = await api('/api/docs');
  const sel = $('#docsel');
  if (!d.docs.length) {
    sel.innerHTML = '<option>（无文档）</option>';
    $('#list').innerHTML = '<div class="emptybox"><div class="big">还没有文档</div>' +
      '<span class="hint">点右上角「导入」，填图片文件夹路径即可<br>' +
      '（命令行也行：plat.bat ingest --doc 名 --src 图片目录）</span></div>';
    return;
  }
  const saved = localStorage.getItem('docplat.doc');
  // 下拉里直接带出每卷进度：一眼看出哪几卷还没跑，不用一卷卷点开看
  sel.innerHTML = d.docs.map(x => {
    const c = x.counts || {};
    const p = c.pending || 0;
    let tag = '';
    if (p) tag = ` · 待跑 ${p}`;
    else if (c.escalated) tag = ` · 待VL ${c.escalated}`;
    else if (c.failed) tag = ` · 失败 ${c.failed}`;
    return `<option value="${x.id}">${esc(x.name)} · ${x.pages} 页${tag}</option>`;
  }).join('');
  S.doc = d.docs.some(x => x.id === saved) ? saved : d.docs[0].id;
  sel.value = S.doc;
  applyCounts(d.docs.find(x => x.id === S.doc).counts);
}

function applyCounts(c) {
  if (!c) return;
  S.counts = c;
  for (const k of ['review', 'escalated', 'failed', 'lowscore', 'sample', 'all']) {
    const el = $('#n-' + k);
    if (el) el.textContent = c[k];
  }
  $('#counter').textContent =
    `共 ${c.all} 页 · 待人工 ${c.review} · 升级 ${c.escalated} · 失败 ${c.failed} · 已识别 ${c.done}`;
  $('#counter').className = 'badge' + (c.review + c.failed ? ' warn' : '');
  // 待人工/失败非空时，标签数字标红，扫一眼就知道还有活
  $('#tabs button[data-kind="review"]').classList.toggle('hot', c.review > 0);
  $('#tabs button[data-kind="escalated"]').classList.toggle('hot', c.escalated > 0);
  $('#tabs button[data-kind="failed"]').classList.toggle('hot', c.failed > 0);
  updateRunBtn();
}

// ---------- 队列 ----------

async function loadQueue(reset, autoSelect = true) {
  if (reset) S.offset = 0;
  const q = $('#q').value.trim();
  // 抽检队列每次随机取 20 页，翻页没有意义
  const limit = S.kind === 'sample' ? 20 : S.limit;
  const d = await api(`/api/queue?doc=${encodeURIComponent(S.doc)}&kind=${S.kind}` +
    `&limit=${limit}&offset=${S.offset}&q=${encodeURIComponent(q)}`);
  S.pages = d.pages;
  S.total = d.total;
  renderList();
  updatePager();
  // 当前页不在新队列里：默认跳到第一条，免得右栏和左栏对不上。
  // 但调用方准备自己指定下一页时（autoSelect=false）就别抢，
  // 否则「核对完自动跳下一条」会连跳两级。
  if (!S.pages.some(p => p.hash === S.cur)) {
    if (!S.pages.length) clearPanes();
    else if (autoSelect) select(S.pages[0].hash);
  }
}

function renderList() {
  const box = $('#list');
  if (!S.pages.length) {
    const c = S.counts || {};
    const judged = (c.done || 0) + (c.review || 0) + (c.failed || 0);
    let big = '没有符合条件的页', hint = '换个队列或清掉筛选条件试试。';
    if (c.all && judged === 0) {
      big = `这 ${c.all} 页还没识别`;
      hint = '点右上角「开始识别」，它会按页序把整本跑一遍。';
    } else if (S.kind === 'review') {
      big = '这个队列已经清空了';
      hint = '机器判为可疑的页都已核对。切到「全部」可以回看任何一页。';
    }
    box.innerHTML = `<div class="emptybox">
      <div class="big">${big}</div><span class="hint">${hint}</span></div>`;
    return;
  }
  box.innerHTML = S.pages.map(p => {
    const dotCls = p.status === 'done' ? 'ok'
      : p.status === 'review' ? 'warn'
        : p.status === 'failed' ? 'bad' : '';
    const mark = p.verified
      ? '<span class="ok" title="已核对">✓</span>'
      : p.status === 'failed'
        ? '<span class="bad" title="失败">!</span>' : '';
    const tag = p.channel === 'vl' ? '<span class="tag vl">VL</span>'
      : p.channel === 'glm' ? '<span class="tag glm">GLM</span>' : '';
    return `<div class="item${p.hash === S.cur ? ' active' : ''}"
        data-hash="${p.hash}" onclick="select('${p.hash}')"
        title="${esc(p.src)}${p.err ? '｜' + esc(p.err) : ''}">
      <span class="dot ${dotCls}"></span>
      <span class="pid">p${String(p.page_no).padStart(4, '0')}</span>
      <span class="fname">${esc(p.src)}</span>${tag}${mark}</div>`;
  }).join('');
}

function updatePager() {
  if (S.kind === 'sample') {
    $('#pageinfo').textContent = `随机 ${S.pages.length} 页 · 从 ${S.total} 页里抽`;
    $('#prevpg').disabled = true;
    $('#nextpg').disabled = false;
    $('#nextpg').title = '换一批';
    return;
  }
  $('#nextpg').title = '下一页';
  const from = S.total ? S.offset + 1 : 0;
  const to = S.offset + S.pages.length;
  $('#pageinfo').textContent = `${from}–${to} / ${S.total}`;
  $('#prevpg').disabled = S.offset <= 0;
  $('#nextpg').disabled = S.offset + S.pages.length >= S.total;
}

function markActive(hash) {
  document.querySelectorAll('.item').forEach(el => {
    el.classList.toggle('active', el.dataset.hash === hash);
  });
  const el = document.querySelector(`.item[data-hash="${hash}"]`);
  if (el) el.scrollIntoView({ block: 'nearest' });
}

// ---------- 单页 ----------

function clearPanes() {
  S.cur = null; S.data = null;
  $('#imgwrap').innerHTML = '<div class="empty">选择左侧任一页开始</div>';
  $('#text').innerHTML = '<div class="empty">—</div>';
  $('#text').style.display = 'block';
  $('#editor').style.display = 'none';
  $('#statbar').innerHTML = '—';
  $('#state').textContent = '—';
  $('#imginfo').textContent = '—';
  $('#verifybtn').style.display = 'none';
}

async function select(hash) {
  S.cur = hash; S.editing = false; S.zoomed = false;
  history.replaceState(null, '', `#kind=${S.kind}&hash=${hash}`);
  $('#editor').style.display = 'none'; $('#text').style.display = 'block';
  $('#savebtn').style.display = 'none'; $('#editbtn').textContent = '编辑';
  $('#zoombtn').textContent = '放大'; $('#imgwrap').classList.remove('zoom');
  markActive(hash);

  $('#imgwrap').innerHTML = `<img src="/img/${S.doc}/${hash}?t=${Date.now()}" alt="">`;
  $('#imgopen').href = `/img/${S.doc}/${hash}`;
  $('#imginfo').textContent = '加载中…';

  let d;
  try { d = await api(`/api/page?doc=${S.doc}&hash=${hash}`); }
  catch (e) { toast('读取失败：' + e.message, true); return; }
  S.data = d;

  $('#imginfo').textContent =
    `p${String(d.page_no).padStart(4, '0')} · ${d.src}` +
    (d.width ? ` · ${d.width}×${d.height}` : '');
  $('#state').textContent = LABELS[d.status] || d.status;

  // 显示与编辑都用渲染版：模型原始输出里的 HTML 表格会摊成分行文字，
  // 否则复核时看到的是一堆 <td style='...'>。正文文件仍是原始输出。
  const text = (d.render ?? d.text ?? '');
  $('#editor').value = text;
  $('#text').innerHTML = text.trim() ? esc(text)
    : `<div class="empty">这一页没有机器结果。<br>
       ${esc(d.err || '可能是空白页、影像页，或识别失败。')}<br>
       <br>可以点右上「重跑本页」再试，或直接「编辑」手写。</div>`;

  const verified = !!d.verified_at;
  $('#verifybtn').style.display = 'inline-block';
  $('#verifybtn').textContent = verified ? '取消核对' : '✓ 已核对';
  $('#verifybtn').className = 'mini' + (verified ? '' : ' primary');
  $('#verifybtn').title = verified
    ? `本页在 ${d.verified_at} 标记为已核对（${d.verified_verdict === 'edited' ? '人工改过' : '原样确认'}），点一下撤销`
    : '确认这页看过了：从「待人工」队列里移除';

  const why = d.channel === 'vl'
    ? '这一页规划给 PaddleOCR-VL，需占满显存，请在命令行跑'
    : d.channel === 'human'
      ? '这一页按版式规则直接转人工，重跑结果不会变，请对照原图手写'
      : '用当前渠道重跑这一页，其他页不受影响';
  $('#rerunbtn').disabled = d.channel === 'vl' || d.channel === 'human';
  $('#rerunbtn').title = why;

  renderStat(d);
}

function renderStat(d) {
  const bar = [];
  bar.push(`<span class="pill mono">p${String(d.page_no).padStart(4, '0')}</span>`);
  bar.push(`<span class="pill">${esc(d.layout || '未判版面')}</span>`);
  if (d.engine) bar.push(`<span class="pill">${esc(d.engine)}</span>`);
  bar.push(`<b>${d.chars || 0}</b> 字`);
  if (d.sec) bar.push(`<span class="pill mono">${d.sec.toFixed(1)}s</span>`);

  if (d.status === 'review') {
    bar.push(`<span class="pill amber">${esc(d.err || '机器判为可疑')}</span>`);
  } else if (d.status === 'failed') {
    bar.push(`<span class="pill red">失败：${esc(d.err || '未知')}</span>`);
    if (d.retries) bar.push(`<span class="hint">已重试 ${d.retries} 次</span>`);
  } else if (d.status === 'done' && d.score != null && d.score < 1) {
    bar.push(`<span class="pill amber">自评 ${(d.score * 100).toFixed(0)}%</span>`);
  }
  if (d.verified_at) {
    bar.push(`<span class="pill green" title="${esc(d.verified_note || '')}">` +
      `${d.verified_verdict === 'edited' ? '已修改' : '✓ 已核对'} · ${d.verified_at}</span>`);
  }
  bar.push(`<span class="hint">${d.chars ? '对照左图核对，改完点保存' : '无正文'}</span>`);
  $('#statbar').innerHTML = bar.join('');
}

// ---------- 操作 ----------

function toggleEdit() {
  if (!S.cur) return;
  S.editing = !S.editing;
  $('#text').style.display = S.editing ? 'none' : 'block';
  $('#editor').style.display = S.editing ? 'block' : 'none';
  $('#editbtn').textContent = S.editing ? '取消' : '编辑';
  $('#savebtn').style.display = S.editing ? 'inline-block' : 'none';
  if (S.editing) { $('#editor').focus(); $('#editor').style.height = '100%'; }
}

async function savePage() {
  if (!S.cur || !S.editing) return;
  const text = $('#editor').value;
  try {
    const d = await api('/api/page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hash: S.cur, text }),
    });
    toast(`已保存（${d.chars} 字）并记为已修改`);
    await afterResolved();
  } catch (e) { toast('保存失败：' + e.message, true); }
}

async function toggleVerified() {
  if (!S.cur || !S.data) return;
  const on = !S.data.verified_at;
  try {
    const d = await api('/api/verify', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hash: S.cur, verified: on }),
    });
    toast(on ? '已核对，从待人工队列移除' : '已撤销核对');
    applyCounts(d.counts);
    if (on && (S.kind === 'review' || S.kind === 'lowscore')) await afterResolved();
    else { await refresh(); await select(S.cur); }
  } catch (e) { toast('操作失败：' + e.message, true); }
}

// 一页处理完（核对/改完）之后：留在同一条道上继续，别回头点左栏。
// 先把「下一条是谁」记下来再去刷新队列 —— 刷新会让当前页从队列里消失，
// 那时再算下标就错位了，会连跳两级。
async function afterResolved() {
  const i = S.pages.findIndex(p => p.hash === S.cur);
  const nextHash = (i >= 0 && i + 1 < S.pages.length) ? S.pages[i + 1].hash : '';
  const hasMore = S.offset + S.pages.length < S.total;

  await refresh(false);
  if (nextHash && S.pages.some(p => p.hash === nextHash)) {
    select(nextHash);
  } else if (hasMore) {
    S.offset += S.limit;
    await loadQueue();                    // 会自动选第一条
  } else if (S.pages.length) {
    select(S.pages[0].hash);
  } else {
    clearPanes();
  }
}

async function rerunPage() {
  if (!S.cur) return;
  if (!confirm('重跑这一页？其他页结果不受影响。')) return;
  const btn = $('#rerunbtn');
  btn.disabled = true; btn.textContent = '重跑中…';
  try {
    const d = await api('/api/rerun', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hash: S.cur }),
    });
    const st = d.stat || {};
    toast(`重跑完成：${st.done || 0} 页正常 / ${st.review || 0} 页待人工 / ${st.failed || 0} 失败`);
    await refresh();
    await select(S.cur);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.textContent = '重跑本页';
    btn.disabled = false;
  }
}

function zoom() {
  S.zoomed = !S.zoomed;
  $('#imgwrap').classList.toggle('zoom', S.zoomed);
  $('#zoombtn').textContent = S.zoomed ? '适应' : '放大';
}

async function refresh(autoSelect = true) {
  const d = await api('/api/docs');
  const me = d.docs.find(x => x.id === S.doc);
  if (me) applyCounts(me.counts);
  await loadQueue(false, autoSelect);
}

// ---------- 事件绑定 ----------

$('#docsel').addEventListener('change', e => {
  S.doc = e.target.value;
  localStorage.setItem('docplat.doc', S.doc);
  S.cur = null;
  loadDocs().then(() => loadQueue(true)).then(() => pollRun());
});

$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-kind]');
  if (!b) return;
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  S.kind = b.dataset.kind;
  S.cur = null;
  history.replaceState(null, '', '#kind=' + S.kind);
  loadQueue(true);
});

$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') loadQueue(true); });
$('#q').addEventListener('input', e => { if (!e.target.value) loadQueue(true); });

$('#prevpg').onclick = () => { S.offset = Math.max(0, S.offset - S.limit); loadQueue(); };
$('#nextpg').onclick = () => {
  if (S.kind === 'sample') { loadQueue(true); return; }   // 换一批
  S.offset += S.limit; loadQueue();
};
$('#exportbtn').onclick = async () => {
  $('#exportbtn').disabled = true;
  try {
    const d = await api(`/api/export?doc=${encodeURIComponent(S.doc)}`);
    toast(`已导出 ${d.path}（${(d.size / 1024).toFixed(0)} KB）`);
  } catch (e) {
    toast('导出失败：' + e.message, true);
  } finally {
    $('#exportbtn').disabled = false;
  }
};

// ---------- 识别（跑批走子进程，页面只负责按电钮）----------

let runTimer = null;

function fmtDur(sec) {
  if (!isFinite(sec) || sec <= 0) return '';
  const m = Math.round(sec / 60);
  if (m < 1) return '不到 1 分钟';
  if (m < 60) return `${m} 分钟`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

function updateRunBtn() {
  const b = $('#runbtn');
  const c = S.counts || {};
  const running = !!(S.run && S.run.running);
  const judged = (c.done || 0) + (c.review || 0) + (c.failed || 0);
  const allDone = !!c.all && judged >= c.all;

  b.classList.toggle('busy', running);
  if (!running && S.run && S.run.queued) {   // 下一批已排队，别让点击插队
    b.disabled = true;
    b.textContent = '已排队';
    b.title = '当前批次结束后会自动接着跑下一阶段';
    return;
  }
  if (running) {                    // 跑的时候用进度条上的暂停/停止，这里不接点击
    b.disabled = true;
    b.textContent = S.run.paused ? '已暂停'
      : (S.run.batch ? '整批运行中…' : '识别中…');
    return;
  }
  if (c.escalated > 0) {
    b.disabled = false;
    b.textContent = `换 VL 重跑 ${c.escalated}`;
    b.title = '结构闸门判为分格条目的页，需要换 PaddleOCR-VL；跑之前要先停掉 GLM 服务';
  } else if (allDone) {
    b.disabled = true;              // 没活了就别给一个点了没反应的按钮
    b.textContent = '全部已识别';
    b.title = '这份文档没有待识别的页了';
  } else {
    b.disabled = false;
    b.textContent = '开始识别';
    b.title = '按页序识别所有还没处理的页';
  }
}

function renderRun(d) {
  const bar = $('#runbar');
  const c = d.counts || S.counts || {};
  const total = c.all || 0;
  // 升级到 vl 的页也算「这一轮已经走过」——它们已经在排队等换模型，
  // 不算进来的话进度条会永远卡在 60% 出头不动，看着像卡死。
  const done = (c.done || 0) + (c.review || 0) + (c.failed || 0) + (c.escalated || 0);
  const pct = total ? Math.min(100, Math.round(done / total * 100)) : 0;
  const paused = !!d.paused;

  if (!d.running) {
    // 有下一批在排队（glm 跑完等 vl 起来）：别让进度条凭空消失
    if (d.queued && d.queued.stages) {
      bar.style.display = 'flex';
      bar.classList.add('paused');
      $('#rfill').style.width = '0%';
      $('#rlabel').textContent =
        `已排队：当前批次结束后自动跑 ${d.queued.stages.join(' → ')}`;
      $('#retx').textContent = d.queued.since ? `从 ${d.queued.since} 开始等` : '';
      $('#pausebtn').textContent = '暂停';
      return;
    }
    bar.style.display = 'none';
    bar.classList.remove('paused');
    return;
  }
  bar.style.display = 'flex';
  bar.classList.toggle('paused', paused);
  $('#rfill').style.width = pct + '%';

  // 跑的是另一份文档（或整批）时，进度条要标出来，否则顶栏数字和当前文档对不上
  let head = '识别中';
  if (d.batch) head = `整批${d.stage_label || ''}中`;
  else if (d.doc && d.doc !== S.doc) head = `识别中（${d.doc}）`;
  let label = (paused ? '已暂停' : head) + ` ${done}/${total} · ${pct}%`;
  // 剩余时间只在跑起来之后才有意义：前几页的速率噪声太大，估出来反而误导
  if (!paused && done >= 3 && d.sec > 3) {
    const eta = (total - done) / (done / d.sec);
    label += ' · 剩 ' + fmtDur(eta);
  }
  // 后面排着阶段（glm 完自动接 vl）：写进进度文字，省得以为跑完就没事了
  if (d.queued && d.queued.stages) {
    const next = d.queued.stages.filter(s => s !== d.channel);
    if (next.length) label += ` · 后续 ${next.join('→')}`;
  }
  $('#rlabel').textContent = label;
  $('#retx').textContent = d.tail || '';
  $('#pausebtn').textContent = paused ? '继续' : '暂停';
}

async function pollRun() {
  let d;
  try { d = await api('/api/run'); } catch (e) { return; }
  const prev = S.run || {};
  S.run = d;
  // 统计只认当前选中的文档：后台在跑卷二时，切到卷一不能拿卷二的数字顶上去
  if (d.counts && (!d.doc || d.doc === S.doc)) {
    S.counts = d.counts;
    applyCounts(d.counts);
  }
  renderRun(d);
  updateRunBtn();

  if (prev.running && !d.running) {   // 刚跑完：刷新统计和队列
    await loadDocs();
    await loadQueue(false);
    // 后面还有排队的阶段（glm 完接 vl）就别停轮询，否则等不到 vl 起来
    if (d.queued) {
      toast('这一阶段跑完了，下一阶段排队中，会自动接上');
      return;
    }
    clearInterval(runTimer);
    runTimer = null;
    const c = d.counts || {};
    if (prev.batch) {
      toast('整批跑完了。各卷的 txt 已重新导出，'
          + '「换 VL 重跑 N」的按钮会告诉你还剩多少页要换模型');
    } else {
      const bad = d.code ? `（退出码 ${d.code}）` : '';
      toast(`识别结束${bad}：已识别 ${c.done || 0} · 待人工 ${c.review || 0}` +
        ` · 升级 ${c.escalated || 0} · 失败 ${c.failed || 0}`,
        !!d.code || (c.failed || 0) > 0);
    }
  }
}

function startPolling() {
  clearInterval(runTimer);
  runTimer = setInterval(pollRun, 2000);
  pollRun();
}

$('#runbtn').onclick = async () => {
  if (S.run && S.run.running) return;
  const c = S.counts || {};
  const channel = c.escalated > 0 ? 'vl' : 'glm';
  if (channel === 'vl' &&
      !confirm('换 VL 要占满显存：先把 GLM 服务停掉，再来点确定。\n已经停了就继续。')) return;
  $('#runbtn').disabled = true;
  try {
    await api('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, channel }),
    });
    toast(channel === 'vl' ? '已开始换 VL 重跑' : '已开始识别');
    startPolling();
  } catch (e) {
    toast(e.message, true);
    $('#runbtn').disabled = false;
  }
};

$('#pausebtn').onclick = async () => {
  const paused = !!(S.run && S.run.paused);
  try {
    await api(paused ? '/api/run/resume' : '/api/run/pause', { method: 'POST' });
    toast(paused ? '已继续' : '会在当前这一页跑完后停下');
    await pollRun();
  } catch (e) {
    toast(e.message, true);
  }
};

$('#stopbtn').onclick = async () => {
  if (!confirm('停止识别？已经跑完的页会保留，剩下的下次接着跑。')) return;
  try { await api('/api/run/stop', { method: 'POST' }); } catch (e) { /* 已经停了 */ }
  await pollRun();
};

// ---------- 导入 ----------

$('#importbtn').onclick = () => {
  $('#impsrc').value = localStorage.getItem('docplat.imp') || '';
  $('#impdoc').value = '';
  $('#impok').disabled = false;
  $('#impok').textContent = '开始导入';
  $('#impdlg').showModal();
  setTimeout(() => $('#impsrc').focus(), 40);
};

$('#impform').onsubmit = async (e) => {
  e.preventDefault();
  if (e.submitter && e.submitter.value === 'cancel') { $('#impdlg').close(); return; }
  const src = $('#impsrc').value.trim();
  if (!src) { toast('先填文件夹路径', true); return; }
  const ok = $('#impok');
  ok.disabled = true;
  ok.textContent = '扫描中…';
  try {
    // 导入是同步的（要算 242 个文件的 hash），几百页以内秒级返回；
    // 上千页要等，所以按钮先锁住，别让人连点重复提交。
    const d = await api('/api/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ src, doc_id: $('#impdoc').value.trim() }),
    });
    localStorage.setItem('docplat.imp', src);
    localStorage.setItem('docplat.doc', d.doc_id);
    $('#impdlg').close();
    await loadDocs();
    // 刚导入的书一页都还没跑，「待人工」必然是空的 —— 直接切到「全部」，
    // 让人看到 266 页确实进来了，而不是对着一个空列表发愣。
    S.kind = 'all';
    document.querySelectorAll('#tabs button')
      .forEach(x => x.classList.toggle('on', x.dataset.kind === 'all'));
    await loadQueue(true);
    await pollRun();
    toast(`已导入 ${d.pages} 页（重复 ${d.dup} 张跳过）· 点「开始识别」开跑`);
  } catch (err) {
    toast('导入失败：' + err.message, true);
    ok.disabled = false;
    ok.textContent = '开始导入';
  }
};

document.addEventListener('keydown', e => {
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) {
    if (e.key === 'Escape' && e.target.id === 'editor') toggleEdit();
    if ((e.ctrlKey || e.metaKey) && e.key === 's' && e.target.id === 'editor') {
      e.preventDefault(); savePage();
    }
    return;
  }
  const i = S.pages.findIndex(p => p.hash === S.cur);
  if (e.key === 'ArrowDown' || e.key === 'j') {
    e.preventDefault();
    if (i < S.pages.length - 1) select(S.pages[i + 1].hash);
    else if (S.pages.length && S.offset + S.pages.length < S.total) {
      S.offset += S.limit; loadQueue();
    }
  } else if (e.key === 'ArrowUp' || e.key === 'k') {
    e.preventDefault();
    if (i > 0) select(S.pages[i - 1].hash);
    else if (S.offset > 0) { S.offset = Math.max(0, S.offset - S.limit); loadQueue(); }
  } else if (e.key === 'Enter') { e.preventDefault(); toggleEdit(); }
  else if (e.key === 'Escape' && S.editing) toggleEdit();
  else if (e.key === 'v' && S.data) toggleVerified();
  else if (e.key === 'z') zoom();
});

// 左栏宽度：拖 splitter 调整，记在 localStorage
(function () {
  const MIN = 220, MAX = 640;
  const root = document.documentElement;
  const saved = parseInt(localStorage.getItem('asideW') || '', 10);
  if (saved >= MIN && saved <= MAX) root.style.setProperty('--aside-w', saved + 'px');
  const sp = $('#splitter');
  let dragging = false, startX = 0, startW = 0;
  sp.addEventListener('mousedown', e => {
    dragging = true; startX = e.clientX;
    startW = document.querySelector('aside').getBoundingClientRect().width;
    sp.classList.add('dragging'); document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const w = Math.max(MIN, Math.min(MAX, startW + (e.clientX - startX)));
    root.style.setProperty('--aside-w', w + 'px');
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false; sp.classList.remove('dragging'); document.body.style.cursor = '';
    const w = parseInt(getComputedStyle(root).getPropertyValue('--aside-w'), 10);
    if (w) localStorage.setItem('asideW', String(w));
  });
})();

loadDocs()
  .then(() => {
    // 地址栏里的 #kind=review&hash=xxx 决定打开时停在哪
    const p = new URLSearchParams(location.hash.slice(1));
    const k = p.get('kind');
    if (['review', 'failed', 'lowscore', 'sample', 'all'].includes(k)) {
      S.kind = k;
      document.querySelectorAll('#tabs button')
        .forEach(x => x.classList.toggle('on', x.dataset.kind === k));
    }
    return loadQueue(true, false).then(() => {
      const h = p.get('hash');
      if (h && S.pages.some(x => x.hash === h)) select(h);
      else if (S.pages.length) select(S.pages[0].hash);
    });
  })
  .catch(e => toast('加载失败：' + e.message, true))
  .then(() => {
    // 打开页面时如果后台正在跑，接上进度条（刷新页面不该丢掉正在跑的活）；
    // 没在跑就问一句上一批的结果——不然关了浏览器回来两眼一抹黑。
    pollRun().then(() => {
      if (S.run && S.run.running) { startPolling(); return; }
      return api('/api/last-batch').then(d => {
        if (!d || !d.at || !d.docs || !d.docs.length) return;
        const s = d.docs.reduce((a, x) => ({
          all: a.all + (x.all || 0),
          done: a.done + (x.done || 0),
          vl: a.vl + (x.escalated || 0),
        }), { all: 0, done: 0, vl: 0 });
        toast(`上次批量 ${d.at} 跑完：${d.docs.length} 卷 · 已识别 ${s.done}/${s.all}`
          + (s.vl ? ` · 待换模型 ${s.vl} 页` : ''));
      }).catch(() => {});
    });
  });
