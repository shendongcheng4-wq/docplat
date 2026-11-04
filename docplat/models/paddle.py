"""PaddleOCR-VL（0.9B）—— 表格/世系表的兜底通道。

实测：35.2 秒/页（中位仅 6 秒，均值被 7.9% 的长尾页拖高），峰值显存 15.4 GB。
族谱里 39 页世系表只有它能还原成 HTML <table>，所以不能因为慢就砍掉。

⚠ 显存冲突：GLM 服务占 4.5 GB，Paddle 峰值要 15.5 GB，16 GB 卡上装不下两个。
   流程必须分阶段（先跑 glm 通道，再停 GLM 服务跑 vl 通道），
   ensure_room() 会在启动前检查并给出明确指示，而不是跑到一半 OOM。
"""
import re
import subprocess
import time
from pathlib import Path

from .. import config as C
from ..legacy import extract_text, is_path_echo
from .base import Engine, EngineError, Recognition


def gpu_free_gb() -> float:
    """当前可用显存（GB）。拿不到就返回 0，让调用方按"不足"处理。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().splitlines()[0]) / 1024
    except Exception:
        return 0.0


class PaddleEngine(Engine):
    name = "vl"
    gpu_gb = C.ENGINE_GPU_GB["paddle"]

    def __init__(self):
        self._eng = None

    def ensure_room(self, need_gb: float = None):
        need = need_gb if need_gb is not None else self.gpu_gb
        free = gpu_free_gb()
        if free and free < need:
            raise EngineError(
                f"显存不足：Paddle 需要约 {need:.1f} GB，当前可用 {free:.1f} GB。\n"
                f"    先停掉 GLM 服务（它占 {C.ENGINE_GPU_GB['glm']:.1f} GB）：\n"
                f"    taskkill /IM llama-server.exe /F\n"
                f"    然后重跑本批 vl 通道。")

    def available(self) -> bool:
        try:
            import paddleocr  # noqa: F401
            return True
        except ImportError:
            return False

    def _build(self):
        if self._eng is not None:
            return self._eng
        self.ensure_room()
        try:
            from paddleocr import PaddleOCRVL
        except ImportError as e:
            raise EngineError("当前 PaddleOCR 版本没有 PaddleOCRVL") from e
        self._eng = PaddleOCRVL()
        return self._eng

    def recognize(self, img: Path, hint: str = "") -> Recognition:
        eng = self._build()
        src = self._shrink(img)
        t0 = time.time()
        raw = eng.predict(str(src))
        sec = time.time() - t0

        if raw is None:
            return Recognition(text="", engine=self.name, sec=sec)
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        texts = []
        for res in raw:
            t = extract_text(res)
            if t:
                texts.append(t)
        text = "\n\n".join(texts).strip()

        # 模型把输入路径当结果吐回来 —— 空白页/透印页/整幅大字的确定性行为，
        # 重跑结果一样。这里置空，由上层判为 needs_review，路径绝不进正文。
        echo = is_path_echo(text, Path(src))
        if echo:
            text = ""
        return Recognition(text=text, engine=self.name, sec=sec,
                           meta={"path_echo": echo, "has_table": "<table" in text.lower(),
                                 "has_img": "<img" in text.lower()})

    def _shrink(self, img: Path) -> Path:
        """长边超过 C.VL_MAX_SIDE 就先缩一份临时副本。

        为什么不原地缩：src_path 是用户的原图，只读不改；而且缩小后的副本
        可以按文件名缓存，重跑不用再缩一次。缩不动（读不出来/本来就不大）
        就原样返回，让 predict 自己报错。
        """
        import numpy as np
        import cv2

        try:
            from PIL import Image
            with Image.open(img) as im:
                w, h = im.size
        except Exception:
            return img
        if max(w, h) <= C.VL_MAX_SIDE:
            return img

        dst = C.DATA / "_vl_in" / f"{img.stem}_{C.VL_MAX_SIDE}{img.suffix}"
        if dst.exists() and dst.stat().st_mtime >= img.stat().st_mtime:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        s = C.VL_MAX_SIDE / max(w, h)
        arr = cv2.imdecode(np.fromfile(str(img), dtype=np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return img
        small = cv2.resize(arr, (max(1, int(w * s)), max(1, int(h * s))),
                           interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(dst.suffix or ".jpg", small,
                               [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return img
        buf.tofile(str(dst))
        return dst

    def close(self):
        if self._eng is not None:
            try:
                del self._eng
            finally:
                self._eng = None
            try:
                import paddle
                paddle.device.cuda.empty_cache()
            except Exception:
                pass
