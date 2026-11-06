"""从 v1 搬过来的纯函数 —— 这些是实测调出来的，重写没有意义。

刻意不搬的东西：
  · human_note()：v1 把「待人工复核」提示写进正文，污染了唯一的评测基准
    （p0016 基准 44 字里 40 字是标记，导致模型判分从 100% 掉到 2.3%）。
    v2 改成写数据库 status 字段，正文永远是纯识别结果。
  · app.py / run_all.py / stop_all.py：三层进程那套是 v1 事故的根源。
"""
import re
import zlib
from pathlib import Path

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 草书被误识成假名/谚文/西里尔字母的典型症状
NONCJK_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]")
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")


def _dict_pick(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def _from_md_value(v):
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        got = _dict_pick(v, ("markdown_texts", "markdown", "text"))
        if got:
            return got
        for val in v.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    if isinstance(v, list) and v:
        joined = "\n\n".join(x for x in v if isinstance(x, str) and x.strip())
        if joined.strip():
            return joined.strip()
    return None


def extract_text(res):
    """从 PaddleOCR-VL 的一页结果里取 markdown。

    结果对象同时支持 dict 取值和属性访问，但 'markdown' 只是属性不是键，
    所以必须先试属性再试字典 —— 顺序反了就永远拿不到。
    """
    if res is None:
        return None
    if isinstance(res, str):
        return res.strip() or None
    if hasattr(res, "markdown"):
        got = _from_md_value(getattr(res, "markdown"))
        if got:
            return got
    if isinstance(res, dict) or hasattr(res, "get"):
        for key in ("markdown", "md", "text", "markdown_texts"):
            try:
                got = _from_md_value(res.get(key))
            except Exception:
                got = None
            if got:
                return got
        try:
            rec = res.get("rec_texts")
        except Exception:
            rec = None
        if isinstance(rec, list) and rec:
            return "\n".join(str(x) for x in rec).strip()
    for attr in ("md", "text", "markdown_texts", "rec_texts"):
        v = getattr(res, attr, None)
        if v is not None:
            got = _from_md_value(v)
            if got:
                return got
    try:
        js = res.json
        if isinstance(js, dict):
            got = _from_md_value(js.get("res") or js.get("result") or js)
            if got:
                return got
    except Exception:
        pass
    return None


def clean_output(t: str) -> str:
    """去掉行首的 markdown 记号（# > ` * 和列表符），行内字符一律不动。"""
    lines, out, blank = (t or "").splitlines(), [], False
    for raw in lines:
        s = raw.strip()
        s = re.sub(r"^[#>\s]+", "", s)
        s = re.sub(r"^([-*+])\s+", "", s)
        s = s.strip().strip("`").strip()
        s = s.replace("\u200b", "").replace("\ufeff", "").rstrip()
        if s:
            out.append(s)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def clean_piece(t: str) -> str:
    """一列就是一整行：去记号、空行、不含汉字的杂讯行。"""
    lines = []
    for ln in (t or "").splitlines():
        ln = re.sub(r"<[^>]+>", " ", ln)
        ln = ln.strip().lstrip("#").strip().strip("`").strip()
        if not ln or not CJK_RE.search(ln):
            continue
        lines.append(re.sub(r"\s+", "", ln))
    return "".join(lines)


def is_path_echo(text: str, img: Path) -> bool:
    """模型是不是把输入路径当结果返回了。

    整页找不到可识别文本块时（空白纸、纸背透印、整幅大字），
    PaddleOCR-VL 会退化成吐回图片路径。重跑结果完全一样，但路径不能进正文。
    """
    s = (text or "").strip().strip('"').strip("'").strip()
    if not s or "\n" in s:
        return False
    if s == str(img):
        return True
    low = s.lower()
    return (any(low.endswith(e) for e in IMG_EXT)
            and img.stem in s
            and any(c in s for c in ":\\/"))


def body_chars(text: str) -> int:
    return len(re.sub(r"\s", "", text or ""))


TAG_RE = re.compile(r"<[^>]+>")


def text_only(t: str) -> str:
    """去掉 HTML 标签后的纯文本 —— 判字数、判长短必须用它。

    PaddleOCR-VL 的表格输出每格都带一大串
    `style='text-align: center; word-wrap: break-word;'`。
    p0202 这页按原始输出算 9608 字，其中真正的字不到 20 个，
    直接拿原串算字数会把「空表格」判成「重复生成」（实测 5 页全中）。
    """
    return TAG_RE.sub("", t or "")


def text_len(t: str) -> int:
    """纯文本字数（不含标签与空白）。"""
    return len(re.sub(r"\s", "", text_only(t)))


TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def effective_len(t: str) -> int:
    """真实内容量：表格输出里完全相同的格子只算一次。

    vl 拆格失败时会把同一段字反复铺进几十个格子 —— p0162 的 `後譜贊贊前`
    出现 78 次、`○闕資料` 出现 28 次，2730 个格子里 145 个有字、去重后只剩 31 种。
    按原串数字数会得 675 字，把「拆格崩了」误当成「读得很多」，
    于是回退比较时永远判成「GLM 更少、不划算」，页面就一直卡在废表格上。

    只用于比较两侧谁读得多，不作为展示用的字数。
    """
    s = t or ""
    if "<td" not in s:
        return text_len(s)
    cells = TD_RE.findall(s)
    if not cells:
        return text_len(s)
    seen, total = set(), 0
    for c in cells:
        x = text_only(c).strip()
        if not x or x in seen:
            continue
        seen.add(x)
        total += text_len(x)
    return total or text_len(s)


def _flat_cell(s: str) -> str:
    """格子内文归一：去标签、去 vl 加的 ○ 标号、去空格与字面 `\\n`。"""
    s = text_only(s).strip()
    s = s.replace("\\n", " ").replace("\\r", " ")   # vl 有时吐字面的反斜杠 n
    s = re.sub(r"^[○〇◯●]", "", s)
    return re.sub(r"\s", "", s)


def vl_only_fragments(old_table: str, new_text: str, min_len: int = 2,
                      max_repeat: int = 3) -> list[str]:
    """vl 表格里读到、但新文本里找不到的片段。

    换成纯文本清单是「用结构换可读性」，代价是可能丢东西：
    p0163 的 vl 输出里有 `生卒葬俱缺`、`佳生卒葬亦`，GLM 重读后这两段没了。
    这种丢失不写进正文（v1 就是把提示写进正文污染了基准），而是作为复核提示，
    让人决定要不要补 —— 所以这里只负责算出差集。

    两条过滤，都是为了不被拆格失败的产物淹没：
      · ○ 前缀是 vl 给无名者加的标号，比对前必须去掉（否则 p0162 误报 12 处）；
      · 在表格里重复 >= max_repeat 次的片段是拆格填充垃圾（p0162 的 `後譜贊贊前`
        出现 78 次），不是真内容。
    """
    cells = TD_RE.findall(old_table or "")
    if not cells:
        return []
    flat = [_flat_cell(c) for c in cells]
    hay = _flat_cell(new_text)
    seen, out = set(), []
    for f in flat:
        if len(f) < min_len or f in seen:
            continue
        seen.add(f)
        if flat.count(f) >= max_repeat:
            continue
        if f not in hay:
            out.append(f)
    return out


def has_foreign(text: str) -> bool:
    """含假名/谚文/西里尔 —— 草书误识的典型症状。"""
    return bool(NONCJK_RE.search(text or ""))


# ============ 渲染：把模型原始输出变成人能读的文本 ============
# 正文文件（data/pages/*.md）永远存模型原样，渲染只发生在导出和界面展示时。
# 这样「表格」这层信息不丢，但用户看到的不是一堆 <td style='...'>。

TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
BR_RE = re.compile(r"<br\s*/?>", re.I)
# 同一个 2~12 字片段连续重复 6 次以上 —— 模型退化，不是内容
REPEAT_RE = re.compile(r"(.{2,12}?)\1{5,}", re.S)

GRID_MAX_COLS = 12      # 超过这个列数就不当表格（p0154 一行 821 格）
GRID_MAX_CELL = 40      # 格子超过这个字数也不当表格（p0143 单格 3273 字）
GRID_MAX_EMPTY = 0.60   # 空格子比例上限（竖排世系表每列长短不齐，空格子是常态）
LONG_CELL = 20          # 超过这个字数的格子当正文段落，不和别的格拼一行
MD_FLAT_WIDTH = 200     # md 表摊平时不折行的宽度：数值型表格一行一记录更好读
WIDE_COLS = 8           # 一行超过这个格数就按「宽表」处理：一行一记录、保留空位
SQUASH_MIN = 20         # 同片段重复到这个次数才算模型退化（族谱实测 1967/2020 次）


def _squash_repeat(s: str) -> str:
    """把连续重复的片段压成一次并标注次数。

    p0188 的 `奏請奏請奏請…`（约 200 次）、p0143 的 `卒缺葬祖塋缺配王`、
    p0147 的 `子二子二…` 都是模型卡住后的退化输出，原样导出要占几百行。

    只在**单元格内部**或**表格之外的正文**上做 —— 整段乱压会把 markdown 表格的
    `|---|---|…` 分隔行也当成重复压掉（p0138 实测中招）。

    ⚠ 次数下限是 20（2026-11-03 由 6 提到 20）：溧阳商业志里「1961 100 — — —
    …」（13 个破折号表示无数据）、「20 10」×15（同规格的 15 行）都是真数据，
    按 6 次的老阈值会被压成「——……（重复 13 次）」，比原样更难读。
    真正的退化是成百上千次，20 次这条线两边都没有样本。
    """
    def _sub(m):
        unit, whole = m.group(1), m.group(0)
        n = len(whole) // len(unit)
        if n < SQUASH_MIN:
            return whole
        return f"{unit}……（重复 {n} 次）"
    return REPEAT_RE.sub(_sub, s)


# 整行只有分隔符（模型把 ASCII 表格的横线当正文吐出来了，p94 就是
# `--- ---- ---- ---- …`）→ 直接丢掉，别做成一行
_SEP_LINE = re.compile(r"^[\s\-—－_=+·|／/\\]{3,}$")

# 行首的 markdown 标题记号（`# 卷八`、`## 前川陽氏宗詣`）。
_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*")

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _entities(s: str) -> str:
    """收拾模型吐出来的标记残留。

    · `&lt;nl&gt;`：模型用 <nl> 表示换行，转义后在正文里留着（p1644 表头）
    · `$\\textcircled{4}$`：LaTeX 圈号，正文里应该就是 ④（p7031；本书一直用到 ⑰）
    """
    s = (s or "").replace("&lt;", "<").replace("&gt;", ">") \
                .replace("&quot;", '"').replace("&nbsp;", " ").replace("&amp;", "&")
    s = re.sub(r"</?nl\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"\$?\\textcircled\{(\d+)}\$?",
               lambda m: _CIRCLED[int(m.group(1)) - 1]
               if 1 <= int(m.group(1)) <= len(_CIRCLED) else m.group(0), s)
    return s


def _prose(s: str) -> str:
    """表格之外的正文：去标签、<br> 换行、压退化重复。"""
    s = BR_RE.sub("\n", _entities(s))
    s = text_only(s)
    for _ in range(3):
        t = _squash_repeat(s)
        if t == s:
            break
        s = t
    lines = [ln for ln in s.split("\n") if not _SEP_LINE.match(ln)]
    # GLM 回的是 markdown，版心/标题常带 `#`、`##`。正文文件要 flat（记事本直读、
    # 无任何记号），符号必须去掉 —— 只剥行首的 1~6 个 #，行内的字一个不动。
    lines = [_HEAD_RE.sub("", ln) for ln in lines]
    return "\n".join(lines)


def _cells_of(html: str) -> list[list[str]]:
    """把一段 <table> 拆成 [[单元格文本, ...], ...]，每格已归一且压过重复。"""
    rows = []
    for tr in ROW_RE.findall(html):
        row = []
        for c in CELL_RE.findall(tr):
            c = BR_RE.sub("\n", _entities(c))
            # vl 有时把换行吐成字面的反斜杠 n（p0135 整页塞进一个 td 就是这形态），
            # 不还原的话正文里会明晃晃留着 `\n` 两个字符
            c = c.replace("\\n", "\n").replace("\\r", "\n")
            c = text_only(c).strip()
            c = re.sub(r"[ \t\u3000]+", " ", c)
            c = _HEAD_RE.sub("", c)
            c = _squash_repeat(c)
            row.append(c)
        if row:
            rows.append(row)
    return rows


def _flatten_rows(rows: list[list[str]], width: int = 40) -> str:
    """逐行铺开：短格子用全角空格连成一行（超宽折行），长格子独占一段。

    宽表（一行 8 格以上，如统计年鉴里的「12 月 × 数量/金额/平均价」）
    例外：一行就是一记录，**不折行、也不丢空位** —— 空位用「-」占着，
    读者才能数出这个数字属于哪个月。折行会把一条记录切成两条，读数就串了。
    """
    out = []
    for row in rows:
        if len(row) >= WIDE_COLS:
            cells = [(c or "-") for c in row]
            if not any(c.strip() and c != "-" for c in cells):
                continue                      # 整行空
            if max(len(c) for c in cells) <= LONG_CELL:
                out.append("\u3000".join(cells).rstrip("\u3000-"))
                continue
        cells = [c for c in row if c.strip()]
        if not cells:
            continue
        # 长格子（整段正文塞进一格，p0153/p0188 这种）就让它自成段落，
        # 不要和别的格拼成一行，否则会变成一条几千字的流水账
        if len(cells) == 1 or max(len(c) for c in cells) > LONG_CELL:
            out.extend(cells)
            continue
        line = ""
        for c in cells:
            add = c if not line else "\u3000" + c
            if line and len(line) + len(add) > width:
                out.append(line)
                line = c
            else:
                line += add
        if line:
            out.append(line)
    return "\n".join(out)


def _cols_text(rows: list[list[str]], width: int = 40, rev: bool = True) -> str:
    """按列铺开：一列一段，列内从上到下，格间用全角空格连起来。

    rev=True 时从最右列开始（HTML <td> 是左→右的自然顺序，古籍要反过来读）。
    width<=0 表示不折行 —— 一列就是一条记录（p0144「二六世 伯承 字武烈…」），
    折行后读者分不清是续行还是下一列。
    """
    n = max((len(r) for r in rows), default=0)
    if not n:
        return ""
    grid = [list(r) + [""] * (n - len(r)) for r in rows]
    order = range(n - 1, -1, -1) if rev else range(n)
    out = []
    for j in order:
        cells = []
        for i in range(len(grid)):
            c = (grid[i][j] or "").strip()
            c = re.sub(r"\s+", " ", c)          # 格内换行并成空格，别把一段切断
            if c:
                cells.append(c)
        if not cells:
            continue
        if width <= 0:
            out.append("\u3000".join(cells))
            continue
        line = ""
        for c in cells:
            add = c if not line else "\u3000" + c
            if line and len(line) + len(add) > width:
                out.append(line)
                line = "\u3000\u3000" + c       # 续行缩进，标出这是同一列的延续
            else:
                line += add
        if line:
            out.append(line)
    return "\n".join(out)


def _flatten_cols(rows: list[list[str]], width: int = 40) -> str:
    """竖排表按「列」铺开：先最右一列，列内从上到下，一列一段。

    古籍竖排从右往左，一页世系图就是若干竖列。VL 的 <td> 是 HTML 自然顺序
    （从左到右），按行摊平会得到「次子 平長子 次子 次子 子 裔長子」这种
    横着拼的行 —— 列序反了，而且一列的句子会被下一列截断（p0133、p0135）。
    """
    return _cols_text(rows, width, rev=True)


def _unstack_single(rows: list[list[str]]):
    """整页被塞进一个单元格时，把里面的「行 × 空格分段」还原成矩阵。

    p0135 就是这样：一个 <td> 里 15 行，每行是「舜三十六世 襲父國位謚 襲位十一年 …」
    —— 五列的字被按视觉行横着拼在一起。这里按段数切回五列。

    返回 (标题行列表, 矩阵) 或 None（不是这种形态）。标题行 = 表体之前那些
    段数不足的行（「右圖自周受封…」「遠祖世表」「第一世 第二世 …」）。
    """
    if len(rows) != 1 or len(rows[0]) != 1:
        return None
    text = rows[0][0]
    if "\n" not in text:
        return None
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if len(lines) < 3:
        return None
    segs = [[s for s in re.split(r"[ \u3000]+", ln) if s] for ln in lines]
    counts = [len(s) for s in segs]
    m = max(counts)
    if m < 3:
        return None
    start = counts.index(m)                     # 表体从第一个「满段」行开始
    body = segs[start:]
    if len(body) < 2:
        return None
    return lines[:start], body


def _columnar(rows: list[list[str]]) -> bool:
    """这张表是不是真「多列竖排」—— 只有在多列时才谈得上转置。

    反例 p0129（上古至陳國世系吊线图）：6 行里 5 行是 `colspan=6` 合并成一格，
    每行本来就是一段横写的字。转置会把 6 段硬串成一条 40 字长行，比不转更糟。
    判据用「多格行占比」：真竖排表（p0133 那种）几乎每行都是满格。
    """
    n = max((len(r) for r in rows), default=0)
    if n < 2 or len(rows) < 3:
        return False
    multi = sum(1 for r in rows if len(r) >= 2)
    return multi / len(rows) >= 0.5


_GEN_RE = re.compile(r"^第?[〇零一二三四五六七八九十百千两]{1,4}世$")


def _is_gen_row(row: list[str]) -> bool:
    """这一行是不是「世代号行」：二六世 | 二七世 | 二八世 …

    VL 对竖排吊线图是按「视觉行」横切的：页面同一高度上的 N 个竖列各成一格。
    所以「世代号」会出现在同一行里，而且各列从上往下 = 一个人物的完整条目
    （p0144：二六世 伯承 字武烈 生卒缺 配姜氏 …）。这种页不能整体反着转置，
    要以世代号行为界切段，段内按列正序读。
    """
    cs = [c.strip() for c in row if c.strip()]
    if len(cs) < 3:
        return False
    return sum(1 for c in cs if _GEN_RE.match(c)) >= len(cs) * 0.6


def _gen_segments(rows: list[list[str]]) -> str | None:
    """把「世代号行 + 若干内容行」组成的页按段、按列正序读出来。

    段 = 一个世代号行连同它下面直到下一个世代号行的内容行；段首之前
    （书眉、支派标题）单独成段。没有世代号行返回 None，交给转置分支。
    """
    idx = [i for i, r in enumerate(rows) if _is_gen_row(r)]
    if not idx:
        return None
    segs = []
    if idx[0] > 0:
        segs.append(rows[:idx[0]])
    for k, s in enumerate(idx):
        e = idx[k + 1] if k + 1 < len(idx) else len(rows)
        segs.append(rows[s:e])
    out = []
    for seg in segs:
        # 段内第 0 列是第一个世代，正序读 —— 和多格竖排图（rev=True）相反。
        # 不折行：一列 = 一个人的完整小传，折了就跟下一列混在一起。
        t = _cols_text(seg, 0, rev=False)
        if t:
            out.append(t)
    return "\n".join(out) or None


def _flatten_vertical(rows: list[list[str]], width: int = 40) -> str:
    """竖排页的总入口：多格表格直接转置，整页单格先拆段再转置。

    注意「竖排」≠「一律转置」：吊线图、版心、服制图这些每行只有一格的表，
    转置只会把各行串成流水账，按行读才对（2026-11-04 用户指出 p0129）。
    含「世代号行」的页（p0144 那种每人一条小传）也不是整体转置，
    见 _gen_segments。
    """
    single = _unstack_single(rows)
    if single:
        head, body = single
        # 单格里的段是 VL 按古书顺序（右→左）拼的，第 0 段已经是最右列，
        # 所以这里正序输出 —— 和多格表格的方向正好相反。
        cols = _cols_text(body, 0, rev=False)
        return "\n".join(head + ([cols] if cols else []))
    if not _columnar(rows):
        return _flatten_rows(rows, width)
    gen = _gen_segments(rows)
    if gen:
        return gen
    return _flatten_cols(rows, width)


def _is_grid(rows: list[list[str]]) -> bool:
    """是不是一张规整的分格表 —— 只有这种才值得还原成 markdown 表格。"""
    if len(rows) < 2:
        return False
    cols = {len(r) for r in rows}
    if len(cols) != 1:
        return False
    n = cols.pop()
    if not (2 <= n <= GRID_MAX_COLS):
        return False
    flat = [c for r in rows for c in r]
    if not flat:
        return False
    if sum(1 for c in flat if not c) / len(flat) > GRID_MAX_EMPTY:
        return False
    return max(len(c) for c in flat) <= GRID_MAX_CELL


def _md_rows(line: str) -> list[str] | None:
    """一行 pipe 表 → 单元格列表；不是 pipe 行返回 None。"""
    s = line.strip()
    if len(s) < 2 or not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


def _is_md_sep(cells: list[str]) -> bool:
    """`| --- | :---: |` 这种分隔行。

    也要认被退化压缩过的形态 `| :--- ……（重复 8 次）|` —— render_text 里
    管道表排在 _prose 之后处理，那时分隔行已经被 REPEAT_RE 压掉一半了。
    """
    if not cells or not any(cells):
        return False
    for c in cells:
        if not c:
            continue
        c = re.sub(r"…+（重复\s*\d+\s*次）", "", c).strip()
        if not re.fullmatch(r":?-{2,}:?", c):
            return False
    return True


def _md_tables(s: str, tables: str, width: int, vertical: bool = False) -> str:
    """把正文里的 markdown 管道表也接管过来。

    为什么需要：GLM 遇到印刷表格常直接吐 md 管道表（不是 HTML <table>），
    老版本 render_text 只认 <table>，于是 txt 里留着歪斜的管道行，而且分隔行
    `| --- | --- |…` 会被 _prose 的退化压缩误伤成 `| :--- ……（重复 8 次）|`。
    这里先把连续管道行整块摘走，再按 tables 模式重新渲染，杜绝这两种脏输出。
    """
    lines = s.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        cells = _md_rows(lines[i])
        if cells is None:
            out.append(lines[i])
            i += 1
            continue
        rows, j = [], i
        while j < n:
            c = _md_rows(lines[j])
            if c is None:
                break
            if not _is_md_sep(c):        # 分隔行丢掉，需要时由 _as_md_table 重生成
                rows.append(c)
            j += 1
        if rows:
            if vertical:
                out.append(_flatten_vertical(rows, width))
            else:
                out.append(_as_md_table(rows)
                           if (tables == "auto" and _is_grid(rows))
                           else _flatten_rows(rows, max(width, MD_FLAT_WIDTH)))
        i = j
    return "\n".join(out)


def _as_md_table(rows: list[list[str]]) -> str:
    n = len(rows[0])
    head = [(c or " ").replace("|", "｜").replace("\n", " ") for c in rows[0]]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join(["---"] * n) + "|"]
    for r in rows[1:]:
        cells = [(c or "").replace("|", "｜").replace("\n", " ") for c in r]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _redundancy(s: str) -> float:
    """压缩冗余度：0 = 完全随机，1 = 一个字重复到底。

    用来一眼认出模型卡死的页 —— 实测正常页中位 0.28，退化页 0.97~0.99，
    中间没有样本，阈值放 0.85 很安全。
    """
    if len(s) < 200:
        return 0.0
    raw = s.encode("utf-8")
    try:
        return 1 - len(zlib.compress(raw)) / len(raw)
    except Exception:
        return 0.0


