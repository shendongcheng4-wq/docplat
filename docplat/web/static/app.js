// 校对工作台前端 —— 数据全部来自 /api/*，不做任何本地状态持久化（除左栏宽度）
const $ = s => document.querySelector(s);

const S = {
  doc: '', kind: 'review', limit: 50, offset: 0, total: 0,
  pages: [], cur: null, data: null, editing: false,
  // 缩放：true = 原始像素（图比栏宽就横向滚动），false = 适应栏宽。
  // **默认适应栏宽**：族谱原图 1900+ 宽，默认原始像素会把正文栏挤成 0 宽
  // （2026-11-06 实测 1440/1920 下右栏整栏看不见）。要看细节再点「原始」。
  // 偏好记在 localStorage，翻页不再重置。
  zoomed: localStorage.getItem('docplat.zoom') === '1',
  counts: null, run: null,
  sel: new Set(), anchor: -1,        // 批量勾选；anchor 是 Shift 选一段的起点
  qmode: localStorage.getItem('docplat.qmode') || 'meta',   // meta=页号/文件名, text=正文
  searching: false, terms: [],
};

const LABELS = {
  pending: '未处理', running: '处理中', done: '已识别',
  failed: '失败', review: '待人工',
};

// 引号也要转义：备注、片段会进 HTML 属性（title），只处理 &<> 会把属性截断
function esc(s) {
  return (s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function pad4(n) { return String(n).padStart(4, '0'); }

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
  // 标签页标题跟着文档走：开多个标签来回切时，一眼知道哪个是哪本书
  const me = d.docs.find(x => x.id === S.doc);
  document.title = (me ? me.name + ' · ' : '') + '文档校对工作台';
  applyCounts(me.counts);
}

function applyCounts(c) {
  if (!c) return;
  S.counts = c;
  for (const k of ['review', 'escalated', 'failed', 'lowscore', 'sample', 'all']) {
    const el = $('#n-' + k);
    if (el) el.textContent = c[k];
  }
  // 顶栏只说「一共多少、已识别多少、还剩多少没跑」。各队列的数字在左栏标签上，
  // 两边各放一遍只会让人多读一遍（以前那串「共 242 页 · 待人工 2 · 升级 0…」就是）。
  const rest = Math.max(0, (c.all || 0) - (c.done || 0) - (c.review || 0)
    - (c.failed || 0) - (c.escalated || 0));
  $('#counter').textContent = `${c.all || 0} 页 · 已识别 ${c.done || 0}`
    + (rest ? ` · 待跑 ${rest}` : '');
  $('#counter').className = 'counter' + (c.review + c.failed ? ' busy' : '');
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
  S.searching = S.qmode === 'text' && !!q;

  // 正文搜索走另一条接口：它扫的是 md 正文，不是库里的文件名/页号
  if (S.searching) {
    const d = await api(`/api/search?doc=${encodeURIComponent(S.doc)}` +
      `&q=${encodeURIComponent(q)}`);
    S.pages = d.pages;
    S.total = d.total;
    S.terms = d.terms || [];
    renderList();
    updatePager();
    if (!S.pages.some(p => p.hash === S.cur)) {
      if (!S.pages.length) clearPanes();
      else if (autoSelect) select(S.pages[0].hash);
    }
    return;
  }

  S.terms = [];
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

// 片段里把命中的词包成 <mark>。先转义再整段替换，避免用户输入的 < > 破坏结构。
function hl(s, terms) {
  const out = esc(s);
  const ts = (terms || []).filter(Boolean).map(t =>
    esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!ts.length) return out;
  try {
    return out.replace(new RegExp(`(${ts.join('|')})`, 'gi'), '<mark>$1</mark>');
  } catch (e) {
    return out;
  }
}

function renderList() {
  const box = $('#list');
  box.classList.toggle('searching', S.searching);
  if (!S.pages.length) {
    const c = S.counts || {};
    const judged = (c.done || 0) + (c.review || 0) + (c.failed || 0);
    let big = '没有符合条件的页', hint = '换个队列或清掉筛选条件试试。';
    if (S.searching) {
      big = '正文里没有这句话';
      hint = '换个词试试。多个词用空格分开，要求同时出现。';
    } else if (c.all && judged === 0) {
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
  box.innerHTML = S.pages.map((p, i) => {
    const dotCls = p.status === 'done' ? 'ok'
      : p.status === 'review' ? 'warn'
        : p.status === 'failed' ? 'bad' : '';
    // 核对过 = 一枚朱印；失败由状态点表示，不再多挂一个「!」
    const mark = p.verified
      ? '<span class="seal" title="已核对">校</span>' : '';
    const tag = p.channel === 'vl' ? '<span class="tag vl">VL</span>'
      : p.channel === 'glm' ? '<span class="tag glm">GLM</span>' : '';
    // 有备注的页标一个小方块，鼠标停上去看内容
    const note = p.note
      ? `<span class="note" title="备注：${esc(p.note)}"></span>` : '';
    // 字数比文件名有用：0 字多半是空白页，个位数多半只剩页脚/署名，
    // 扫一眼就能挑出该看的那几页。未跑过的页不给数字（那是「还没数」不是「0 字」）。
    const known = p.status !== 'pending' && p.status !== 'running';
    const n = p.chars || 0;
    const cls = !known ? '' : n === 0 ? ' z' : n < 10 ? ' q' : '';
    const chars = `<span class="chars${cls}">${known ? n : ''}</span>`;
    const on = S.sel.has(p.hash);
    const two = p.snip !== undefined;
    const snip = two ? `<span class="snip">${hl(p.snip, S.terms)}</span>` : '';
    const title = p.snip !== undefined
      ? `p${pad4(p.page_no)} · 命中 ${p.hits} 处`
      : `${p.src}${p.err ? '｜' + p.err : ''}`;
    return `<div class="item${p.hash === S.cur ? ' active' : ''}${on ? ' sel' : ''}${two ? ' two' : ''}"
        data-hash="${p.hash}" onclick="select('${p.hash}')" title="${esc(title)}">
      <input type="checkbox" class="ck"${on ? ' checked' : ''}
        title="勾选这一页（Shift 点选一段）"
        onclick="event.stopPropagation(); toggleSel('${p.hash}', ${i}, event.shiftKey)">
      <span class="dot ${dotCls}"></span>
      <span class="pid">p${pad4(p.page_no)}</span>
      <span class="fname">${esc(p.src)}</span>${chars}${tag}${note}${mark}${snip}</div>`;
  }).join('');
  renderBatchBar();
}

// ---------- 批量勾选 ----------
// 待人工最多的一卷有 43 页，一页一次点击、点完还要等自动跳下一条，
// 核一本书就是几百次点击。勾选后一次提交，把这件事压成两三下。

function toggleSel(hash, i, shift) {
  if (shift && S.anchor >= 0 && S.anchor !== i) {
    // Shift 选一段：从上次点击的位置到当前位置，全部设成「跟当前这条一样」
    const want = !S.sel.has(hash);
    const [a, b] = S.anchor < i ? [S.anchor, i] : [i, S.anchor];
    for (let k = a; k <= b; k++) {
      const h = S.pages[k] && S.pages[k].hash;
      if (!h) continue;
      if (want) S.sel.add(h); else S.sel.delete(h);
    }
  } else if (S.sel.has(hash)) {
    S.sel.delete(hash);
  } else {
    S.sel.add(hash);
  }
  S.anchor = i;
  // 只改这两处样式，不重排整列（重排会让滚动位置跳）
  document.querySelectorAll('#list .item').forEach(el => {
    const h = el.dataset.hash;
    el.classList.toggle('sel', S.sel.has(h));
    const ck = el.querySelector('.ck');
    if (ck) ck.checked = S.sel.has(h);
  });
  renderBatchBar();
}

function renderBatchBar() {
  const n = S.sel.size;
  $('#batchbar').style.display = n ? 'block' : 'none';
  if (!n) return;
  $('#bcount').textContent = `已选 ${n} 页`;
  // 按钮上不重复页数 —— 左边「已选 N 页」已经写了，三个按钮各带一遍数字
  // 在 320px 的左栏里正好顶到边（2026-11-06 实测贴边被裁）。
  $('#bverify').textContent = '核对';
  $('#bvl').textContent = '转 VL';
  $('#bblank').textContent = '标为空白';
  // 整批都勾上了就把按钮改成反向操作，否则点了没反应会以为坏了
  const all = S.pages.length && S.pages.every(p => S.sel.has(p.hash));
  $('#ballbtn').textContent = all ? '取消全选' : `全选本页 ${S.pages.length}`;
}

function clearSel() {
  S.sel.clear(); S.anchor = -1;
  document.querySelectorAll('#list .item').forEach(el => {
    el.classList.remove('sel');
    const ck = el.querySelector('.ck');
    if (ck) ck.checked = false;
  });
  renderBatchBar();
}

async function batchDo(action) {
  const n = S.sel.size;
  if (!n) return;
  const hashes = [...S.sel];
  if (action === 'blank' &&
      !confirm(`把这 ${n} 页标为空白页？\n\n正文会被清空、记为已核对；` +
        `原文备份到 data/_quarantine，点错了能找回。`)) return;
  if (action === 'to_vl' &&
      !confirm(`把这 ${n} 页转成「等 PaddleOCR-VL 重跑」？\n\n` +
        `它们会离开待人工队列、进「升级」队列（正文先留着，新结果写盘才覆盖）。\n` +
        `转完点顶部「换 VL 重跑 N」，会先帮你停掉 GLM 再开跑。`)) return;
  try {
    const d = await api('/api/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hashes, action }),
    });
    const what = action === 'blank' ? '标为空白'
      : action === 'unverify' ? '撤销核对'
        : action === 'to_vl' ? '转 VL' : '核对';
    toast(`已${what} ${d.done} 页` + (d.skipped.length ? `（${d.skipped.length} 页已不存在）` : ''));
    clearSel();
    applyCounts(d.counts);
    await loadQueue(false);        // 当前页可能已经退出这个队列，自动选下一条
  } catch (e) {
    toast('批量操作失败：' + e.message, true);
  }
}

$('#bclearbtn').onclick = () => clearSel();
$('#ballbtn').onclick = () => {
  const all = S.pages.length && S.pages.every(p => S.sel.has(p.hash));
  S.pages.forEach(p => { if (all) S.sel.delete(p.hash); else S.sel.add(p.hash); });
  S.anchor = S.pages.length - 1;
  renderList();
};
$('#bverify').onclick = () => batchDo('verify');
$('#bvl').onclick = () => batchDo('to_vl');
$('#bblank').onclick = () => batchDo('blank');

function updatePager() {
  if (S.searching) {
    $('#pageinfo').textContent = `命中 ${S.total} 页`;
    $('#prevpg').disabled = true;
    $('#nextpg').disabled = true;
    $('#nextpg').title = '正文搜索不分页';
    return;
  }
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
  $('#imgwrap').innerHTML = '<div class="empty">左栏点一页看原图<br>' +
    '（↑ ↓ 也能翻）</div>';
  $('#text').innerHTML = '<div class="empty">还没选中页。<br>' +
    '这一栏会显示识别结果，和导出 txt 是同一套排版。</div>';
  $('#text').style.display = 'block';
  $('#editor').style.display = 'none';
  $('#statbar').innerHTML = '—';
  $('#state').textContent = '—';
  $('#state').className = '';
  $('#imginfo').textContent = '—';
  $('#imgzoom').textContent = '';
  $('#verifybtn').style.display = 'none';
  $('#peek').style.display = 'none';
  $('#notebtn').classList.remove('has');
}

async function select(hash) {
  S.cur = hash; S.editing = false;
  history.replaceState(null, '', `#kind=${S.kind}&hash=${hash}`);
  $('#editor').style.display = 'none'; $('#text').style.display = 'block';
  $('#savebtn').style.display = 'none'; $('#editbtn').textContent = '编辑';
  markActive(hash);

  $('#imgwrap').innerHTML = `<img src="/img/${S.doc}/${hash}?t=${Date.now()}" alt="">`;
  $('#imgopen').href = `/img/${S.doc}/${hash}`;
  $('#imginfo').textContent = '加载中…';
  $('#imgzoom').textContent = '';
  const im = $('#imgwrap img');
  if (im.complete) applyZoom(); else im.addEventListener('load', applyZoom);

  let d;
  try { d = await api(`/api/page?doc=${S.doc}&hash=${hash}`); }
  catch (e) { toast('读取失败：' + e.message, true); return; }
  S.data = d;

  $('#imginfo').textContent =
    `p${String(d.page_no).padStart(4, '0')} · ${d.src}` +
    (d.width ? ` · ${d.width}×${d.height}` : '');
  $('#state').textContent = LABELS[d.status] || d.status;
  $('#state').className = 'st-' + (d.status || 'none');

  // 只读视图用 display（后端 book_layout 排过：段首缩进、段间不空行），
  // 跟导出的 txt 是同一套排法；编辑态仍用 render 的原始分行 ——
  // 缩进是展示层的事，写回库里会变成正文里的全角空格。
  const raw = (d.render ?? d.text ?? '');
  const shown = (d.display ?? raw);
  $('#editor').value = raw;
  $('#text').innerHTML = shown.trim() ? esc(shown)
    : `<div class="empty">这一页没有机器结果。<br>
       ${esc(d.err || '可能是空白页、影像页，或识别失败。')}<br>
       <br>可以点右上「重跑本页」再试，或直接「编辑」手写。</div>`;

  const verified = !!d.verified_at;
  $('#verifybtn').style.display = 'inline-block';
  $('#verifybtn').textContent = verified ? '取消核对' : '核对';
  $('#verifybtn').className = 'mini' + (verified ? '' : ' primary');
  $('#verifybtn').title = verified
    ? `本页在 ${d.verified_at} 标记为已核对（${d.verified_verdict === 'edited' ? '人工改过' : '原样确认'}），点一下撤销`
    : '确认这页看过了：从「待人工」队列里移除';

  // 「重跑…」是个选择器：GLM 当场跑，VL 只能排进队列（要占满显存）。
  // 别在这里禁用按钮 —— 待人工里 GLM 读不了的页正是最想换个模型的。
  const cur = d.channel === 'vl' ? 'PaddleOCR-VL' : d.channel === 'human' ? '人工' : 'GLM';
  $('#rerunbtn').disabled = false;
  $('#rerunbtn').title = `重跑这一页（当前 ${cur}）：可选 GLM 当场跑，或换 PaddleOCR-VL 排进 VL 队列`;

  renderPeek(d);

  // 备注按钮：写过内容的换个颜色 —— 否则每次都得点开才知道这页有没有备注
  const nb = $('#notebtn');
  nb.classList.toggle('has', !!d.note);
  nb.title = d.note ? `备注：${d.note}` : '页级备注：不影响正文，也不改变核对状态';

  renderStat(d);
}

// 相邻页：上一页取结尾、下一页取开头。跨页接续的句子不用再回左栏翻。
function renderPeek(d) {
  const box = $('#peek');
  const pv = d.prev, nx = d.next;
  if (!pv && !nx) {
    box.style.display = 'none';
    return;
  }
  box.style.display = 'flex';
  const fill = (el, x, arrow) => {
    if (!x) {
      el.className = 'pk none';
      el.onclick = null;
      el.innerHTML = '';
      return;
    }
    el.className = 'pk';
    el.innerHTML = `<span class="who">${arrow} p${pad4(x.page_no)}</span>` +
      esc(x.peek || '（这一页没有正文）');
    el.onclick = () => select(x.hash);
  };
  fill($('#peekprev'), pv, '↑');
  fill($('#peeknext'), nx, '↓');
}

// 2026-10-30T16:21:44 → 10-30 16:21：状态栏里塞 ISO 串又长又难看
function fmtWhen(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s || '');
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : (s || '');
}

function renderStat(d) {
  // 左边固定是「这一页是什么」，右边是「核没核过、机器怎么判的」。
  // 以前全拼成一条，日期还带 T，扫视时找不到重点。
  const L = [], R = [];

  L.push(`<span class="sb-id">p${String(d.page_no).padStart(4, '0')}</span>`);
  L.push(`<span class="sb-tag">${esc(d.layout || '未判版面')}</span>`);
  if (d.engine) L.push(`<span class="sb-tag">${esc(d.engine)}</span>`);
  L.push(`<span class="sb-num"><b>${(d.chars || 0).toLocaleString('en-US')}</b> 字</span>`);
  if (d.sec) L.push(`<span class="sb-meta">${d.sec.toFixed(1)}s</span>`);

  // 编辑态才留这条提示位；空着也占一个位置的话，右栏第一项前面会多一个
  // 分隔点（`* + *::before` 不看内容），看着像是漏了个字段。
  if (S.editing) R.push('<span class="hint" id="sbtip"></span>');
  if (d.status === 'review') {
    R.push(`<span class="sb-warn">${esc(d.err || '机器判为可疑')}</span>`);
  } else if (d.status === 'failed') {
    R.push(`<span class="sb-bad">失败：${esc(d.err || '未知')}` +
      (d.retries ? ` · 重试 ${d.retries} 次` : '') + '</span>');
  } else if (d.status === 'done' && d.score != null && d.score < 1) {
    R.push(`<span class="sb-warn">自评 ${(d.score * 100).toFixed(0)}%</span>`);
  }
  if (d.verified_at) {
    R.push(`<span class="sb-ok" title="${esc(d.verified_note || '')}">` +
      `<span class="seal">校</span>` +
      `${d.verified_verdict === 'edited' ? '已修改' : '已核对'} · ${fmtWhen(d.verified_at)}</span>`);
  } else {
    R.push('<span class="hint">未核对</span>');
  }
  if (!d.chars) R.push('<span class="sb-tag">无正文</span>');

  // 分隔点显式插，不靠 `* + *::before` —— 空元素也占位，第一项前面会多一个点
  const sep = '<span class="sep"></span>';
  $('#statbar').innerHTML =
    `<div class="sb-l">${L.join(sep)}</div><div class="sb-r">${R.join(sep)}</div>`;
}

// ---------- 操作 ----------

function toggleEdit() {
  if (!S.cur) return;
  S.editing = !S.editing;
  $('#text').style.display = S.editing ? 'none' : 'block';
  $('#editor').style.display = S.editing ? 'block' : 'none';
  $('#editbtn').textContent = S.editing ? '取消' : '编辑';
  $('#savebtn').style.display = S.editing ? 'inline-block' : 'none';
  // 编辑态看到的是原始分行（没有缩进），说清楚一句，免得以为排版丢了
  if (S.data) renderStat(S.data);          // 重画一次，编辑态那条提示才会出现
  const tip = $('#sbtip');
  if (tip) tip.textContent = '编辑态显示原始分行，保存后按书排版';
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
    toast(on
      ? `已核对，从${S.kind === 'escalated' ? '升级' : '待人工'}队列移除`
      : '已撤销核对');
    applyCounts(d.counts);
    // 升级页核对 = 认了 GLM 的结果（后端会把它收成 done），跟待人工一样要往下跳，
    // 不然核对完停在一条已经不在列表里的页上，下一条还得自己点
    if (on && (S.kind === 'review' || S.kind === 'lowscore' || S.kind === 'escalated'))
      await afterResolved();
    else { await refresh(); await select(S.cur); }
  } catch (e) { toast('操作失败：' + e.message, true); }
}

// 一页处理完（核对/改完）之后：留在同一条道上继续，别回头点左栏。
// 先把「下一条是谁」记下来再去刷新队列 —— 刷新会让当前页从队列里消失，
// 那时再算下标就错位了，会连跳两级。
async function afterResolved() {
  // 搜索结果不分页、也没有「下一条」的概念：核对完就重搜一遍，
  // 命中的页少一条，停在列表第一条上（不按 offset 翻页）
  if (S.searching) {
    await refresh(false);
    if (S.pages.length) select(S.pages[0].hash); else clearPanes();
    return;
  }
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

// 重跑：先选模型。以前只有「用原通道重跑」一条路，GLM 读不了的页
// （目录点线死循环吐三万字、图版被当表格拆）只能去命令行；2026-11-06 用户要
// 「重跑要能换模型」，所以这里给两个选项。
function openRerun() {
  if (!S.cur || !S.data) return;
  const d = S.data;
  const cur = d.channel === 'vl' ? 'PaddleOCR-VL'
    : d.channel === 'human' ? '人工（不走模型）' : 'GLM';
  $('#rerunsum').textContent =
    `p${String(d.page_no).padStart(4, '0')} · 当前 ${cur} · ${d.chars || 0} 字\n` +
    'GLM 当场跑，几秒到几十秒；VL 要占满显存，只会排进队列 ——' +
    '转完点顶部「换 VL 重跑 N」，会先帮你停掉 GLM。';
  $('#rerundlg').showModal();
}

$('#rerunglm').onclick = () => doRerun('glm');
$('#rerunvl').onclick = () => doRerun('vl');

async function doRerun(ch) {
  if (!S.cur) return;
  const dl = $('#rerundlg');
  if (dl.open) dl.close();
  if (ch === 'vl' &&
      !confirm('换 VL 要占满显存：这页会排进 VL 队列，等你停掉 GLM 服务后\n' +
               '再点顶部「换 VL 重跑 N」才会真跑。继续？')) return;
  const btn = $('#rerunbtn');
  btn.disabled = true;
  btn.textContent = ch === 'vl' ? '转队列…' : '重跑中…';
  try {
    const d = await api('/api/rerun', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hash: S.cur, channel: ch }),
    });
    if (d.queued === 'vl') {
      toast('已排进 VL 队列：点顶部「换 VL 重跑 N」，会先帮你停掉 GLM 再开跑');
      await refresh();
      return;
    }
    const st = d.stat || {};
    toast(`重跑完成（${d.channel || ch}）：${st.done || 0} 页正常 / ` +
      `${st.review || 0} 页待人工 / ${st.failed || 0} 失败`);
    await refresh();
    await select(S.cur);
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.textContent = '重跑…';
    btn.disabled = false;
  }
}

