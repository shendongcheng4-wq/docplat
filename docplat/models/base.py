"""引擎抽象 —— 模型藏在接口后面，换模型不改业务代码。"""
from dataclasses import dataclass, field
from pathlib import Path


class EngineError(RuntimeError):
    """引擎不可用（未启动 / 显存不足 / 超时）。"""


@dataclass
class Recognition:
    text: str
    engine: str
    sec: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def chars(self) -> int:
        # 只数纯文本：表格输出里每格都带 style=... 的标签，按原串数会虚高百倍
        from ..legacy import text_len
        return text_len(self.text or "")

    @property
    def ok(self) -> bool:
        return bool((self.text or "").strip())


class Engine:
    name = "base"
    gpu_gb = 0.0            # 需要多少显存，0 表示 CPU 或外部服务

    def available(self) -> bool:
        return True

    def recognize(self, img: Path, hint: str = "") -> Recognition:
        """hint: 'text' / 'table' / 'image'，引擎可据此换提示词。"""
        raise NotImplementedError

    def close(self):
        pass
