"""单页试机：确认 PaddleOCR-VL 在 v2 里能起、能吃下显存、能还原表格。

用法：python tools/smoke_vl.py p0153 [p0151 ...]
输出直接打到 stderr/stdout，由调用方重定向到日志。
"""
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMG = IMAGES


def _used() -> int:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return 0


def main(ids):
    peak = {"v": 0}
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            peak["v"] = max(peak["v"], _used())
            stop.wait(1.0)

    th = threading.Thread(target=watch, daemon=True)
    th.start()

    from docplat.models.paddle import gpu_free_gb
    from docplat.models.paddle import PaddleEngine

    print(f"空闲显存 {gpu_free_gb():.2f} GB  基线已用 {_used()} MiB", flush=True)
    eng = PaddleEngine()
    eng.ensure_room()
    print("ensure_room 通过", flush=True)

    for pid in ids or ["p0153"]:
        t = time.time()
        rec = eng.recognize(IMG / f"{pid}.jpg")
        print(f"\n===== {pid}  {rec.sec:.1f}s  字数 {len(rec.text)}  "
              f"has_table={rec.meta.get('has_table')} echo={rec.meta.get('path_echo')} "
              f"墙钟 {time.time() - t:.1f}s", flush=True)
        print(rec.text[:600], flush=True)

    stop.set()
    time.sleep(1.5)
    print(f"\n峰值显存 used = {peak['v']} MiB / 16311", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
