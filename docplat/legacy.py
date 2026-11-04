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
    return "\n".join(lines)


def _cells_of(html: str) -> list[list[str]]:
    """把一段 <table> 拆成 [[单元格文本, ...], ...]，每格已归一且压过重复。"""
    rows = []
    for tr in ROW_RE.findall(html):
        row = []
        for c in CELL_RE.findall(tr):
            c = BR_RE.sub("\n", _entities(c))
            c = text_only(c).strip()
            c = re.sub(r"[ \t\u3000]+", " ", c)
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


def _md_tables(s: str, tables: str, width: int) -> str:
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


def render_text(text: str, tables: str = "auto", width: int = 40) -> str:
    """把一页原始输出渲染成人能读的文本。

    tables="auto"：规整的分格表还原成 markdown 管道表，其余（一行几百格、
    单格几千字、列数参差）改成分行文字。
    tables="flat"：一律分行文字（工作台展示用 —— 复核时看格子，不如看行）。

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
            parts.append(_as_md_table(rows)
                         if (tables == "auto" and _is_grid(rows))
                         else _flatten_rows(rows, width))
        last = m.end()
    parts.append(_prose(s[last:]))
    s = re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()
    # 管道表最后处理：_prose 会把 `| --- | --- |…` 这种分隔行当退化重复压成
    # `| :--- ……（重复 8 次）|`，_is_md_sep 能认这个形态，所以放后面也能收拾干净。
    s = _md_tables(s, tables, width)

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
