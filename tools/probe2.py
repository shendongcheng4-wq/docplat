"""量化探针 v2：按「长且细」筛格线，统计每页的格线数与闭合格子数。

关键修正：上一版只按长度筛，文字列（宽 20-30px 的连续墨块）也被当成竖线
——p0001 竖线像素 43590，比真表格 p0153 的 9932 还多。格线的本质特征是
「又长又细」（宽度 1-4px），因此必须加线宽约束。
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
IMG = IMAGES
ROUTING = ROOT / "tests" / "fixtures" / "routing_242.json"

WORK_W = 1000          # 提到 1000：字宽 ~38px，格线仍是 2-4px，线宽判别余量更大
MARGIN = 0.05


def load(path):
    raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None:
        return None
    h, w = raw.shape
    g = cv2.resize(raw, (WORK_W, max(1, int(h * WORK_W / w))), interpolation=cv2.INTER_AREA)
    hh, ww = g.shape
    mx, my = int(ww * MARGIN), int(hh * MARGIN)
    return g[my:hh - my, mx:ww - mx]


def ink_mask(g):
    b = cv2.GaussianBlur(g, (3, 3), 0)
    bw = cv2.adaptiveThreshold(b, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 31, 15)
    return bw


def line_components(bw, vertical, min_len_frac):
    """抽长细线。返回 (条数, 总长占比, 平均线宽)。"""
    h, w = bw.shape
    if vertical:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, int(h * 0.06))))
        span, thick_axis = h, "x"
    else:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, int(w * 0.06)), 1))
        span, thick_axis = w, "y"
    m = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k, iterations=1)
    n, lab, stats, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    need = span * min_len_frac
    keep, widths, lens = 0, [], []
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        length = ch if vertical else cw
        thick = cw if vertical else ch
        if length < need or thick > 6:
            continue
        # 线宽：面积 / 长度，比 bbox 更准（斜一点也不至于判错）
        tw = area / max(1, length)
        if tw > 6:
            continue
        keep += 1
        widths.append(tw)
        lens.append(length / span)
    return keep, (sum(lens) if lens else 0.0), (float(np.mean(widths)) if widths else 0.0)


def cells(bw):
    h, w = bw.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w * 0.10)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(h * 0.04))))
    hor = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, hk)      # close 补断口，让格子能闭合
    ver = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    grid = cv2.bitwise_or(hor, ver)
    grid = cv2.dilate(grid, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(grid, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    ok = 0
    for c in cnts:
        a = cv2.contourArea(c)
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.02 or ch < h * 0.02:      # 格子不可能比页面 2% 还小
            continue
        if a < w * h * 0.0008:
            continue
        if a / float(cw * ch) < 0.7:
            continue
        ok += 1
    return ok


def feats(path):
    g = load(path)
    if g is None:
        return None
    bw = ink_mask(g)
    vn, vl, vw = line_components(bw, True, 0.30)
    hn, hl, hw = line_components(bw, False, 0.22)
    return {"v": vn, "vl": round(vl, 2), "vw": round(vw, 2),
            "h": hn, "hl": round(hl, 2), "hw": round(hw, 2),
            "cells": cells(bw), "ink": round(float((bw > 0).mean()), 3)}


def main():
    data = json.loads(ROUTING.read_text(encoding="utf-8"))
    rows = []
    for r in data["pages"]:
        p = IMG / f"{r['id']}.jpg"
        if not p.exists():
            continue
        f = feats(p)
        if f:
            f["id"], f["kind"] = r["id"], r["kind"]
            rows.append(f)

    tab = [r for r in rows if r["kind"] == "table"]
    txt = [r for r in rows if r["kind"] == "text"]
    print(f"{len(rows)} 页  table {len(tab)} / text {len(txt)}\n")

    keys = ["v", "vl", "vw", "h", "hl", "hw", "cells", "ink"]
    print(f"{'kind':<6}" + "".join(f"{k:>8}" for k in keys) + "   ← 中位数")
    import statistics as st
    for name, grp in (("table", tab), ("text", txt)):
        print(f"{name:<6}" + "".join(f"{st.median([r[k] for r in grp]):>8.2f}" for k in keys))

    print("\n表格页（按 cells 降序）")
    for r in sorted(tab, key=lambda x: -x["cells"]):
        print(f"  T {r['id']} " + " ".join(f"{k}={r[k]}" for k in keys))
    print("\n正文页中 cells 最高的 20 个")
    for r in sorted(txt, key=lambda x: -x["cells"])[:20]:
        print(f"  . {r['id']} " + " ".join(f"{k}={r[k]}" for k in keys))


if __name__ == "__main__":
    main()