def redundancy_of(text: str) -> float:
    """整段文本的压缩冗余度（先去掉空白与标签）。"""
    return _redundancy(re.sub(r"\s", "", text_only(text or "")))


DEGEN_KEEP = 300          # 截断前保留的字数（阈值在 config.DEGEN_REDUNDANCY）
DEGEN_TAIL = "\n……（此处起为模型重复生成，导出时省略；原文见页面文件，待复核）"


def render_text(text: str, tables: str = "auto", width: int = 40,
                vertical: bool = False) -> str:
    """把一页原始输出渲染成人能读的文本。

    tables="auto"：规整的分格表还原成 markdown 管道表，其余（一行几百格、
    单格几千字、列数参差）改成分行文字。
    tables="flat"：一律分行文字（工作台展示用 —— 复核时看格子，不如看行）。
    vertical=True：表格按列读、从右往左（竖排族谱，见 config.is_vertical）。

    正文文件不改，渲染只发生在导出和界面展示时。
    """
    s = text or ""
    # 表格被 max_tokens 截断时没有 </table>（溧阳商业志 p165：30 列的月报表，
    # 吐到一半就到 6000 token 上限）—— 不补上闭合标签，整个表会被当成正文，
    # 几百个格子全糊成一行。补上以后至少能按行摊开，残缺的那一行看得见。
    if "<table" in s.lower() and "</table>" not in s.lower():
        s += "</table>"
    parts, last = [], 0
    for m in TABLE_RE.finditer(s):
        parts.append(_prose(s[last:m.start()]))
        rows = _cells_of(m.group(1))
        if rows:
            if vertical:
                parts.append(_flatten_vertical(rows, width))
            else:
                parts.append(_as_md_table(rows)
                             if (tables == "auto" and _is_grid(rows))
                             else _flatten_rows(rows, width))
        last = m.end()
    parts.append(_prose(s[last:]))
    s = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    # 管道表最后处理：_prose 会把 `| --- | --- |…` 这种分隔行当退化重复压成
    # `| :--- ……（重复 8 次）|`，_is_md_sep 能认这个形态，所以放后面也能收拾干净。
    s = _md_tables(s, tables, width, vertical)

    # 整页退化（p0154 把六个名字洗了 821 格）：逐格压不住，直接截断并说明
    from . import config as C          # 阈值只在 config 里定义一份
    plain = re.sub(r"\s", "", text_only(text or ""))
    if len(plain) > DEGEN_KEEP and _redundancy(plain) >= C.DEGEN_REDUNDANCY:
        mark = s.find("……（重复")
        if mark > 40:
            s = s[:mark]
            cut = s.rfind("\n")
            if cut > 40:
                s = s[:cut]
        else:
            s = s[:DEGEN_KEEP]
            cut = s.rfind("\n")
            if cut > DEGEN_KEEP // 2:
                s = s[:cut]
        s = s.rstrip("\u3000 \n") + DEGEN_TAIL
    return s


