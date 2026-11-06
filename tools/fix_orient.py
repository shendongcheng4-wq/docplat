"""把带 EXIF 方向标记的页，按「正过来」重读一遍，只收下更好的结果。

背景：扫描件的 Orientation=3（存成倒置），看图软件会替你转，但 llama.cpp /
OpenCV 用裸像素 —— 整页倒着进模型。GLM 对整页正文基本能倒着读（实测字数常一样），
但**图版页的小字会成片丢**：铁的新四军 p0119 是 4×4 将军像，倒置时只认出 16 个
名字里的 4 个（0.5 秒就收工），正过来 16 个全出。

所以这里不是「一律重跑」——那样会把本来读对的页也换掉（实测 p0093 正过来反而
把「要图」读成「图」）。只按一条硬标准替换：**正过来后汉字数明显更多**
（≥ +3 字），且新结果没退化。其余原样留着，人想改再走工作台重跑。

用法：
    python tools/fix_orient.py            只体检，打印会改哪些页
    python tools/fix_orient.py --apply    真的写盘
    python tools/fix_orient.py --apply --min-chars 60   只看短页（默认全量）
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docplat import config as C          # noqa: E402
from docplat import imgio, pipeline, store   # noqa: E402
from docplat.legacy import redundancy_of      # noqa: E402
from docplat.models.glm import GlmEngine      # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")


def cjk(t: str) -> int:
    return len(CJK.findall(t or ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写盘（默认只体检）")
    ap.add_argument("--doc", default="", help="只处理某本书")
    ap.add_argument("--min-chars", type=int, default=-1,
                    help="只处理现有字数少于该值的页")
    a = ap.parse_args()

    store.init()
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT doc_id, hash, page_no, src_path, chars, status, channel, "
            "layout, out_path FROM pages ORDER BY doc_id, page_no")]
    rows = [r for r in rows if imgio.orientation(r["src_path"]) != 1]
    if a.doc:
        rows = [r for r in rows if r["doc_id"] == a.doc]
    if a.min_chars >= 0:
        rows = [r for r in rows if (r["chars"] or 0) < a.min_chars]
    if not rows:
        print("没有带方向标记的页")
        return 0

    print(f"带 EXIF 方向的页：{len(rows)}（{'写盘' if a.apply else '只体检'}）\n")
    eng = GlmEngine()
    changed = kept = skipped = 0

    for r in rows:
        if r["channel"] == "vl":
            print(f"  跳过 {r['doc_id']} p{r['page_no']}：vl 通道，要占显存，另行处理")
            skipped += 1
            continue
        old = ""
        if r["out_path"] and os.path.exists(r["out_path"]):
            old = Path(r["out_path"]).read_text(encoding="utf-8", errors="replace")
        try:
            rec = eng.recognize(Path(r["src_path"]))
        except Exception as e:                       # 服务没起/单页失败不中断
            print(f"  ! {r['doc_id']} p{r['page_no']} 识别失败：{e}")
            skipped += 1
            continue

        o, n = cjk(old), cjk(rec.text)
        red = redundancy_of(rec.text)
        better = n >= o + 3 and red < 0.6
        tag = "换" if better else "留"
        if better:
            changed += 1
        else:
            kept += 1
        print(f"  [{tag}] {r['doc_id']} p{r['page_no']} "
              f"{r['status']}/{r['channel']} {o} → {n} 字 冗余{red:.2f}")
        if better or o == 0:
            print(f"       旧: {old[:60].replace(chr(10), ' / ')}")
            print(f"       新: {rec.text[:60].replace(chr(10), ' / ')}")

        if better and a.apply:
            v = pipeline.evaluate(rec, r["layout"] or "text")
            op = pipeline.out_path(r["hash"])
            op.parent.mkdir(parents=True, exist_ok=True)
            op.write_text((rec.text or "").rstrip() + "\n", encoding="utf-8")
            store.set_page(r["doc_id"], r["hash"], status=v.status,
                           engine=rec.engine, chars=rec.chars,
                           sec=round(rec.sec, 2), score=v.score,
                           cover=v.score if v.status == pipeline.DONE else None,
                           out_path=str(op),
                           err="按 EXIF 方向校正后重读：" + v.reason)

    print(f"\n可换 {changed} 页，保留 {kept} 页，跳过 {skipped} 页"
          + ("" if a.apply else "（未写盘，加 --apply 生效）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
