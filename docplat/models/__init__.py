"""引擎注册表 —— 换模型只改这里。"""
from .base import Engine, EngineError, Recognition

_REGISTRY = {}


def register(cls):
    _REGISTRY[cls.name] = cls
    return cls


def get(name: str) -> Engine:
    from .glm import GlmEngine
    from .paddle import PaddleEngine
    _REGISTRY.setdefault("glm", GlmEngine)
    _REGISTRY.setdefault("vl", PaddleEngine)
    if name in _REGISTRY:
        return _REGISTRY[name]()
    # 通道名到引擎名的映射
    alias = {"text": "glm", "table": "vl"}
    if name in alias:
        return _REGISTRY[alias[name]]()
    raise EngineError(f"未知引擎 {name}，可选：{', '.join(_REGISTRY)}")


__all__ = ["Engine", "EngineError", "Recognition", "get", "register"]