# ---- 交付用排版（2026-11-04 用户定）--------------------------------------

# 页标记（2026-11-04 用户定的全局规则，形态照他给的样例）：
#     【第11页 / Image00011】
# 前半是原书页序号，后半是这一页的图片文件名（去掉扩展名）—— 页码对原书、
# 文件名对图库，两边都能回查。单独一行、顶格、不与上下段接续。
# 拿不到文件名时退化成 【第11页】。
PAGE_MARK = "【第{no}页 / {img}】"
PAGE_MARK_BARE = "【第{no}页】"
_PAGE_MARK_RE = re.compile(r"^【第\d+页(?: / [^】]+)?】$")


def page_mark(page_no, img: str = "") -> str:
    """原书第 N 页的标记行。img = 该页图片文件名（不带扩展名）。

    文件名就是页码时（`1.jpg`、`0001.jpg` 这类）不重复写一遍，
    只出 `【第1页】` —— 否则会得到 `【第1页 / 1】` 这种废话。
    """
    try:
        no = int(page_no)
    except (TypeError, ValueError):
        return ""
    img = (img or "").strip()
    if img.isdigit() and int(img) == no:
        img = ""
    return (PAGE_MARK.format(no=no, img=img) if img
            else PAGE_MARK_BARE.format(no=no))