// 把 S.zoomed 落到 DOM 上。图片没加载完也算不准比例，所以 onload 里要再调一次。
function applyZoom() {
  const wrap = $('#imgwrap');
  wrap.classList.toggle('zoom', S.zoomed);
  $('#zoombtn').textContent = S.zoomed ? '适应' : '原始';
  const img = wrap.querySelector('img');
  if (!img) return;
  if (S.zoomed) {
    // 原图比栏还窄时（小尺寸扫描页）也铺满栏宽，"原始"不该比"适应"更小
    const w = Math.max(img.naturalWidth || 0, wrap.clientWidth - 24);
    img.style.width = w ? w + 'px' : '';
  } else {
    img.style.width = '';
  }
  const nw = img.naturalWidth;
  const shown = nw ? Math.round(100 * img.getBoundingClientRect().width / nw) : 0;
  $('#imgzoom').textContent = shown ? shown + '%' : '';
}

function zoom() {
  S.zoomed = !S.zoomed;
  localStorage.setItem('docplat.zoom', S.zoomed ? '1' : '0');
  applyZoom();
}

// 双击图片也能切（鼠标停在图上，比去点右上角按钮顺手）
$('#imgwrap').addEventListener('dblclick', e => {
  if (e.target.tagName === 'IMG') zoom();
});

