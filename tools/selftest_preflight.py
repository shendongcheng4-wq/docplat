"""导出体检的分档口径自检。

背景（2026-11-05）：用户问「待人工已经是 0 了为什么还有提示」。
原因是 preflight 把「0 字」一律当风险，而族谱/史料里几十页真空白
（pipeline 已判「空白页（墨淡/透印）」）也被算进去，于是每次导出都弹窗。

跑一段回归，保证这四类不会被搅在一起：

  没结果(miss)       → 必须拦
  0 字且没判空白(zero) → 必须拦（这才是真风险）
  0 字但已判空白(blank) → 不拦，只计数
  已核对过的            → 两个都不要算
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docplat import config as C                      # noqa: E402
from docplat import preflight                        # noqa: E402


def page(no, chars, err="", exists=True, hash=None, tmp=None):
    p = Path(tmp) / f"p{no}.md"
    if exists:
        p.write_text("x" * chars, encoding="utf-8")
    return {"page_no": no, "chars": chars, "err": err, "hash": hash or f"h{no}",
            "out_path": str(p) if exists else str(Path(tmp) / "nope.md")}


def main() -> int:
    ok, n, bad = True, 0, 0

    def check(name, got, want):
        nonlocal ok, n, bad
        n += 1
        flag = "✓" if got == want else "✗"
        print(f"  {flag} {name}: {got}（期望 {want}）")
        if got != want:
            ok = False
            bad += 1

    with tempfile.TemporaryDirectory() as td:
        pages = [
            page(1, 700, tmp=td),                                        # 正常页
            page(2, 0, "空白页，无正文", tmp=td),                        # 判空白
            page(3, 0, "空白页（墨淡且无字）", tmp=td),                  # 判空白
            page(4, 0, "透印近空白页，模型幻觉输出已清空", tmp=td),       # 判空白（措辞变体）
            page(5, 0, "", tmp=td),                                      # 0 字无理由 → 拦
            page(6, 0, "", tmp=td),                                      # 0 字但已核对 → 不算
            page(7, 0, "空白页，无正文", tmp=td),                        # 判空白但已核对 → 不算
            page(8, 0, "空白页，无正文", exists=False, tmp=td),           # 没结果优先算 miss
        ]
        got = preflight.classify(pages, {"h6", "h7"})

        check("miss（没结果优先于空白判定）", got["miss"], [8])
        check("zero（0 字且没判空白）", got["zero"], [5])
        check("blank（判过空白、未核对）", got["blank"], [2, 3, 4])
        check("已核对的不出现在任何一档",
              [x for x in (6, 7) if x in got["zero"] + got["blank"] + got["miss"]], [])

        check("混合（含 miss/zero）→ 拦", preflight.blocking(got), True)
        only_blank = preflight.classify([pages[1], pages[2]], set())
        check("纯空白页 → 不拦", preflight.blocking(only_blank), False)
        only_miss = preflight.classify([pages[7]], set())
        check("纯 miss → 拦", preflight.blocking(only_miss), True)
        only_zero = preflight.classify([pages[4]], set())
        check("纯 zero → 拦", preflight.blocking(only_zero), True)

        old = C.PREFLIGHT_WARN_BLANK
        try:
            C.PREFLIGHT_WARN_BLANK = True
            check("严格模式（WARN_BLANK=True）→ 空白也拦",
                  preflight.blocking(only_blank), True)
        finally:
            C.PREFLIGHT_WARN_BLANK = old

    print(f"全部通过（{n}/{n}）" if ok else f"有失败项（{bad}/{n} 未过）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
