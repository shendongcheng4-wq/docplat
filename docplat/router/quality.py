"""质量闸门 —— 判断「这一页读好了没有」。

v1 没有这个环节：跑完只能人工逐页看。v2 分两种模式：
  · 回归模式（有基准）：字符集覆盖率，见 routing_242.json 的口径
  · 生产模式（无基准）：用可疑信号 —— 空输出/外文字符/字数异常

生产模式的信号都不完美，但足以把「明显有问题」的页挑出来，
剩下靠 1% 抽样人工兜底。比逐页看几十万张现实得多。
"""
import re
from dataclasses import dataclass

from .. import config as C
from ..legacy import has_foreign
from .layout import BLANK, IMAGE, TABLE, TEXT

DONE, REVIEW, RETRY = "done", "review", "retry"


@dataclass
class Verdict:
    status: str
    score: float
    reason: str


def coverage(gold: str, pred: str) -> float:
    """字符集重合率 —— 免疫列序与分块差异（v1 的基准列序不可靠）。"""
    from collections import Counter
    if not gold or not pred:
        return 0.0
    inter = sum((Counter(gold) & Counter(pred)).values())
    return inter / max(len(gold), len(pred))


_PIPE_ONLY_RE = re.compile(r"^[\s|｜]+$")


def _empty_grid(text: str) -> bool:
    """整页是不是「只有表头标签 + 一片空格子」的空白表单模板。

    p58「市机关部门领导干部领导联系点登记表」、p132「党内民主评议表」都是这样：
    原图是没填写的表格，GLM 读对表头后连吐 9~13 行 `| | | |`。每行都不一样
    （都是空的），压缩冗余抓不到；但「行块重复」会把它当成退化 —— 其实是真内容，
    空格子就是这张表的样子。

    判据：管道行 ≥4，其中纯竖线/空白的行 ≥3 且占 60% 以上，剩下的字还能成句。
    上限 200 格是为了把「整页拆格崩成上千个空壳」那种真失败挡在外面。
    """
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    pipes = [ln for ln in lines if "|" in ln or "｜" in ln]
    if len(pipes) < 4:
        return False
    empty = [ln for ln in pipes if _PIPE_ONLY_RE.match(ln)]
    if len(empty) < 3 or len(empty) / len(pipes) < 0.6:
        return False
    cells = sum(ln.count("|") + ln.count("｜") for ln in pipes)
    if cells > 200:
        return False
    rest = "\n".join(ln for ln in lines if not _PIPE_ONLY_RE.match(ln))
    return len(re.sub(r"[\s|｜:=\-]", "", rest)) >= 10


def evaluate(rec, layout: str, gold: str = "", expect_chars: int = 0) -> Verdict:
    from ..legacy import text_len, text_only
    text = (rec.text or "").strip()
    # 字数一律按纯文本算：VL 的表格输出每格都带 style=... 标签，
    # p0202 按原串是 9608 字、纯文本不到 20 字，会把空表格误判成重复生成。
    n = text_len(text)

    # 空白页/影像页没字是正常的
    if not n:
        if layout in (BLANK, IMAGE):
            return Verdict(DONE, 1.0, "空白页/影像页，无正文属正常")
        return Verdict(REVIEW, 0.0, "未识别出任何文字")

    if layout == IMAGE:
        return Verdict(REVIEW, 0.3, "影像页却输出了文字，需核对")

    # ---- 回归模式 ----
    if gold:
        c = coverage(text_only(gold), text_only(text))
        if c >= C.COVER_OK:
            return Verdict(DONE, c, f"覆盖 {c*100:.0f}%")
        if c >= C.COVER_PART:
            return Verdict(REVIEW, c, f"覆盖 {c*100:.0f}%，偏低需抽查")
        return Verdict(REVIEW, c, f"覆盖 {c*100:.0f}%，疑似漏读")

    # ---- 生产模式 ----
    if has_foreign(text):
        return Verdict(REVIEW, 0.2, "混入假名/谚文等外文字符，疑似草书误识")

    if n < C.SHORT_CHARS and layout == TEXT:
        return Verdict(REVIEW, 0.3, f"仅 {n} 字，大字/草书/题签版面")

    # 模型卡死退化：同一片段反复输出。实测正常页压缩冗余中位 0.28，
    # 退化页 0.97~0.99（p0147 的 `子二`×1967、p0188 的 `奏請`×2020、
    # p0154 把六个名字洗成 821 格），中间一个样本都没有，阈值很好定。
    red = _redundancy(text)
    if red >= C.DEGEN_REDUNDANCY:
        return Verdict(REVIEW, 0.1,
                       f"疑似重复生成（压缩冗余 {red:.2f}，正常页约 0.28），需重跑或人工")

    # 空白表单模板：表头读对了，格子本来就是空的（见 _empty_grid）。
    # 要在「整块重复」之前判，否则那一片空格子会被当成退化。
    if _empty_grid(text):
        return Verdict(DONE, 0.9, f"空白表格（表单未填写，{n} 字表头）")

    # 整块重复：每行都不重复，只有「行块」重复，上面的冗余度抓不到
    bratio, bhits, bsample = _block_rep(text)
    if bratio >= C.BLOCK_REP_RATIO:
        return Verdict(REVIEW, 0.15,
                       f"疑似整块重复生成（{bhits} 行成块重复，占 {bratio*100:.0f}%，"
                       f"如「{bsample}」），需重跑或人工")

    # 表格页几乎没读出字 —— 不是"这页字少"，而是模型把图内小字漏了。
    # 实测：p0131 世系树 109 个字块只吐出 8 字，p0205 服制图 305 个字块只吐出 3 字。
    if layout == TABLE and n < C.TABLE_MIN_CHARS:
        return Verdict(REVIEW, 0.2, f"表格页仅 {n} 字，疑似图内小字整片漏识")

    # 空壳网格：拆出成百上千个单元格却几乎全空
    cells, filled = _grid_stats(text)
    if cells >= C.GRID_CELLS_MIN and filled / cells < C.GRID_FILL_MIN:
        return Verdict(REVIEW, 0.2,
                       f"拆出 {cells} 个单元格但只有 {filled/cells*100:.0f}% 有字，疑似拆格失败")

    if expect_chars and n > expect_chars * C.BLOAT_RATIO:
        return Verdict(REVIEW, 0.2,
                       f"输出 {n} 字，超预期 {expect_chars} 字的 {C.BLOAT_RATIO:.0f} 倍，疑似重复生成")

    if n > 6000:
        return Verdict(REVIEW, 0.3, f"输出 {n} 字异常长，疑似重复生成")

    return Verdict(DONE, 1.0, "正常")