def is_page_mark(s: str) -> bool:
    return bool(_PAGE_MARK_RE.match((s or "").strip()))


# 书里的正文：段落首行缩进两格，段落之间**不空行**（空行是 markdown 的段落分隔，
# 不是书上的排法）。缩进用全角空格，Notepad 里正好两格。
INDENT = "\u3000\u3000"
# 顶格的行：书上的标题、目录条、图注、名录条目、堂号/卷次、表格摊平出来的数据行
# 都是顶格的。判据只有两条 —— 有没有句末标点、够不够长，不再看 16 字那种短阈值：
# 族谱名录里「登慶慼二年進士樞密直學士判尙書省贈少師」22 字却该顶格，
# 而正文段落大多是几十上百字、以「。」收尾。
FLUSH_MAX = 30
_END_PUNCT = "。，、；：！？…“”‘’）》」』】"
# 一行里出现三个及以上「数字串 + 空白」→ 这是表格摊平出来的数据行
_NUMROW = re.compile(r"\d[\d,.\-]*[\s\u3000]+\d[\d,.\-]*[\s\u3000]+\d")
# 另起一行的标志：一、二、／第X章／（一）／1.
_LEAD = re.compile(r"^(?:[一二三四五六七八九十]+[、．.]|第[一二三四五六七八九十百零〇\d]+[章节篇回讲部]"
                   r"|[（(][一二三四五六七八九十\d]+[)）]|\d+[、.．]\s?|[●○◆◇■□·•])")
