"""版面分类器的特征提取 + 阈值评估。

设计前提（2026-10-30 实测 239 页得出，改动前先读）
--------------------------------------------------
1) 这本书的正文页也有「版框」（四周黑边），所以**总线条数**在两堆里都是 4-6，
   完全没有区分度。必须只看**框内格线**，并且要按线宽区分：
   真格线宽 1-2.5% 页宽，文字列的墨块宽 3-6% 页宽。
2) 竖排正文的「界行」（列间细线）在别的书里会有——那种页竖线多但没有横行线。
   所以只认**竖+横同时存在**的栅格，不认单方向竖线。
3) 图像侧只能判「有栅格表格」。无格线的世系行传（如 p0146）图像上跟正文
   几乎无法区分，交给文本侧兜底。
4) 文本侧（GLM 输出后，免费）：表格页行字中位 5、行数中位 28；正文页 15 / 10。
   单用文本侧表格召回 95%、正文误报 33%，所以要和图像侧合起来投票。

产物
  data/layout_features.json   每页特征（缓存，评估/调参直接读它）
"""
import json
import re
import statistics as st
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
IMG = IMAGES
ROUTING = ROOT / "tests" / "fixtures" / "routing_242.json"
GLM_JSON = RESULT_JSON
CACHE = ROOT / "data" / "layout_features.json"

WORK_W = 1000
MARGIN = 0.05
EDGE = 0.03          # 距裁边 3% 以内算版框，不计入"框内格线"
THICK = 0.025        # 线宽上限（占页宽/页高比例）
MIN_LEN = 0.30       # 线长下限（占页高/页宽比例）
KERNEL = 0.06


def load_gray(path: Path):
    # cv2.imread 在 Windows 中文路径下静默返回 None，必须走 imdecode
    raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None:
        return None
    h, w = raw.shape
    g = cv2.resize(raw, (WORK_W, max(1, int(h * WORK_W / w))), interpolation=cv2.INTER_AREA)
    hh, ww = g.shape
    mx, my = int(ww * MARGIN), int(hh * MARGIN)
    return g[my:hh - my, mx:ww - mx]


def extract(path: Path) -> dict:
    g = load_gray(path)
    if g is None:
        return {}
    h, w = g.shape
    b = cv2.GaussianBlur(g, (3, 3), 0)
    bw = cv2.adaptiveThreshold(b, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 31, 15)

    out = {"w": w, "h": h, "ink": round(float((bw > 0).mean()), 4)}

    for name, vert in (("v", True), ("h", False)):
        kw = (1, max(12, int(h * KERNEL))) if vert else (max(12, int(w * KERNEL)), 1)
        m = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, kw))
        n, _, stats, _ = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
        span = h if vert else w
        edge_px = (w if vert else h) * EDGE
        inner = outer = 0
        inner_cov = 0.0
        for i in range(1, n):
            x, y, cw, ch, area = stats[i]
            length = ch if vert else cw
            thick = cw if vert else ch
            if length < span * MIN_LEN:
                continue
            if thick > (w if vert else h) * THICK:
                continue
            pos = x + cw / 2 if vert else y + ch / 2
            if pos < edge_px or pos > (w if vert else h) - edge_px:
                outer += 1
            else:
                inner += 1
                inner_cov += length / span
        out[name + "_in"] = inner
        out[name + "_out"] = outer
        out[name + "_cov"] = round(inner_cov, 2)
    return out