_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _redundancy(text: str) -> float:
    from ..legacy import redundancy_of
    return redundancy_of(text)


_LINE_BREAK_RE = re.compile(r"<br\s*/?>|</(?:tr|p|div|li|h[1-6]|table)>", re.I)
_CELL_END_RE = re.compile(r"</t[dh]>", re.I)


def lines_of(text: str) -> list:
    """把模型输出切成「视觉行」。

    HTML 表格按行切（一行 = 一条记录），普通正文按换行切。这样后续的
    块重复检测对两种输出用同一套口径。
    """
    from ..legacy import text_only
    s = _LINE_BREAK_RE.sub("\n", text or "")
    s = _CELL_END_RE.sub(" ", s)
    s = text_only(s)
    return [ln.strip() for ln in s.split("\n") if ln.strip()]


_THIN_CHARS = set(" \t\u3000-—－_=＝·・○〇0０.'\"`~／/\\")


def _thin(line: str) -> bool:
    """占位行：只有横线/圆圈/零/空格 —— 这类的重复是真表格的常态，不算退化。"""
    s = line.strip()
    return len(s) <= 1 or all(c in _THIN_CHARS for c in s)


def _block_rep(text: str) -> tuple:
    """同一段多行内容在页内反复出现 → (占比, 涉及行数, 示例行)。

    与 `_redundancy` 的分工：那个抓「一个字串重复上千次」，这个抓「一行块
    被复制好几遍」，而且**不要求相邻** —— p160 是 8 行数值块在 7 个网具之间
    各出现一次（中间夹着「钩卡/每船」这类标签行），连续块检测永远碰不到。

    p160 实测：k=8 覆盖 84 行 / 155 行 = 0.54；k=2 只有 0.09。
    取各 k 的最大覆盖，短块的碎片就不会把长块的信号淹掉。

    阈值偏保守是故意的：真数据里「同一段文字反复出现」很常见（族谱捐款名录
    每笔都是「懷德堂/伍佰元」、辈分字表整页同字），漏报比误报代价小 ——
    漏了还有 1% 抽样兜底，误报会把复核队列灌满、人就真不看了。
    """
    lines = lines_of(text)
    n = len(lines)
    if n < C.BLOCK_REP_KMIN * C.BLOCK_REP_TIMES:
        return 0.0, 0, ""
    best = (0.0, 0, "")
    for k in range(C.BLOCK_REP_KMIN, C.BLOCK_REP_KMAX + 1):
        if n < k * C.BLOCK_REP_TIMES:
            break
        where = {}
        for i in range(n - k + 1):
            blk = tuple(lines[i:i + k])
            if all(_thin(x) for x in blk):
                continue
            where.setdefault(blk, []).append(i)
        covered, sample = set(), ""
        for blk, pos in where.items():
            if len(pos) < C.BLOCK_REP_TIMES:
                continue
            last = -1
            for i in pos:                      # 非重叠取用
                if i >= last:
                    covered.update(range(i, i + k))
                    last = i + k
                    if not sample:
                        sample = blk[0][:20]
        if covered and len(covered) / n > best[0]:
            best = (len(covered) / n, len(covered), sample)
    return best


def _grid_stats(text: str) -> tuple:
    """(单元格数, 有字单元格数)。非 HTML 表格返回 (0, 0)。"""
    from ..legacy import text_only
    cells = _TD_RE.findall(text or "")
    if not cells:
        return 0, 0
    filled = sum(1 for c in cells if text_only(c).strip())
    return len(cells), filled


def expect_from_layout(layout: str) -> int:
    """粗估一页应该有多少字 —— 只用于识别「暴涨」。"""
    return 0 if layout in (BLANK, IMAGE) else 400