# 句子没说完的结尾：这种行下一行一定是接着写的（模型对整版正文常按视觉行断开）
_CONT = "，、；：,;:"
# 汉字之间夹空格（分栏排法的痕迹：表头、名录、族谱世系都长这样）
_SPACED = re.compile(r"[\u4e00-\u9fff][ \u3000]+[\u4e00-\u9fff]")
# 目录条目：一行里带点线引导符，或以「（页码）」收尾。这种行必须各自成段 ——
# 粘起来就成了「…几点思考 …… (171)搞好人大任命干部述职评议…」一锅粥。
# 只认「点线 ≥4 个」或「纯数字页码」，正文里的「（38）」引用多是行中、极少独占
# 行尾，误伤面很小（2026-11-05 人大五十年 p35 目录页的教训）。
_TOC = re.compile(r"[….·]{2,}\s*[（(]?\s*\d{1,4}\s*[）)]?\s*$"
                  r"|[（(]\s*\d{1,4}\s*[）)]\s*$")


def _flush_line(s: str) -> bool:
    if is_page_mark(s):
        return True                        # 页标记顶格，前面不缩进
    if s.startswith("影像"):
        return True
    if s[:1] in ("|", "#", "<", ">"):
        return True
    if _NUMROW.search(s) and len(CJK_RE.findall(s)) < 30:
        return True
    # 世系吊线图 / 摊平的宽表：格子之间用全角空格或连串空格隔开，
    # 一行里出现三次以上就是「一行一记录」，不是段落，顶格。
    if s.count("\u3000") >= 3 or re.search(r"\S {3,}\S", s):
        return True
    if s.endswith(tuple(_END_PUNCT)) and len(CJK_RE.findall(s)) >= 8:
        return False                       # 说完了 → 是一段正文
    # 被页尾截断的段落（结尾停留在「…参与政」这种半句上）也是一段正文，
    # 只要够长就缩进；30 字这个坎是为了把族谱名录（22 字上下、无标点）留在顶格。
    return len(CJK_RE.findall(s)) < FLUSH_MAX


