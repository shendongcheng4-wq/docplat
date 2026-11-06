"""从当前库状态生成「透印/空白判据」的回归样本 tests/fixtures/bleed_cases.json。

为什么要它：`bleed.py` 每改一次判据，验证方式都是「全库重跑 + 人眼抽 3000 页」。
这里把**已经被人确认过的现状**冻成一份样本（该删的页 / 该留的页 / 拿不准的页），
以后改判据只跑 `tools/selftest_bleed.py`，就能看出有没有把该留的删掉。

⚠️ 只有当用户已经确认过当前库状态（交付 txt 逐本核过）时才该重新生成，
否则冻进去的就是错的。

    python tools/make_bleed_fixture.py            # 只报告会取哪些样本
    python tools/make_bleed_fixture.py --write    # 写进 tests/fixtures/

图片路径按「书籍根目录」存成相对路径（默认 ~/Desktop/书籍，可用
DOCPLAT_BOOKS 覆盖），这样夹具里不会出现 C:\\Users\\<用户名> 这类本机路径。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docplat import bleed, pipeline, store          # noqa: E402
from docplat.router import IMAGE                    # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bleed_cases.json"
BOOKS_ROOT = Path(os.environ.get("DOCPLAT_BOOKS") or (Path.home() / "Desktop" / "书籍"))

# 每个文档取多少：该删的优先取「原本有字」的（那才是判据真正难的地方），
# 该留的优先取墨最淡的（离阈值最近，改判据最先翻车的也是它们）。
N_BLANK, N_TEXT, N_UNSURE = 8, 6, 5

# 人工点过名的页，无论抽样结果如何都要进样本
MUST = [("商业史料", 170), ("全区域旅游", 23)]


def _rel(src: str) -> str:
    p = Path(src)
    try:
        return p.relative_to(BOOKS_ROOT).as_posix()
    except ValueError:
        return str(p)                   # 不在书籍根下：原样存，跑测试时按绝对路径读


def sample_doc(doc_id: str) -> list:
    pages = [p for p in store.queue(doc_id, "all", limit=10 ** 9)
             if p.get("layout") != IMAGE]
    pages.sort(key=lambda r: r["page_no"])
    if not pages:
        return []
    rows = [(p["page_no"], p["src_path"], pipeline._page_text(p)) for p in pages]
    res = bleed.scan(rows)
    feats = {p["page_no"]: bleed.features(p["src_path"]) for p in pages}

    blank = [(pn, v[1]) for pn, v in res.items() if v[0] is True]
    text = [(pn, v[1]) for pn, v in res.items() if v[0] is False]
    unsure = [(pn, v[1]) for pn, v in res.items() if v[0] is None]

    by_no = {p["page_no"]: p for p in pages}

    def ink(pn):                  # 墨的深浅，用来挑最危险的样本
        f = feats.get(pn)
        return f["c"] if f else 999.0

    # 该删的：先挑「原本有字」的（模型读出过东西，说明不是一眼空白），再挑没字的
    blank.sort(key=lambda x: (-(by_no[x[0]]["chars"] or 0), x[0]))
    # 该留的：「墨深正常」的才是危险样本 —— 它们离阈值最近，改判据最先翻车；
    # 「有大片深墨」的页被 d140 那道闸门直接放行，采进来只能验一道简单的闸。
    text.sort(key=lambda x: (0 if "墨深正常" in x[1] else 1, ink(x[0])))
    unsure.sort(key=lambda x: ink(x[0]))

    must = [pn for d, pn in MUST if d in doc_id and pn in by_no]
    picks = []
    for pn in must:
        picks.append((pn, res[pn][0], res[pn][1]))
    for pn, why in blank[:N_BLANK]:
        if pn not in must:
            picks.append((pn, True, why))
    for pn, why in text[:N_TEXT]:
        if pn not in must:
            picks.append((pn, False, why))
    for pn, why in unsure[:N_UNSURE]:
        if pn not in must:
            picks.append((pn, None, why))

    out = []
    for pn, v, why in picks:
        pg = by_no[pn]
        out.append({
            "doc": doc_id,
            "page_no": pn,
            "src": _rel(pg["src_path"]),
            "expect": {True: "blank", False: "text", None: "unsure"}[v],
            "chars": pg["chars"] or 0,
            "note": why,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="写进 tests/fixtures/")
    ap.add_argument("docs", nargs="*", help="只处理这些文档（默认全部）")
    a = ap.parse_args()

    store.init()
    docs = [d["id"] for d in store.list_docs()]
    if a.docs:
        docs = [d for d in docs if any(k in d for k in a.docs)]
    if not docs:
        print("没有匹配的文档")
        return 1

    cases = []
    for d in docs:
        got = sample_doc(d)
        c = {k: sum(1 for x in got if x["expect"] == k)
             for k in ("blank", "text", "unsure")}
        print(f"{d[:34]:<36} 取 {len(got):>2} 页  "
              f"空白 {c['blank']:>2} / 正文 {c['text']:>2} / 拿不准 {c['unsure']:>2}")
        cases += got

    if not cases:
        print("没取到样本")
        return 1
    if not a.write:
        print(f"\n共 {len(cases)} 页（加 --write 才落盘）")
        return 0

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({
        "meta": {
            "generated": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "source": "docplat 当前库状态（交付 txt 已人工核过）",
            "why": "改 bleed.py 判据时靠它确认「该删的还删、该留的还留」",
            "books_root": str(BOOKS_ROOT),
            "count": len(cases),
        },
        "cases": cases,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写 {FIXTURE} （{len(cases)} 页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
