"""图像方向归一：按 EXIF Orientation 把图正过来，再交给模型。

为什么需要：扫描件/翻拍件的 JPEG 头里常带 Orientation 标记（3 = 已存成倒置、
6/8 = 存成侧躺），看图软件和浏览器会替你转回来，但 llama.cpp（stb_image）和
OpenCV 用的是裸像素 —— 标记被无声忽略，整页倒着进模型。

实测（铁的新四军 p0119「将军像」页，Orientation=3）：
    原样喂（倒置）  0.52 秒，16 个名字只出 4 个
    正过来再喂      1.93 秒，16 个名字全出
倒置对整页正文影响不大（模型能读，实测目录页照样读对），但图注、小字、表格格
里的名字会成片丢 —— 这种「只丢一部分」比全错更难发现。全库 7646 页里 263 页
带方向标记。

原则同 `paddle._shrink`：不动用户原图。需要转的页在 data/_upright/ 下缓存一份
（同一页重跑直接复用），不需要转的直接返回原路径，零拷贝零开销。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import config as C

# 这几种 Orientation 是「宽高被交换过的」：6/7/8 旋转 90°，5 是转置
SWAPPED = (5, 6, 7, 8)


def orientation(src) -> int:
    """EXIF 方向值，没有就是 1（正常）。读不出来也当 1，别让坏文件卡住流程。"""
    try:
        from PIL import Image
        with Image.open(src) as im:
            return int(im.getexif().get(274) or 1)
    except Exception:
        return 1


def needs_fix(src) -> bool:
    return orientation(src) != 1


def upright(src, out_dir=None, name: str = "") -> Path:
    """返回「像素已经是正方向」的图片路径。

    已经是正方向就原样返回 src；否则生成/复用 data/_upright 下的副本。
    name 用来给缓存起名（有页 hash 就传 hash）—— 别用文件名，不同书都有
    Image00009.jpg，会互相覆盖。
    """
    src = Path(src)
    o = orientation(src)
    if o == 1 or not src.exists():
        return src
    out_dir = Path(out_dir) if out_dir else (C.DATA / "_upright")
    key = name or hashlib.sha1(
        str(src.resolve()).encode("utf-8", "ignore")).hexdigest()[:16]
    dst = out_dir / f"{key}_o{o}.jpg"
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst

    from PIL import Image, ImageOps
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        fixed = ImageOps.exif_transpose(im)
        if fixed.mode not in ("RGB", "L"):
            fixed = fixed.convert("RGB")
        # 不写 exif：转完的像素就是最终的，留着旧标记反而会被别的工具再转一次
        fixed.save(dst, quality=95)
    return dst


def dims(src):
    """显示方向下的 (宽, 高)。入库记尺寸用，不能让倒置页把宽高记反。"""
    try:
        from PIL import Image
        with Image.open(src) as im:
            w, h = im.size
            o = int(im.getexif().get(274) or 1)
    except Exception:
        return (None, None)
    return (h, w) if o in SWAPPED else (w, h)
