"""目录页的点线引导符（…………）预处理。

为什么必须有这一步
------------------
GLM 读到目录行末的点线就会掉进循环：p9（先进性教育活动）读出第一行
「44、关于深化农村共产党员好实事好事竞赛活动的通知」，后面接 **32744 个「…」**，
直到 6000 token 上限才停。整页 3.2 万字、压缩冗余 1.00，质量闸门判「疑似重复生成」，
人一看就是"目录页识别不了" —— 其实字看得清清楚楚，是点线把模型带进了死循环。

抹点线只抹**点自己的那一小块**
----------------------------
绝不能被连通域外框整行涂白：点线常和左边的文字连成一个连通域（尤其扫描件），
按外框涂会把「附 录」的「附」一起吃掉（2026-11 第一版就是这么错的）。
所以判据换成「先定出点线带，再只抹带内的小连通域」，见 find_dots 的注释。

页码「(135)」是正文，不抹。
"""
from pathlib import Path

import cv2
import numpy as np

from . import config as C
from . import imgio


def _imread(path: Path) -> np.ndarray | None:
    """中文路径不能直接 cv2.imread（会静默返回 None），走 fromfile + imdecode。"""
    try:
        buf = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


def find_dots(gray: np.ndarray) -> tuple[list, list]:
    """找出目录点线上的小点，返回 (要抹的点列表, 点线带列表)。

    每个点是 (x, y, w, h)，取自连通域自己的 bbox；每条带是 (x, y, w, h)。

    两段式（2026-11-06 改）：
      1) **定带**：把小候选点画成掩膜，做横向闭运算（核宽 TOC_MORPH_W）把同一行的
         点连成一条细长的线，宽度够、厚度够薄的连通块就是「点线带」。不用等距性
         判定 —— 扫描件一页是斜的，等距/同带高那套在小字号页上漏一半（水西颂
         p0014 的 24 行只认出 10 行，剩下的残点照样把 GLM 带进死循环）。
      2) **抹点**：只抹**落在带内**的小连通域。字是大连通域，压根不进候选表，
         所以带判歪一点也啃不到字（旧版「整条带候选一起抹」就是这么啃掉
         「陈毅」的「陈」的）。

    尺寸这一关仍要卡死：实测 1850px 宽的扫描件，点 w/h 4~6px、面积 ≈21，正文汉字
    15~30px。第一版阈值放到 32px/900 面积，把「（2003年06月01日 溧试点办[第35号]）」
    整片抹成了方块 —— 凡是「字」也会被当点，这一页就毁了。
    """
    h_img, w_img = gray.shape[:2]
    # Otsu 反相：墨迹为白（255），纸面为黑
    _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    n, _, stats, _ = cv2.connectedComponentsWithStats(binimg, connectivity=8)

    cand = []
    for i in range(1, n):
        x, y, w, h, area = (stats[i][0], stats[i][1], stats[i][2],
                            stats[i][3], stats[i][4])
        if not (2 <= area <= C.TOC_DOT_AREA):
            continue
        # 高度是硬闸门：正文汉字 15~30px，点线（含被 JPEG 糊成短横的）都在 10px 以内。
        if h > C.TOC_DOT_HMAX or w > C.TOC_DOT_WIDE:
            continue
        cand.append((int(x), int(y), int(w), int(h)))

    if not cand:
        return [], []

    # ---- 1) 定带：小点掩膜 → 横向闭运算 → 细长连通块 ----
    small = np.zeros_like(binimg)
    for x, y, w, h in cand:
        small[y:y + h, x:x + w] = 255
    kern = cv2.getStructuringElement(cv2.MORPH_RECT, (C.TOC_MORPH_W, 1))
    joined = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kern)
    n2, _, st2, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)

    span_min = max(C.TOC_SPAN_MIN, int(w_img * C.TOC_SPAN_RATIO))
    bands = []
    for i in range(1, n2):
        bx, by, bw, bh = (int(v) for v in st2[i][:4])
        # 够长 + 够薄：成行的小字连起来也有这么长，但厚度会超过 TOC_BAND_MAX_H
        if bw >= span_min and bh <= C.TOC_BAND_MAX_H:
            bands.append((bx, by, bw, bh))
    if not bands:
        return [], []

    # ---- 2) 抹点：只抹带内的候选，字不在候选里所以碰不到 ----
    dots = []
    for x, y, w, h in cand:
        cx, cy = x + w / 2, y + h / 2
        for bx, by, bw, bh in bands:
            # 容差 2px：点偶尔被切在带边界上
            if (bx - 2 <= cx <= bx + bw + 2) and (by - 2 <= cy <= by + bh + 2):
                dots.append((x, y, w, h))
                break
    return dots, bands


def strip_dot_leaders(img: Path, out_dir: Path, name: str = "") -> Path | None:
    """抹掉目录点线，返回清理后的图片路径；没找到点线返回 None（调用方别重跑）。

    name 给缓存文件起名（传页 hash 最好）—— 别用原文件名：不同书都有
    Image00009.jpg，用文件名会互相覆盖，排查时根本分不清是哪一本。
    """
    src = Path(img)
    if not src.exists():
        return None
    # 倒置页的点线在像素里是倒着的，方向正过来再找（同 GLM/Paddle 的入图口径）
    src = imgio.upright(src)
    gray = None
    im = _imread(src)
    if im is None:
        return None
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)

    dots, bands = find_dots(gray)
    if not dots:
        return None

    for x, y, w, h in dots:
        # 只填自己的框：不外扩、不整行涂白
        im[y:y + h, x:x + w] = 255

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 原图是 PNG 就出 PNG（无压缩损失，自检能按像素比）；扫描件都是 JPG，
    # 用 92 质量重存，肉眼与原图无差。
    if src.suffix.lower() == ".png":
        out = out_dir / ((name or src.stem) + ".png")
        ok, buf = cv2.imencode(".png", im)
    else:
        out = out_dir / ((name or src.stem) + ".jpg")
        ok, buf = cv2.imencode(".jpg", im, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        return None
    out.write_bytes(buf.tobytes())
    return out
