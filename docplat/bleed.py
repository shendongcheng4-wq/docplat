"""空白页的「背面透印」判定。

## 问题

纸太薄时，本页（实体上是空白）会把**同一张纸另一面**（紧邻页）的印刷透出来，
而且是左右镜像。模型对这类页有两种反应，两种都会污染结果：

  · 读成一串乱码 —— `业业业业`、`址 址 址`、`休史業布面紫的慰藉的腆物`
  · 干脆编一段 —— 目录页编出几百条「影响因素和影响」、跑题写成政治试题

## 判据一：这页有没有「正印的深墨」

正印墨直接印在纸上，能深到灰阶 20~60；透印墨隔着中间那层纸，最深处也只到
「纸色以下 60~150」，**永远不出现大面积深墨**。这是物理约束，不是调参。

    c   = percentile(gray,90) - percentile(gray,0.1)   纸色与最深墨的落差
    d140= (gray < 纸色-140) 的像素占比                  真正印深的墨有多少

纸张色取 90 分位而不是中位数：彩色画册、整版照片的书，深色图块会把中位数
拉到 100 上下，「纸」和「墨」的落差被压没，真正的正文页也会被误判成淡页
（天目湖之旅 p5 有 1132 字正文，中位数基准下 c 只有 82，取亮部则是 232）。

## 判据二：文本有没有「实义」

光看墨淡不够 —— 章扉页常用浅灰大字印，比透印页还淡
（全区域旅游 p23「第一章 深化改革背景下的全域旅游与县域治理」，c 只有 40，
而商业史料那张透印内衬 c 是 75）。这两者只能靠文本分。

用**本书自己的双字词表**：正常页的用词与全书共享，透印乱码则是随机字序，
双字组合几乎全不命中。实测 p2 覆盖率 0.00，章扉页 0.47~0.71，正常正文 0.09+。

## 拿不准就不删

分类结果是三值的。None（拿不准）不进正文也不删，留在库里给人看 ——
漏删一页只是多一条待复核，误删一页就是丢了一页正文。
"""
import math
import re
from collections import Counter

import cv2
import numpy as np

# 判「墨淡」时相对全书参照值的系数。
FAINT_RATIO = 0.80
# 参照值的下限：书整体拍得很淡时（纸色 150 上下），别把阈值压到没意义。
REF_FLOOR = 100.0
# 深墨占比超过这个百分比，就认定「本页确实印了东西」，无论 c 多低。
DEEP_GUARD = 0.5
# 双字词覆盖率低于「全书正常页中位数」的这个比例，判为随机字序。
LEX_RATIO = 0.30
# 词表取全书最高频的多少个双字组合。
LEX_TOP = 300


def _read(path, width=560):
    """按固定宽度读灰度图。中文路径下 cv2.imread 会静默失败，必须 fromfile。"""
    im = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if im is None:
        return None
    h, w = im.shape
    return cv2.resize(im, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)


def features(path, width=560):
    """单页墨色特征。读不到图时返回 None（调用方按「不确定」处理，别当空白删）。"""
    im = _read(path, width)
    if im is None:
        return None
    g = im.astype(np.float32)
    bg = float(np.percentile(g, 90))
    return {
        "c": bg - float(np.percentile(g, 0.1)),
        "d140": float((g < bg - 140).mean() * 100),
        "bg": bg,
    }


def doc_reference(feats):
    """全书参照值：正常正印页应有的墨深度。"""
    vals = [f["c"] for f in feats.values() if f]
    if not vals:
        return None
    return max(float(np.percentile(vals, 75)), REF_FLOOR)


# ---- 文本侧 ---------------------------------------------------------------

def _cn(text):
    return [ch for ch in text if "\u4e00" <= ch <= "\u9fff"]


def _bigrams(text):
    ch = _cn(text)
    return [ch[i] + ch[i + 1] for i in range(len(ch) - 1)]


def _lexicon(texts):
    c = Counter()
    for t in texts:
        c.update(_bigrams(t))
    return {b for b, _ in c.most_common(LEX_TOP)}


def _lex_cov(text, lex):
    b = _bigrams(text)
    if not b:
        return 0.0
    return sum(1 for x in b if x in lex) / len(b)


def table_cells(text):
    """带内容的表格单元格个数。

    浅色油墨印的宽表墨也淡（商业志 p167「品名/1/2/3」、p326「黄锦凤/女/1943」），
    但它是真内容，不能删。空白页/透印页只会吐一张全空的表（`<th></th><td></td>`），
    真表页一定会填进字或数 —— 这是「模型确实看见过东西」的硬证据。
    """
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", text, re.S | re.I)
    n = sum(1 for c in cells if re.sub(r"<[^>]+>|\s", "", c))
    if n == 0:
        for line in text.splitlines():
            if line.count("|") >= 3 and re.search(r"[^\s|:\-]", line):
                n += line.count("|") - 1
    return n


# ---- 主流程 ---------------------------------------------------------------

def scan(rows, width=560):
    """rows: [(page_no, src_path, text), ...] → {page_no: (verdict, reason)}

    verdict: True=空白/透印（该删）  False=有正文  None=拿不准（留给人看）
    """
    feats = {pn: features(sp, width) for pn, sp, _ in rows}
    ref = doc_reference(feats)
    texts = {pn: t or "" for pn, _, t in rows}

    verdict = {}
    if ref is None:
        return {pn: (None, "读图失败") for pn, _, _ in rows}

    # 第一步：墨够深 / 有大片深墨 → 有正文。只有「墨淡」的页才需要看文本。
    faint = []
    for pn, _, _ in rows:
        f = feats[pn]
        if f is None:
            verdict[pn] = (None, "读图失败")
        elif f["d140"] >= DEEP_GUARD:
            verdict[pn] = (False, "有大片深墨")
        elif f["c"] >= FAINT_RATIO * ref:
            verdict[pn] = (False, "墨深正常")
        else:
            faint.append(pn)

    # 第二步：用「已经确认有正文」的页建词表 —— 不让乱码页污染语料。
    pool = [texts[pn] for pn in texts
            if pn not in faint and len(_cn(texts[pn])) >= 40]
    lex = _lexicon(pool) if pool else set()
    base = 0.0
    if lex:
        covs = sorted(_lex_cov(texts[pn], lex) for pn in texts if pn not in faint
                      and len(_cn(texts[pn])) >= 40)
        if covs:
            base = covs[len(covs) // 2]          # 正常页覆盖率的中位数

    for pn in faint:
        t = texts[pn]
        if table_cells(t) >= 3:
            verdict[pn] = (None, "墨淡但填出了表格")
            continue
        n = len(_cn(t))
        if n == 0:
            verdict[pn] = (True, "墨淡且无字")
            continue
        if not lex or base <= 0:
            # 没有干净的语料可参照（整本都淡）→ 不下结论
            verdict[pn] = (None, "墨淡，缺语料无法比对")
            continue
        cov = _lex_cov(t, lex)
        if cov < LEX_RATIO * base:
            verdict[pn] = (True, f"墨淡且字序随机（词覆盖 {cov:.2f} < {LEX_RATIO * base:.2f}）")
        else:
            verdict[pn] = (None, f"墨淡但用词正常（词覆盖 {cov:.2f}）")
    return verdict