def _joins(prev: str, cur: str) -> bool:
    """cur 是不是 prev 那段话被视觉换行截断的下一截。

    只有「上一行没把句子收住」才接。**别**再加「上一行够不够一整行」这类宽度闸门：
    同一页里模型常常一半段落已并好（长行）、一半还在逐行吐（短行），页面级的分位值
    会被长行带偏，结果是把真正的换行截断当成条目行，句子又被劈开（2026-11-05 试过，
    溧阳南城 p50/p60 立刻出现「这说／明，发展休闲产业…」这种断行）。
    代价是「一行一条」的清单会被并成一段（溧阳商业志「中国百货公司…公司；」），
    这个可以接受：字没丢，读起来仍是连续的列举。
    """
    if is_page_mark(prev) or is_page_mark(cur):
        return False                       # 页码自成一行，前后都不许粘
    if not prev or prev.startswith("影像") or cur.startswith("影像"):
        return False
    if prev.endswith(tuple(_END_PUNCT)) and not prev.endswith(tuple(_CONT)):
        return False                       # 上一行已经把句子收住了
    if _NUMROW.search(prev) or _NUMROW.search(cur):
        return False                       # 表格摊平的行不能粘
    if _TOC.search(prev) or _TOC.search(cur):
        return False                       # 目录条目（点线/页码）各自成段
    # 竖排表按列摊出来的段（一列一段，格间是全角空格）也不能粘 ——
    # 粘起来就把「第 1 世／第 2 世…」各列又串成一大段，等于白排
    if prev.count("\u3000") >= 3 or cur.count("\u3000") >= 3:
        return False
    if _LEAD.match(cur) or _LEAD.match(prev):
        return False                       # 编号条目各自成段
    n_prev = len(CJK_RE.findall(prev))
    n_cur = len(CJK_RE.findall(cur))
    # 分栏排的表头行（「年代 币制 价格（石）」）：汉字之间夹空格、格子至少三段、
    # 又只有十来个字。这种行常以「：」「）」收尾，只看标点会当成正文尾巴粘进上一句。
    # 门槛卡「三段 + 短」是为了放过族谱世系（「三子裕金 四子坤延 五子孝龙」也带空格，
    # 但那是**一个人**的完整记载，必须接回去 —— 2026-11-05 第一版没卡长度，
    # 族谱每卷凭空多出几百行）。
    if n_cur <= 16 and len(_SPACED.findall(cur)) >= 2:
        return False
    if n_prev < 10:
        return False                       # 标题/短行不往正文里塞
    # 下一行够长就是接着写的；短行只有在「它自己把句子收住了」时才算尾巴
    # （换行常常把最后几个字单独甩到下一行：「…高端健康住宅」/「引领者。」）
    if n_cur < 12 and not cur.endswith(tuple(_END_PUNCT)):
        return False
    return True


