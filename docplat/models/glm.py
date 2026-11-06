"""GLM-OCR via llama.cpp（OpenAI 兼容接口）。

实测（242 页族谱）：1.95 秒/页，显存 2.66 GB，正文页内容覆盖 90.3%。
短板：表格/世系表结构还原失败（39 页里 38 页不合格），故表格页必须走 Paddle。
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config as C
from .. import imgio
from ..legacy import clean_output
from .base import Engine, EngineError, Recognition


class GlmEngine(Engine):
    name = "glm"
    gpu_gb = C.ENGINE_GPU_GB["glm"]

    def __init__(self, base: str = None, model: str = None):
        self.base = (base or C.GLM_BASE).rstrip("/")
        self._model = model or C.GLM_MODEL

    def _resolve_model(self) -> str:
        if self._model:
            return self._model
        try:
            with urllib.request.urlopen(f"{self.base}/v1/models", timeout=10) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            raise EngineError(f"GLM 服务不可达（{self.base}）：{e}") from e
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if not ids:
            raise EngineError("GLM 服务没有返回可用模型")
        self._model = ids[0]
        return self._model

    def available(self) -> bool:
        try:
            self._resolve_model()
            return True
        except EngineError:
            return False

    def recognize(self, img: Path, hint: str = "", prompt: str = "") -> Recognition:
        model = self._resolve_model()
        # 按 EXIF 方向先正过来：倒置页会把图注/表格里的小字成片读丢（p0119）
        src = imgio.upright(img)
        b64 = base64.b64encode(Path(src).read_bytes()).decode()
        mime = "image/png" if str(src).lower().endswith(".png") else "image/jpeg"
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": [
                # 表格提示词目前与正文相同 —— GLM 的 table detection 官方标注为
                # Limited，换提示词也救不回来，最终仍要回落 Paddle。
                # （2026-11-05 更正：默认那版确实救不回来，但一句中文表格指令能 ——
                #   复杂表格换成 config.GLM_TABLE_PROMPT 后会吐完整 HTML 表，
                #   只在输出退化成空单元格时由 pipeline 触发。）
                {"type": "text", "text": prompt or C.GLM_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
            "temperature": 0,
            "max_tokens": C.GLM_MAX_TOKENS,
            "stream": False,
        }).encode()

        t0 = time.time()
        req = urllib.request.Request(
            f"{self.base}/v1/chat/completions", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=C.PAGE_TIMEOUT) as r:
                data = json.loads(r.read().decode())
        except urllib.error.URLError as e:
            raise EngineError(f"GLM 调用失败：{e}") from e
        except TimeoutError as e:
            raise EngineError(f"GLM 超时（>{C.PAGE_TIMEOUT}s）") from e

        sec = time.time() - t0
        ch = (data.get("choices") or [{}])[0]
        raw = ((ch.get("message") or {}).get("content") or "").strip()
        return Recognition(
            text=clean_output(raw), engine=self.name, sec=sec,
            meta={"model": model, "finish": ch.get("finish_reason"),
                  "raw_chars": len(raw)},
        )