def text_feats(pred: str) -> dict:
    t = re.sub(r"<[^>]+>", "", pred or "")
    lines = [re.sub(r"\s", "", ln) for ln in t.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return {"t_lines": 0, "t_med": 0.0, "t_short": 1.0}
    L = [len(x) for x in lines]
    return {"t_lines": len(L), "t_med": float(st.median(L)),
            "t_short": round(sum(1 for x in L if x <= 6) / len(L), 2)}


def build_cache(refresh=False):
    if CACHE.exists() and not refresh:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if len(d.get("pages", {})) > 200:
            return d
    data = json.loads(ROUTING.read_text(encoding="utf-8"))
    glm = {r["id"]: r for r in json.loads(GLM_JSON.read_text(encoding="utf-8"))["pages"]}
    pages = {}
    for i, r in enumerate(data["pages"], 1):
        pid = r["id"]
        p = IMG / f"{pid}.jpg"
        if not p.exists():
            continue
        f = extract(p)
        if not f:
            continue
        f["kind"] = r["kind"]
        f["channel"] = r["channel"]
        f.update(text_feats((glm.get(pid) or {}).get("pred", "")))
        pages[pid] = f
        if i % 40 == 0:
            print(f"  {i}/{len(data['pages'])}", flush=True)
    CACHE.write_text(json.dumps({"meta": {"work_w": WORK_W, "margin": MARGIN,
                                          "edge": EDGE, "thick": THICK,
                                          "min_len": MIN_LEN},
                                 "pages": pages}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return {"pages": pages}


def confusion(rows, pred, label="table"):
    tp = sum(1 for r in rows if pred(r) and r["kind"] == label)
    fp = sum(1 for r in rows if pred(r) and r["kind"] != label)
    fn = sum(1 for r in rows if not pred(r) and r["kind"] == label)
    tn = sum(1 for r in rows if not pred(r) and r["kind"] != label)
    n_lab = tp + fn
    rec = tp / n_lab if n_lab else 0
    prec = tp / (tp + fp) if tp + fp else 0
    return tp, fp, fn, tn, rec, prec


def main():
    refresh = "--refresh" in sys.argv
    d = build_cache(refresh)
    pages = d["pages"]
    rows = [dict(v, id=k) for k, v in pages.items()]
    tab = [r for r in rows if r["kind"] == "table"]
    txt = [r for r in rows if r["kind"] == "text"]
    print(f"\n{len(rows)} 页：table {len(tab)} / text {len(txt)}\n")

    keys = ["v_in", "v_out", "h_in", "h_out", "v_cov", "h_cov", "ink",
            "t_lines", "t_med", "t_short"]
    print(f"{'kind':<6}" + "".join(f"{k:>8}" for k in keys) + "   中位数")
    for name, grp in (("table", tab), ("text", txt)):
        print(f"{name:<6}" + "".join(f"{st.median([r[k] for r in grp]):>8.2f}" for k in keys))

    print("\n── 图像侧：(框内竖线 >= a) 且 (框内横线 >= b) ──")
    best = None
    for a in range(0, 7):
        for b in range(0, 7):
            if a == 0 and b == 0:
                continue
            tp, fp, fn, tn, rec, prec = confusion(rows, lambda r: r["v_in"] >= a and r["h_in"] >= b)
            score = rec * prec
            if best is None or score > best[0]:
                best = (score, a, b, rec, prec, tp, fp, fn)
            if a in (1, 2, 3) and b in (1, 2, 3):
                print(f"  v_in>={a} h_in>={b}  召回 {rec*100:5.1f}%  精确 {prec*100:5.1f}%  "
                      f"TP{tp:>3} FP{fp:>3} FN{fn:>3}")
    print(f"  最优：v_in>={best[1]} h_in>={best[2]}  召回 {best[3]*100:.1f}% 精确 {best[4]*100:.1f}%")

    print("\n── 文本侧：行字中位 <= m 且 行数 >= n ──")
    for m in (6, 8, 10, 12):
        for n in (10, 15, 20, 25):
            tp, fp, fn, tn, rec, prec = confusion(rows, lambda r: r["t_med"] <= m and r["t_lines"] >= n)
            print(f"  t_med<={m:<3} t_lines>={n:<3} 召回 {rec*100:5.1f}%  精确 {prec*100:5.1f}%  "
                  f"TP{tp:>3} FP{fp:>3} FN{fn:>3}")


if __name__ == "__main__":
    main()