// ---------- 历史版本 ----------
// save_text 是直接覆盖 out_path，改错一次就回不去（以前一份备份都不留）。
// 现在每次覆盖前留一份，这里列出来，点一下恢复。

// 20261104-153012.md → 11-04 15:30:12
function fmtVerName(n) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:-(\d+))?\.md$/.exec(n || '');
  return m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}` + (m[7] ? ` #${m[7]}` : '') : (n || '');
}

async function openHistory() {
  if (!S.cur) return;
  const box = $('#histlist');
  $('#histsum').textContent = `p${pad4(S.data ? S.data.page_no : 0)} · 读取中…`;
  box.innerHTML = '';
  $('#histdlg').showModal();
  try {
    const d = await api(`/api/history?doc=${encodeURIComponent(S.doc)}` +
      `&hash=${encodeURIComponent(S.cur)}`);
    $('#histsum').textContent = `p${pad4(S.data ? S.data.page_no : 0)} · ` +
      `${d.versions.length} 个历史版本（新的在上）`;
    if (!d.versions.length) {
      box.innerHTML = `<div class="hrow"><div class="hd">
        <div class="hp">还没有历史版本。第一次保存修改之前的原文会自动留一份 ——
        也就是说，从这里往后改错了都能回来。</div></div></div>`;
      return;
    }
    box.innerHTML = d.versions.map(v => `<div class="hrow">
      <div class="hd">
        <span class="ht">${esc(fmtVerName(v.name))}</span>
        <span class="hm">　${v.chars} 字</span>
        <div class="hp">${esc(v.head || '（空白页）')}</div>
      </div>
      <button class="mini" onclick="restoreVersion('${esc(v.name)}')">恢复这一版</button>
    </div>`).join('');
  } catch (e) {
    $('#histsum').textContent = '读取失败：' + e.message;
  }
}

