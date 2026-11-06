"""透印/空白判据的回归自检 —— 跑 tests/fixtures/bleed_cases.json 里的样本。

    python tools/selftest_bleed.py              # 全部文档
    python tools/selftest_bleed.py 商业史料      # 只跑名字匹配的文档
    python tools/selftest_bleed.py -v            # 逐页列出判定理由

样本是「已经被人工核过的现状」（见 tools/make_bleed_fixture.py），三类：
  blank  = 该判空白（透印/无正印墨）
  text   = 必须留（正文页、章扉页、浅墨宽表）
  unsure = 拿不准，只能进复核 —— 判成 blank 就是误删，判成 text 就是放行乱码

跑一次要看的就是：**有没有把 text 判成 blank（误删）**，其余算质量变化。
退出码 1 = 有误删或有页的判定翻转。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docplat import bleed, pipeline, store          # noqa: E402
from docplat.router import IMAGE                    # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bleed_cases.json"
LABEL = {True: "blank", False: "text", None: "unsure"}


def resolve(src: str, root: Path) -> Path:
    p = Path(src)
    return p if p.is_absolute() else root / p


def check_image_pages_excluded() -> tuple:
    """影像页不能进透印判定（2026-11-04 修过的坑：p241/p242 被当成空白，
    连导出里的「影像」占位都吞了）。这里不读图，直接把 bleed.scan 打桩。"""
    orig = bleed.scan
    bleed.scan = lambda rows, width=560: {pn: (True, "打桩：全判空白") for pn, _, _ in rows}
    try:
        pages = [
            {"page_no": 1, "layout": IMAGE, "src_path": "x", "out_path": None, "hash": "h1"},
            {"page_no": 2, "layout": "text", "src_path": "x", "out_path": None, "hash": "h2"},
        ]
        flags = pipeline.bleed_flags("__selftest__", pages)
    finally:
        bleed.scan = orig
    ok = (1 not in flags) and flags.get(2) is True
    return ok, flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("docs", nargs="*", help="只跑这些文档（按名字包含匹配）")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if not FIXTURE.exists():
        print(f"没有样本文件 {FIXTURE}\n先跑 python tools/make_bleed_fixture.py --write")
        return 2
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    root = Path(os.environ.get("DOCPLAT_BOOKS")
                or data.get("meta", {}).get("books_root")
                or (Path.home() / "Desktop" / "书籍"))

    store.init()
    by_doc = {}
    for c in cases:
        by_doc.setdefault(c["doc"], []).append(c)

    bad, skipped, total = 0, 0, 0
    for doc, cs in by_doc.items():
        if a.docs and not any(k in doc for k in a.docs):
            continue
        pages = [p for p in store.queue(doc, "all", limit=10 ** 9)
                 if p.get("layout") != IMAGE]
        pages.sort(key=lambda r: r["page_no"])
        if not pages:
            print(f"— {doc}：库里没有这一本，跳过 {len(cs)} 页")
            skipped += len(cs)
            continue
        rows = [(p["page_no"], p["src_path"], pipeline._page_text(p)) for p in pages]
        res = bleed.scan(rows)
        by_no = {p["page_no"]: p for p in pages}

        print(f"\n{doc}  （{len(cs)} 页样本）")
        for c in sorted(cs, key=lambda x: x["page_no"]):
            pn = c["page_no"]
            total += 1
            img = resolve(c["src"], root)
            if not img.exists():
                skipped += 1
                print(f"  ? p{pn:<4d} 图不在本机：{img}")
                continue
            if pn not in res:
                bad += 1
                print(f"  ✗ p{pn:<4d} 这一页没进判定（版面={by_no[pn]['layout']})")
                continue
            got = LABEL[res[pn][0]]
            why = res[pn][1]
            want = c["expect"]
            if got == want:
                if a.verbose:
                    print(f"  ✓ p{pn:<4d} {got:<6s} {why}  | {c['note'][:28]}")
            else:
                bad += 1
                mark = "误删" if want in ("text", "unsure") and got == "blank" else "翻转"
                print(f"  ✗ p{pn:<4d} {mark}：应 {want}，现判 {got}（{why}）"
                      f"  原 {c['chars']} 字 | {c['note'][:30]}")

    ok, flags = check_image_pages_excluded()
    print(f"\n影像页不参与透印判定：{'✓' if ok else '✗'}  {flags}")

    print(f"\n合计 {total} 页：{'全部一致' if not bad else f'{bad} 页判定变了'}"
          + (f"，{skipped} 页跳过（图不在本机）" if skipped else ""))
    return 1 if (bad or not ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
