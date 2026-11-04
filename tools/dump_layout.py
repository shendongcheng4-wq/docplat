"""把二值化/抽线/格网的中间结果导成图，肉眼定位问题。

  python tools/dump_layout.py p0153 p0146 p0050 p0001
输出 _tmp/layout/<page>.png —— 四联：灰度 / 二值墨迹 / 横线 / 竖线
"""
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
IMG = IMAGES
OUT = ROOT / "_tmp" / "layout"
WORK_W = 800
MARGIN = 0.045


def load(path: Path):
    raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    h, w = raw.shape
    g = cv2.resize(raw, (WORK_W, max(1, int(h * WORK_W / w))), interpolation=cv2.INTER_AREA)
    hh, ww = g.shape
    mx, my = int(ww * MARGIN), int(hh * MARGIN)
    return g[my:hh - my, mx:ww - mx]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ids = sys.argv[1:] or ["p0153", "p0146", "p0050", "p0001"]
    for pid in ids:
        g = load(IMG / f"{pid}.jpg")
        h, w = g.shape
        blur = cv2.GaussianBlur(g, (3, 3), 0)
        bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY_INV, 25, 12)
        ink = (bw > 0).astype(np.uint8) * 255

        hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, w // 25), 1))
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h // 25)))
        hor = cv2.morphologyEx(ink, cv2.MORPH_OPEN, hk)
        ver = cv2.morphologyEx(ink, cv2.MORPH_OPEN, vk)

        tiles = []
        for m in (g, ink, hor, ver):
            tiles.append(cv2.cvtColor(m, cv2.COLOR_GRAY2BGR))
        pad = np.full((h, 6, 3), 128, np.uint8)
        row = tiles[0]
        for t in tiles[1:]:
            row = np.hstack([row, pad, t])
        cv2.imwrite(str(OUT / f"{pid}.png"), row)
        print(f"{pid}: {w}x{h} ink={ink.mean()/255:.3f} "
              f"hor_px={int((hor>0).sum())} ver_px={int((ver>0).sum())}")


if __name__ == "__main__":
    main()