async function restoreVersion(name) {
  if (!S.cur) return;
  if (!confirm(`把正文恢复成 ${fmtVerName(name)} 那一版？\n\n当前内容会先存一份历史，能再撤回来。`)) return;
  try {
    const d = await api('/api/restore', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hash: S.cur, name }),
    });
    $('#histdlg').close();
    toast(`已恢复（${d.chars} 字），并记为已修改`);
    await refresh(false);
    await select(S.cur);
  } catch (e) {
    toast('恢复失败：' + e.message, true);
  }
}

// ---------- 页级备注 ----------
// 不写进 verifications.note：那等于「写备注 = 已核对」，会把待人工的页
// 悄悄移出队列。备注随时能写，不影响正文，也不改变核对状态。

function openNote() {
  if (!S.cur || !S.data) return;
  // 单卷书的文件名就是页号，副标题里别写成「p0153 · p0153.jpg」
  const pn = `p${pad4(S.data.page_no)}`;
  const src = /^p\d{4}\.[a-z]+$/i.test(S.data.src || '') ? '' : ` · ${S.data.src}`;
  $('#notesum').textContent = pn + src +
    (S.data.note_at ? ` · 上次修改 ${fmtWhen(S.data.note_at)}` : '');
  $('#notebody').value = S.data.note || '';
  $('#notedlg').showModal();
  setTimeout(() => $('#notebody').focus(), 40);
}

