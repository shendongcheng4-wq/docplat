"""用 PP-DocLayout 预训练版面检测器做「本页有没有表格」的判定，并在 239 页上评估。

为什么换掉手写几何规则
----------------------
手调「长直线 / 线宽 / 栅格」在这本书上实测精确率只有 17-28%（见上版 eval_layout.py）：
古籍的竖排文字列本身就是连续墨块，真格线和文字列在形态上重叠。这条路 2026-10-30
已是第三次失败，不再继续。

PP-DocLayout-S（PaddleOCR 自带，Apache-2.0）是预训练检测器，CPU 上 0.2-0.3s/页，
不占显存（GLM 在跑的时候也能跑），不需要调参。这才是这个任务的成熟方案。

已知代价：paddle 3.3.1 的 oneDNN 内核在 PIR 执行器上有 bug，
必须 enable_mkldnn=False 才能跑（否则 NotImplementedError: ConvertPirAttribute2RuntimeAttribute）。

产物 data/layout_ppdoc.json（缓存每页的表格框数与全部标签）
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
IMG = IMAGES
ROUTING = ROOT / "tests" / "fixtures" / "routing_242.json"
OUT = ROOT / "data" / "layout_ppdoc.json"

TABLE_LABELS = {"table", "table_title", "table_caption", "table_footnote",
                "表格", "表格标题", "表格说明"}


def detect(ids, model_name="PP-DocLayout-S"):
    from paddleocr import LayoutDetection
    m = LayoutDetection(model_name=model_name, device="cpu", enable_mkldnn=False)
    res = {}
    t0 = time.time()
    for i, pid in enumerate(ids, 1):
        p = IMG / f"{pid}.jpg"
        if not p.exists():
            continue
        r = m.predict(str(p))
        boxes = []
        for one in r:
            boxes = ((one.json or {}).get("res") or {}).get("boxes", [])
        labels = [b["label"] for b in boxes]
        n_tab = sum(1 for l in labels if l in TABLE_LABELS)
        res[pid] = {"labels": dict(Counter(labels)), "n_table": n_tab,
                    "n_boxes": len(boxes)}
        if i % 25 == 0:
            print(f"  {i}/{len(ids)}  {time.time()-t0:.0f}s", flush=True)
    return res, time.time() - t0


def main():
    refresh = "--refresh" in sys.argv
    model = "PP-DocLayout-L" if "--large" in sys.argv else "PP-DocLayout-S"
    data = json.loads(ROUTING.read_text(encoding="utf-8"))
    kinds = {r["id"]: r["kind"] for r in data["pages"]}
    ids = [r["id"] for r in data["pages"]]

    if OUT.exists() and not refresh:
        res = json.loads(OUT.read_text(encoding="utf-8"))["pages"]
        print(f"用缓存 {OUT}")
        sec = 0.0
    else:
        print(f"跑 {model} 扫 {len(ids)} 页…")
        res, sec = detect(ids, model)
        OUT.write_text(json.dumps({
            "meta": {"model": model, "pages": len(res),
                     "seconds": round(sec, 1),
                     "sec_per_page": round(sec / max(1, len(res)), 3),
                     "note": "enable_mkldnn=False（paddle 3.3.1 oneDNN bug）"},
            "pages": res}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"✓ {OUT}  {sec:.0f}s  {sec/len(res):.2f}s/页")

    tab = [k for k in res if kinds.get(k) == "table"]
    txt = [k for k in res if kinds.get(k) == "text"]
    img = [k for k in res if kinds.get(k) == "image"]

    def hit(k):
        return res[k]["n_table"] > 0

    tp = sum(1 for k in tab if hit(k))
    fp = sum(1 for k in txt if hit(k))
    print(f"\n真值 table {len(tab)} / text {len(txt)} / image {len(img)}")
    print(f"表格召回 {tp}/{len(tab)} = {tp/max(1,len(tab))*100:.1f}%")
    print(f"正文误报 {fp}/{len(txt)} = {fp/max(1,len(txt))*100:.1f}%")
    print(f"精确率 {tp/max(1,tp+fp)*100:.1f}%")

    miss = [k for k in tab if not hit(k)]
    if miss:
        print(f"\n漏检 {len(miss)} 页：{', '.join(sorted(miss))}")
        for k in sorted(miss)[:8]:
            print(f"   {k} labels={res[k]['labels']}")
    fp_list = [k for k in txt if hit(k)]
    if fp_list:
        print(f"\n误报 {len(fp_list)} 页：{', '.join(sorted(fp_list))}")

    print("\n全部标签频次（前 15）")
    allc = Counter()
    for v in res.values():
        allc.update(v["labels"])
    for l, c in allc.most_common(15):
        print(f"  {l:<16}{c}")


if __name__ == "__main__":
    main()
