"""EXIF 方向归一的子自检。

不需要真书：合成小图，分别写 Orientation=3（倒置）和 6（侧躺）的 EXIF，验证
  1) orientation 读得出来
  2) upright 真的把像素转正（180° 前后左右都翻；90° 宽高互换）
  3) 不需要转的图**原路径返回**，且不产生缓存文件（零开销）
  4) 缓存复用：第二次调用同一个路径、不重写
  5) 文件不存在时不炸（返回原路径，让上层自己去报错）

跑法：python tools/selftest_imgio.py
"""
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docplat import imgio  # noqa: E402

TMP = Path(__file__).resolve().parents[1] / "_tmp" / "_imgio_test"
ok = fail = 0


def check(name: str, cond: bool, extra: str = ""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name}  {extra}")


def _make(path: Path, size=(60, 30), orientation=None) -> np.ndarray:
    """左上角白、其余黑，四个角再各点一个小色块，转没转一眼看得出。"""
    w, h = size
    a = np.zeros((h, w, 3), np.uint8)
    a[:4, :4] = (255, 255, 255)          # 左上白角
    im = Image.fromarray(a)
    if orientation:
        exif = im.getexif()
        exif[274] = orientation
        im.save(path, quality=95, exif=exif)
    else:
        im.save(path, quality=95)
    return a


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    print("1) 没有方向标记的图：原路径返回、不建缓存")
    p1 = TMP / "plain.jpg"
    _make(p1)
    check("orientation == 1", imgio.orientation(p1) == 1)
    r1 = imgio.upright(p1)
    check("返回原路径", Path(r1) == p1, f"got {r1}")
    check("没生成缓存", not list(TMP.glob("*_o*.jpg")), str(list(TMP.glob("*_o*"))))

    print("2) Orientation=3（倒置）：转 180 度")
    p3 = TMP / "flip.jpg"
    _make(p3, orientation=3)
    check("orientation == 3", imgio.orientation(p3) == 3)
    r3 = Path(imgio.upright(p3, out_dir=TMP / "cache", name="flip"))
    check("生成了副本", r3.exists() and r3 != p3, str(r3))
    src = np.asarray(Image.open(p3).convert("RGB"))
    got = np.asarray(Image.open(r3).convert("RGB"))
    check("尺寸不变", got.shape == src.shape, f"{got.shape} vs {src.shape}")
    # 左上白角应跑到右下（各角以色块的「最亮 4x4 位置」比，容忍 JPEG 误差）
    check("白角翻到右下", got[-6:, -6:].mean() > 100 and got[:6, :6].mean() < 20,
          f"右下{got[-6:, -6:].mean():.0f} 左上{got[:6, :6].mean():.0f}")
    check("副本不再带方向标记", imgio.orientation(r3) == 1)

    print("3) Orientation=6（侧躺）：宽高互换，dims 也跟着换")
    p6 = TMP / "rot.jpg"
    _make(p6, size=(60, 30), orientation=6)
    check("orientation == 6", imgio.orientation(p6) == 6)
    check("dims 交换宽高", imgio.dims(p6) == (30, 60), str(imgio.dims(p6)))
    r6 = Path(imgio.upright(p6, out_dir=TMP / "cache", name="rot"))
    w, h = Image.open(r6).size
    check("副本宽高互换", (w, h) == (30, 60), f"{w}x{h}")

    print("4) 缓存复用：第二次不重写")
    m1 = r3.stat().st_mtime_ns
    again = Path(imgio.upright(p3, out_dir=TMP / "cache", name="flip"))
    check("同一路径", again == r3, f"{again} vs {r3}")
    check("mtime 未变", again.stat().st_mtime_ns == m1)

    print("5) 文件不存在：返回原路径不抛")
    ghost = TMP / "nope.jpg"
    check("原路径返回", Path(imgio.upright(ghost)) == ghost)
    check("orientation 兜底为 1", imgio.orientation(ghost) == 1)

    print(f"\n{ok}/{ok + fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