$('#noteok').onclick = async () => {
  if (!S.cur) return;
  const note = $('#notebody').value.trim();
  try {
    const d = await api('/api/note', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ doc: S.doc, hash: S.cur, note }),
    });
    if (S.data) S.data.note = d.note;
    $('#notedlg').close();
    toast(note ? '备注已保存' : '备注已清空');
    // 列表上的小方块要跟着更新，但别重选当前页（会跳掉滚动位置）
    await loadQueue(false, false);
    markActive(S.cur);
    const nb = $('#notebtn');
    nb.classList.toggle('has', !!d.note);
    nb.title = d.note ? `备注：${d.note}` : '页级备注：不影响正文，也不改变核对状态';
  } catch (e) {
    toast('保存备注失败：' + e.message, true);
  }
};

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
  clearSel();                    // 换书了，勾选不能带过去
  loadDocs().then(() => loadQueue(true)).then(() => pollRun());
});

$('#tabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-kind]');
  if (!b) return;
  document.querySelectorAll('#tabs button').forEach(x => x.classList.toggle('on', x === b));
  S.kind = b.dataset.kind;
  S.cur = null;
  clearSel();                    // 换队列了：勾选的页多半不在新队列里
  // 正文搜索结果和队列是两回事：切队列就把搜索词清掉，
  // 否则点了标签列表纹丝不动，看着像坏了
  if (S.qmode === 'text' && $('#q').value.trim()) {
    $('#q').value = '';
    S.searching = false;
    S.terms = [];
    syncFiltering();
  }
  history.replaceState(null, '', '#kind=' + S.kind);
  loadQueue(true);
});