def book_layout(text: str) -> str:
    """把渲染结果排成「书的样子」：空行全去掉，正文段落首行缩进两格。

    之前导出是 `"\\n\\n".join(页)`，页内段落之间也留着空行 —— 读者在记事本里看到
    的是「每段之间空一行、段首顶格」，跟纸书正好相反。用户 2026-11-04 明确要求
    照书排：缩进两格、段间不空行。

    两件事，顺序不能反：
      1. **先接回被视觉换行截断的段落**。模型对整版正文常常一行一行吐
         （「…也是中」/「鹰集团董事长…」/「团的发展基石。…」），不接回去的话
         每行都缩进两格，读起来像三个段落，比不缩进更糟。
      2. 再缩进：短行、标题、目录条、表格行顶格，其余首行缩进两格。
    只做这两个变换，不改字、不改标点。
    """
    paras = []
    for ln in (text or "").splitlines():
        s = ln.strip("\u3000 \t\r")
        if not s:
            continue                      # 空行不要
        if paras and _joins(paras[-1], s):
            paras[-1] += s
        else:
            paras.append(s)
    return "\n".join(s if _flush_line(s) else INDENT + s for s in paras)


def book_layout_pages(pages) -> str:
    """逐页排版再拼起来 —— 页与页之间不接续。

    跨页接续是陷阱：上一页末尾常被截在半句（「…以」），下一页开头往往是**版心**
    （「全域旅游的溧阳高质量发展实践」），把两者粘起来等于把书名塞进句子中间。
    一页之内接续是安全的，跨页不接。
    """
    return "\n".join(book_layout(p) for p in pages if p and p.strip())

