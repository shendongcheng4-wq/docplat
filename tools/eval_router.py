"""评估结构闸门 —— 唯一一个「这个分类器到底靠不靠谱」的量化答案。

数据来源有三处，必须都说清，否则数字没意义：
  · GLM 输出     $DOCPLAT_GLM_JSON 指向的 result.json（242 页 A/B 实测）
  · 原始标签     tests/fixtures/routing_242.json 的 kind（按 v1 输出里有没有 <table> 标）
  · 人工修正     MANUAL 表。用 ViewImage 逐张看过图后的结论。

为什么要人工修正：原标签把「世系行传 / 目录 / 服制图」都标成了 text，
因为它们没有 `<table>` 标签。这些页在人工眼里就是分格条目。不修正的话，
分类器会被冤枉成 76% 精确率；修正后才是真实水平。

用法
  python tools/eval_router.py
  python tools/eval_router.py --raw      # 用未修正的标签（对照用）
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docplat import config as C                     # noqa: E402
from docplat.router import structure_of             # noqa: E402

GLM = RESULT_JSON
ROUTING = ROOT / "tests" / "fixtures" / "routing_242.json"
TRUTH = ROOT / "tests" / "fixtures" / "layout_truth.json"

SEC_GLM, SEC_VL = 1.95, 35.2


def manual_corrections() -> dict:
    """人工看图核对后的标签修正（data/layout_truth.json）。"""
    if not TRUTH.exists():
        return {}
    d = json.loads(TRUTH.read_text(encoding="utf-8"))
    return {r["id"]: r["kind"] for r in d.get("pages", []) if r.get("kind")}


def load():
    MAN = manual_corrections()
    glm = {r["id"]: r for r in json.loads(GLM.read_text(encoding="utf-8"))["pages"]}
    routing = {r["id"]: r for r in
               json.loads(ROUTING.read_text(encoding="utf-8"))["pages"]}
    rows = []
    for pid, r in routing.items():
        if pid not in glm:
            continue
        kind = r["kind"]
        if kind not in ("table", "text"):
            continue
        rows.append({
            "id": pid,
            "kind_raw": kind,
            "kind": MAN.get(pid, kind),
            "text": glm[pid].get("pred") or "",
            "sec": glm[pid].get("sec") or SEC_GLM,
        })
    return rows


def main():
    raw = "--raw" in sys.argv
    rows = load()
    for r in rows:
        if raw:
            r["kind"] = r["kind_raw"]
        s = structure_of(r["text"])
        r["gate"] = s.structured
        r["brief"] = s.brief()

    tab = [r for r in rows if r["kind"] == "table"]
    txt = [r for r in rows if r["kind"] == "text"]
    tp = sum(1 for r in tab if r["gate"])
    fp = sum(1 for r in txt if r["gate"])
    fn = [r for r in tab if not r["gate"]]
    rec, prec = tp / max(1, len(tab)), tp / max(1, tp + fp)

    tag = "（未修正标签）" if raw else "（人工修正标签）"
    print(f"{len(rows)} 页{tag}：结构化 {len(tab)} / 正文 {len(txt)}")
    print(f"  阈值 行字中位<={C.STRUCT_MED_MAX} 行数>={C.STRUCT_LINES_MIN} "
          f"短行占比>={C.STRUCT_SHORT_RATIO}")
    print(f"\n  召回 {tp}/{len(tab)} = {rec*100:.1f}%   误报 {fp}/{len(txt)} = "
          f"{fp/max(1,len(txt))*100:.1f}%   精确率 {prec*100:.1f}%")

    if fn:
        print(f"\n  漏检 {len(fn)} 页（这些页会保留 GLM 的扁平输出）：")
        for r in sorted(fn, key=lambda x: x["id"]):
            print(f"    {r['id']}  {r['brief']}")

    if fp:
        print(f"\n  误报 {len(fp)} 页（白跑一次 VL，只损失时间）：")
        for r in sorted([r for r in txt if r["gate"]], key=lambda x: x["id"]):
            print(f"    {r['id']}  {r['brief']}  {r['sec']:.1f}s")

    n = len(rows)
    esc = tp + fp
    cost_new = (n * SEC_GLM + esc * SEC_VL) / 60
    cost_all_glm = n * SEC_GLM / 60
    cost_all_vl = n * SEC_VL / 60
    print(f"\n  全量 VL      : {cost_all_vl:6.1f} 分钟")
    print(f"  全量 GLM     : {cost_all_glm:6.1f} 分钟（{len(tab)} 页结构丢失）")
    print(f"  闸门 + 升级  : {cost_new:6.1f} 分钟"
          f"（{esc} 页升级，其中 {fp} 页属于白跑）")
    print(f"  相对全量 VL  : {cost_all_vl/cost_new:.1f}x 提速，"
          f"相对全量 GLM 慢 {cost_new/cost_all_glm:.2f}x")


if __name__ == "__main__":
    main()