// 正在筛选时把文件名露出来：按文件名筛，结果里得看得见文件名
// （正文搜索模式例外：那里文件名没意义，占的位置要留给命中片段）
function syncFiltering() {
  $('#list').classList.toggle('filtering',
    !!$('#q').value.trim() && S.qmode === 'meta');
}

function updateQMode() {
  const text = S.qmode === 'text';
  const b = $('#qmode');
  b.textContent = text ? '正文' : '页·名';
  b.classList.toggle('on', text);
  b.title = text
    ? '当前搜全书正文（空格分隔多个词，全部命中才算）。点一下切回文件名/页号'
    : '当前按文件名/页号筛选。点一下切成搜全书正文';
  $('#q').placeholder = text
    ? '搜全书正文，空格分隔多个词，回车生效'
    : '按文件名或页号筛选，回车生效';
}

$('#qmode').onclick = () => {
  S.qmode = S.qmode === 'text' ? 'meta' : 'text';
  localStorage.setItem('docplat.qmode', S.qmode);
  updateQMode();
  syncFiltering();
  loadQueue(true);
};

$('#q').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  if (S.qmode === 'text' && !e.target.value.trim()) {
    toast('正文搜索：先输入要查的词', true);
    return;
  }
  loadQueue(true);
});
$('#q').addEventListener('input', e => {
  syncFiltering();
  if (!e.target.value) loadQueue(true);
});

