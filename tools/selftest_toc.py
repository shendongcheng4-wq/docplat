"""目录点线清理的自检。

不依赖真实书籍：直接合成一张「文字块 + 点线 + 页码」的假目录页，验证
  1) 点线被抹干净
  2) 文字块一个像素都没动
  3) 没有点线的页面不会被误改（返回 None）

跑法：python tools/selftest_toc.py
"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docplat import toc  # noqa: E402

TMP = Path(__file__).resolve().parents[1] / "_tmp" / "_toc_test"
H, W = 1400, 1200


def _page(dots: bool) -> np.ndarray:
    im = np.full((H, W, 3), 255, np.uint8)
    for i, y in enumerate(range(120, H - 120, 120)):
        # 「文字块」：16px 高的一排方块，模拟一行汉字（cv2 画不了中文）
        x = 100
        for _ in range(9):
            cv2.rectangle(im, (x, y - 8), (x + 13, y + 8), (0, 0, 0), -1)
            x += 20
        if dots:
            dx = x + 20
            while dx < W - 150:
                cv2.circle(im, (dx, y), 2, (0, 0, 0), -1)
                dx += 13
        # 页码：右端一个数字块
        cv2.rectangle(im, (W - 120, y - 10), (W - 100, y + 10), (0, 0, 0), -1)
    return im


def _small_dots(im: np.ndarray) -> int:
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    n, _, st, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    return sum(1 for i in range(1, n) if st[i][3] <= 9 and 2 <= st[i][4] <= 160)


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    ok = True

    # ---- 1. 有点线：抹干净、不动字 ----
    src = TMP / "toc_dotted.png"
    cv2.imwrite(str(src), _page(dots=True))
    out = toc.strip_dot_leaders(src, TMP / "out", name="dotted")
    if not out or not out.exists():
        print("✗ 有点线的页面没被判出来")
        return 1
    before = cv2.imread(str(src))
    after = cv2.imread(str(out))
    n_before = _small_dots(before)
    n_after = _small_dots(after)
    print(f"  点状连通域 {n_before} → {n_after}")
    if n_after > n_before * 0.15:
        print(f"✗ 点线没抹干净（还剩 {n_after}）")
        ok = False

    # 文字块区域（x<300）必须一模一样
    # 文字块最右到 x≈273，点线从 298 才开始，取 285 切干净
    diff = np.abs(before[:, :285].astype(int) - after[:, :285].astype(int)).max()
    print(f"  左侧文字区最大像素差 {diff}")
    if diff != 0:
        print("✗ 文字被改了")
        ok = False

    # ---- 2. 没有点线：不许动 ----
    plain = TMP / "toc_plain.png"
    cv2.imwrite(str(plain), _page(dots=False))
    r = toc.strip_dot_leaders(plain, TMP / "out", name="plain")
    print(f"  无点线页面 → {r}")
    if r is not None:
        print("✗ 无点线的页面也被改了")
        ok = False

    print("全部通过（3/3）" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
