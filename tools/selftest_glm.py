"""GLM 启动锁的自检（2026-11-06 加）。

起因：09:02 工作台和一个页面加载各自调了一次 glm_start，两个 llama-server
相隔 1 秒起来，各吃 4.5GB 显存，后面 Paddle 一加载就 OOM。glm_start 原本是
「先查 glm_up() 再 Popen」，两步之间没有互斥。

这里锁住三件事：
  1. 多个线程同时调 glm_start，只能起一个进程（其余人看到服务已就绪就收工）
  2. 锁用完必须删掉，不留残锁把下次启动卡死
  3. 持有者被杀的残锁（mtime 很旧）能被后来者接管

不真起模型：Popen 换成计数器，glm_up 换成「起过就返回 True」。
跑法：python tools/selftest_glm.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docplat import auto  # noqa: E402

TMP = Path(__file__).resolve().parents[1] / "_tmp" / "_glm_test"
LOCK = TMP / "spawn.lock"

ok = 0
bad = 0


def check(name, got, want):
    global ok, bad
    if got == want:
        print(f"  ✓ {name}：{got}")
        ok += 1
    else:
        print(f"  ✗ {name}：得到 {got!r}，期望 {want!r}")
        bad += 1


class Fake:
    """替身：记录 Popen 次数，替身一个「起完要加载几秒才健康」的模型服务。

    WAIT 必须是正数：真实 llama-server 从被拉起走到 /health 返回 ok 要约 6 秒，
    这段时间里 glm_up() 一直是 False —— 这才是别人闯进来重复起实例的窗口。
    替身如果「起完立刻健康」，线程之间根本撞不上，自检就成了摆设
    （第一次写的时候正是这么写的，去掉锁也照样全绿）。
    """

    WAIT = 1.2

    def __init__(self):
        self.spawns = 0
        self.first = 0.0
        self.lock = threading.Lock()

    def popen(self, *a, **kw):
        with self.lock:
            self.spawns += 1
            if not self.first:
                self.first = time.time()
        return object()

    def up(self, timeout=2):
        return self.spawns > 0 and time.time() - self.first > self.WAIT


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        LOCK.unlink()

    f = Fake()
    # 只替换真正会碰进程/网络的那几个函数，锁逻辑本身保持原样
    orig = (auto.subprocess.Popen, auto.glm_up, auto.glm_prune, auto.GLM_LOCK)
    auto.subprocess.Popen = f.popen
    auto.glm_up = f.up
    auto.glm_prune = lambda *a, **k: []
    auto.GLM_LOCK = LOCK
    try:
        print("1) 12 个线程同时要 GLM，只能起一个进程")
        errs = []

        def call():
            try:
                auto.glm_start(wait=5, quiet=True)
            except Exception as e:            # 不许抛，抛了就是锁没兜住
                errs.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=call) for _ in range(12)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check("起的进程数", f.spawns, 1)
        check("异常数", len(errs), 0)
        if errs:
            print("     首个异常：", errs[0])
        check("锁文件已删除", LOCK.exists(), False)

        print("2) 服务已在线时再调，不该再起")
        auto.glm_start(wait=5, quiet=True)
        check("起的进程数", f.spawns, 1)

        print("3) 残锁能被接管（持有者被杀，锁文件留了下来）")
        LOCK.write_text("999999 0", encoding="utf-8")
        old = time.time() - 9999
        import os
        os.utime(LOCK, (old, old))            # 伪装成很久以前建的
        f.spawns = 0
        with auto._spawn_lock(wait=5) as mine:
            check("拿到锁", mine, True)
        check("锁文件已删除", LOCK.exists(), False)

        print(f"\n全部通过（{ok}/{ok + bad}）" if not bad
              else f"\n{ok} 过 / {bad} 未过")
    finally:
        auto.subprocess.Popen, auto.glm_up, auto.glm_prune, auto.GLM_LOCK = orig
        if LOCK.exists():
            LOCK.unlink()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