$('#prevpg').onclick = () => { S.offset = Math.max(0, S.offset - S.limit); loadQueue(); };
$('#nextpg').onclick = () => {
  if (S.kind === 'sample') { loadQueue(true); return; }   // 换一批
  S.offset += S.limit; loadQueue();
};
// 导出：先体检，再落盘。
// export() 对「还没跑出结果」的页是静默跳过的（文件不存在 → 当成没字），
// txt 里又不写页数，少几页肉眼看不出来 —— 所以先把会缺的页摆出来。
function fmtPages(list, max = 12) {
  const head = list.slice(0, max)
    .map(n => 'p' + String(n).padStart(4, '0')).join(' ');
  return head + (list.length > max ? ` …（共 ${list.length} 页）` : '');
}

async function realExport(pf) {
  const btn = $('#exportbtn');
  btn.disabled = true;
  try {
    const d = await api(`/api/export?doc=${encodeURIComponent(S.doc)}`);
    // 空白页是「流程已经判过、只是没人工点头」的页，不再拦人，
    // 但导出结果里提一句，用户想知道少了什么时有据可查。
    const blank = (pf && pf.blank) ? pf.blank.length : 0;
    toast(`已导出 ${d.path}（${(d.size / 1024).toFixed(0)} KB）`
          + (blank ? `，${blank} 页空白页未计入` : ''));
  } catch (e) {
    toast('导出失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

$('#exportbtn').onclick = async () => {
  const btn = $('#exportbtn');
  btn.disabled = true;
  let pf;
  try {
    pf = await api(`/api/preflight?doc=${encodeURIComponent(S.doc)}`);
  } catch (e) {
    toast('导出前检查失败：' + e.message, true);
    btn.disabled = false;
    return;
  }
  btn.disabled = false;

  const rows = [];
  if (pf.miss.length) {
    rows.push(`<div class="prow bad"><span class="lv"></span><div>
      <b>${pf.miss.length} 页还没识别完</b>，导出时会被跳过，txt 里没有这几页
      <div class="pn">${fmtPages(pf.miss)}</div></div></div>`);
  }
  const c = pf.counts || {};
  const doubt = (c.review || 0) + (c.escalated || 0);
  if (doubt) {
    rows.push(`<div class="prow warn"><span class="lv"></span><div>
      <b>${doubt} 页机器判可疑、还没核对</b>，会原样进 txt
      <div class="pn">待人工 ${c.review || 0} · 待换 VL ${c.escalated || 0}</div></div></div>`);
  }
  if (pf.zero.length) {
    // 只列「0 字又说不出为什么」的页。err 里写着「空白页…」的走 pf.blank，
    // 那档流程已给结论，不该每次都拦人（2026-11-05 用户的抱怨就是这个）。
    rows.push(`<div class="prow warn"><span class="lv"></span><div>
      <b>${pf.zero.length} 页识别出 0 字、也没判成空白页</b>，可能没认出来；
      如果里面本该有字，导出就会缺内容
      <div class="pn">不确定的话，左栏「全部」里字数为 0 的页会标红</div></div></div>`);
  }

  // 只有空白页、没别的风险 → 不弹窗，直接导（提示里会说明少了几页空白）。
  if (!rows.length) { await realExport(pf); return; }
  S.lastPf = pf;                       // 对话框关掉后「仍然导出」还要用它的 blank 计数
  $('#presum').textContent = `${S.doc} · 共 ${pf.total} 页`;
  $('#prelist').innerHTML = rows.join('');
  $('#prego').textContent = pf.miss.length
    ? `仍然导出（会少 ${pf.miss.length} 页）` : '仍然导出';
  // 必须先清空 returnValue：dialog 被 Esc 关掉时它保留上一次的值，
  // 上次点过「仍然导出」的话，这次按 Esc 会直接把文件写出去。
  $('#predlg').returnValue = '';
  $('#predlg').showModal();
};

$('#predlg').addEventListener('close', () => {
  if ($('#predlg').returnValue === 'go') realExport(S.lastPf);
});

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
    b.title = '跑「升级」队列里的这 ' + c.escalated + ' 页：'
      + '结构闸门判为分格条目的 + 你在待人工里手动「转 VL」过来的；'
      + '跑之前要先停掉 GLM 服务';
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
      $('#rfill').style.transform = 'scaleX(0)';
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
  $('#rfill').style.transform = `scaleX(${pct / 100})`;

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
      !confirm('换 VL 要占满显存：先帮你停掉 GLM 服务（跑完要重新拉起），再来点确定。\n' +
               'GLM 已经停了就继续。')) return;
  $('#runbtn').disabled = true;
  try {
    if (channel === 'vl') {
      // 显存腾挪替用户做掉：以前这一步只能去命令行停 GLM，
      // 工作台却让他「停掉再来点」——等于把最麻烦的一步留给人。
      $('#runbtn').textContent = '停 GLM…';
      await api('/api/glm/stop', { method: 'POST' }).catch(e => {
        toast('停 GLM 失败，先手动停一下：' + e.message, true);
      });
    }
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

// ---------- 服务灯 / 日志 ----------
// 识别服务（8081）死了以前只有两种情况会暴露：点「开始识别」等它自己拉起，
// 或者去翻日志文件。挂一个灯，15 秒探一次，点开看日志尾巴。

async function pollLamp() {
  const el = $('#lamp');
  try {
    const d = await api('/api/health');
    const up = !!d.glm;
    el.className = 'lamp ' + (up ? 'up' : 'down');
    el.title = (up
      ? `识别服务正常（${d.glm_base}）`
      : `识别服务没响应（${d.glm_base}）——点「开始识别」会自动把它拉起来`)
      + ' · 点这里看日志';
  } catch (e) {
    el.className = 'lamp down';
    el.title = '工作台连不上：' + e.message;
  }
}

async function refreshLog() {
  const box = $('#logbox');
  try {
    const d = await api('/api/log?lines=200');
    $('#logpath').textContent = d.path || '没有日志文件';
    box.textContent = d.lines.join('\n') || '（空）';
    box.scrollTop = box.scrollHeight;      // 看日志总是关心最后几行
  } catch (e) {
    box.textContent = '读取日志失败：' + e.message;
  }
}

$('#lamp').onclick = () => {
  $('#logbox').textContent = '读取中…';
  $('#logdlg').showModal();
  refreshLog();
};
$('#logrefresh').onclick = refreshLog;
pollLamp();
setInterval(pollLamp, 15000);

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
  else if (e.key === 'n' && S.data) openNote();
  else if (e.key === 'h' && S.data) openHistory();
  else if (e.key === 'x' && S.cur) {                 // 勾/取消勾当前页
    e.preventDefault();
    toggleSel(S.cur, S.pages.findIndex(p => p.hash === S.cur), e.shiftKey);
  } else if (e.key === 'z') zoom();
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

// 原图栏宽度：拖 splitter2 调整，双击复位成 1fr（跟着窗口均分）。
// 以前只有左栏能拖 —— 正文栏是固定宽度的，想把图看大只能把左栏拉窄。
(function () {
  const MIN = 240;
  const root = document.documentElement;
  const pane = document.getElementById('splitter2').previousElementSibling;
  const saved = parseInt(localStorage.getItem('midW') || '', 10);
  if (saved >= MIN) root.style.setProperty('--mid-w', saved + 'px');
  const sp = $('#splitter2');
  let dragging = false, startX = 0, startW = 0;
  // 上限让正文栏至少留 320：窗口小的时候别把右栏挤没
  function cap() {
    const aside = parseInt(getComputedStyle(root).getPropertyValue('--aside-w'), 10) || 330;
    return Math.max(MIN, window.innerWidth - aside - 12 - 320);
  }
  sp.addEventListener('mousedown', e => {
    dragging = true; startX = e.clientX;
    startW = pane.getBoundingClientRect().width;
    sp.classList.add('dragging'); document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const w = Math.max(MIN, Math.min(cap(), startW + (e.clientX - startX)));
    root.style.setProperty('--mid-w', w + 'px');
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false; sp.classList.remove('dragging'); document.body.style.cursor = '';
    const w = parseInt(getComputedStyle(root).getPropertyValue('--mid-w'), 10);
    if (w) localStorage.setItem('midW', String(w));
  });
  sp.addEventListener('dblclick', () => {
    root.style.removeProperty('--mid-w');
    localStorage.removeItem('midW');
  });
  // 窗口缩小时，之前拉宽的像素值可能把右栏挤到 0，顺手收回可用范围内
  window.addEventListener('resize', () => {
    const w = parseInt(getComputedStyle(root).getPropertyValue('--mid-w'), 10);
    if (w && w > cap()) root.style.setProperty('--mid-w', cap() + 'px');
  });
})();

updateQMode();
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
