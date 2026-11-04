"""按当前代码重算已有页面的字数与质量判定，不调模型。

用途：改了字数统计口径 / 阈值之后，把库里存量页面的 chars、score、status
重新推一遍，而不是重跑一遍模型（39 页 VL 要 12 分钟）。

默认只打印差异（dry-run），加 --apply 才写库。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docplat import store                      # noqa: E402
from docplat.legacy import text_len           # noqa: E402
from docplat.models.base import Recognition   # noqa: E402
from docplat.router.quality import evaluate   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", default="zu")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    store.init()
    rows = store.queue(a.doc, "all", 10 ** 9)
    diff, keep = [], 0
    for r in rows:
        op = r.get("out_path")
        if not op or not Path(op).exists():
            continue
        raw = Path(op).read_text(encoding="utf-8")
        old = r["chars"] if r["chars"] is not None else -1
        new = text_len(raw)
        rec = Recognition(text=raw, engine=r["engine"] or "")
        v = evaluate(rec, r["layout"] or "text")
        # 人工通道与已核过的页不重判，避免冲掉人工结论
        if (r["channel"] == "human") or r["status"] == "running":
            keep += 1
            continue
        # retext 替换过的页是**故意**留在 review 的（内容是 GLM 纯文本，可能缺段，
        # 等人工对照原图确认）。这里若按现在的闸门重判，会因为「有字」被推回 done，
        # 把待人工确认的活悄悄吞掉。
        if (r.get("err") or "").startswith("已改用 GLM 纯文本清单"):
            keep += 1
            continue
        if old != new or v.status != r["status"]:
            diff.append((Path(r["src_path"]).stem, old, new,
                         r["status"], v.status, v.reason))

    print(f"共 {len(rows)} 页，需更新 {len(diff)} 页，跳过 {keep} 页\n")
    print(f"{'页':<9}{'旧字':>8}{'新字':>8}  {'旧状态':<7}{'新状态':<7} 理由")
    for pid, old, new, st0, st1, why in diff:
        mark = "  " if st0 == st1 else "→ "
        print(f"{pid:<9}{old:>8}{new:>8}  {st0:<7}{mark}{st1:<7} {why}")

    if a.apply and diff:
        for pid, old, new, st0, st1, why in diff:
            r = next(x for x in rows if Path(x["src_path"]).stem == pid)
            store.set_page(a.doc, r["hash"], chars=new, status=st1,
                           score=1.0 if st1 == "done" else 0.3, err=why)
        print(f"\n已写库 {len(diff)} 页")
    elif diff:
        print("\n（dry-run，未写库；加 --apply 生效）")


if __name__ == "__main__":
    main()
