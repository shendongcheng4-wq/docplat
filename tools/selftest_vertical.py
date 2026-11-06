# -*- coding: utf-8 -*-
"""竖读（legacy.render_text(vertical=True)）的自检。

族谱是竖排、从右往左读，VL 却按 HTML 自然顺序吐表格，直接摊平会得到
「次子 平長子 次子 次子 子 裔長子」这种横着拼的行。渲染层加了三条支路，
这个脚本给每条钉一条断言 —— 2026-11-04 用户先后指出 p0129（图页被串成
长条）和 p0133/p0144（列序反了），所以三条都不能回退：

  ① 每行一格的吊线图        → 逐行输出，绝不转置
  ② 世代号行 + 内容行       → 按段、按列正序，一人一条，且不折行
  ③ 普通多格竖排表          → 转置，从最右列开始
  ④ 整页塞进一个格          → 先拆段还原矩阵，再按列正序

跑法：python tools/selftest_vertical.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from docplat.legacy import render_text

CASES = []


def case(name, html, want):
    CASES.append((name, html, want))


def table(*rows):
    trs = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table>{trs}</table>"


# ① 每行一格（p0129 上古至陳國世系吊线图）：colspan 合并，本来就是一行一段。
#    以前一律转置，6 段被串成一条 40 字长行。
case("每行一格的吊线图逐行输出",
     "<table>"
     "<tr><td colspan=6>陳氏世系圖表</td></tr>"
     "<tr><td colspan=6>上古至陳國世系</td></tr>"
     "<tr><td colspan=6>〇句望——〇橋牛——〇瞽叟</td></tr>"
     "<tr><td colspan=6>〇舜帝——</td></tr>"
     "<tr><td colspan=6>〇胡公滿</td></tr>"
     "</table>",
     "陳氏世系圖表\n上古至陳國世系\n〇句望——〇橋牛——〇瞽叟\n〇舜帝——\n〇胡公滿")

# ② 世代号行 + 内容行（p0144）：页面同一高度上的几个竖列各成一格，
#    一列 = 一个人的完整小传，行首必须是世代号，且一人只占一行（不折行）。
case("世代号行按列正序、一人一条",
     table(["二六世", "二七世", "二八世"],
           ["伯承", "從善", "德紹"],
           ["字武烈生卒", "字積慶喜施", "字福基生當"],
           ["缺配姜氏生", "與拯人危急", "秦季隱匿山"]),
     "二六世　伯承　字武烈生卒　缺配姜氏生\n"
     "二七世　從善　字積慶喜施　與拯人危急\n"
     "二八世　德紹　字福基生當　秦季隱匿山")

# ③ 普通多格竖排表：没有世代号，按最右列先读（p0133 那种人物线）。
case("多格竖排表从最右列读",
     table(["甲", "乙", "丙"], ["丁", "戊", "己"], ["庚", "辛", "壬"]),
     "丙　己　壬\n乙　戊　辛\n甲　丁　庚")

# ④ 整页塞进一个格（p0135）：单元格里是「视觉行 × 空格分段」，
#    先还原成矩阵，第 0 段已是最右列，所以按列正序读。
case("整页单格先拆段再按列读",
     "<table><tr><td>題記一行<br>"
     "第一世 甲 乙<br>第二世 丙 丁<br>第三世 戊 己</td></tr></table>",
     "題記一行\n第一世　第二世　第三世\n甲　丙　戊\n乙　丁　己")


def main() -> int:
    bad = 0
    for name, html, want in CASES:
        got = render_text(html, "flat", vertical=True).strip()
        ok = got == want.strip()
        print(("✓ " if ok else "✗ ") + name)
        if not ok:
            bad += 1
            print("   期望：", repr(want))
            print("   实际：", repr(got))
    if bad:
        print(f"\n失败 {bad} / {len(CASES)}")
        return 1
    print(f"\n全部通过（{len(CASES)}/{len(CASES)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
