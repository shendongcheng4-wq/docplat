# -*- coding: utf-8 -*-
"""启动 GLM-OCR 识别服务（llama.cpp / llama-server + GGUF 视觉模型）。

docplat 自己不带模型推理，只当一个 HTTP 客户端：它把页面图片 POST 给这个服务，
拿回 markdown/HTML 文本。所以想跑识别，必须先让这个脚本把服务拉起来。

    python scripts/glm_serve.py            # 前台启动（Ctrl+C 停）
    python scripts/glm_serve.py --stop     # 按端口杀进程
    python scripts/glm_serve.py --check    # 只看状态

需要两样东西，都得自己准备（体积太大，不进仓库）：
  1. llama.cpp 的 llama-server 可执行文件（含 Vulkan/CUDA 后端）
     → https://github.com/ggml-org/llama.cpp/releases
  2. GLM-OCR 的 GGUF 量化权重：主模型 + mmproj 视觉投影

路径解析顺序（先命中者生效）：
  --models/--bin 参数  >  环境变量 GLM_MODEL_DIR / LLAMACPP_BIN  >
  scripts/local.conf（JSON，已 gitignore，放本机路径）  >  scripts/ 下的默认子目录

关于 --repeat-penalty 1.0：llama.cpp 默认 1.10，会把「刚刚出现过的 token」压低，
于是抄写「一八八八年」时第二个「八」被罚掉，输出成「一八八年」；同理
「一九九九年」→「一九九年」、「二〇〇〇年」→「二〇〇年」。抄书不是创作，
重复字必须照抄，所以这里必须显式关掉。
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent


def _local_conf() -> dict:
    """本机路径覆盖（local.conf 不进仓库，见 .gitignore）。"""
    p = HERE / "local.conf"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


_CONF = _local_conf()

MODEL_DIR = Path(os.environ.get("GLM_MODEL_DIR")
                 or _CONF.get("models")
                 or HERE / "models")
BIN_DIR = Path(os.environ.get("LLAMACPP_BIN")
               or _CONF.get("bin")
               or HERE / "runtime" / "llamacpp")

MAIN = MODEL_DIR / "GLM-OCR-Q8_0.gguf"
MMPROJ = MODEL_DIR / "mmproj-GLM-OCR-Q8_0.gguf"


def find_server() -> Path:
    """在 BIN_DIR 下递归找 llama-server(.exe)，找不到再看 PATH。"""
    names = ("llama-server.exe", "llama-server")
    cands = []
    for n in names:
        cands += glob.glob(str(BIN_DIR / "**" / n), recursive=True)
    if cands:
        # 取路径最深的那个：发行包常见 bin/ 里放可执行、根目录放 dll
        return Path(sorted(cands, key=lambda s: len(Path(s).parts))[-1])
    for d in os.environ.get("PATH", "").split(os.pathsep):
        for n in names:
            p = Path(d) / n
            if p.exists():
                return p
    sys.exit(f"没找到 llama-server，检查 {BIN_DIR}（或用 LLAMACPP_BIN 指过去）")


def probe(port, timeout=3):
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def stop(port):
    """按端口找监听进程并结束整棵进程树（llama-server 会 fork 子进程）。"""
    if os.name == "nt":
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace").stdout
        killed = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                subprocess.run(["taskkill", "/PID", parts[4], "/T", "/F"],
                               capture_output=True)
                killed += 1
        print(f"端口 {port}: 已停止 {killed} 个进程" if killed
              else f"端口 {port} 上没有服务")
        return
    # POSIX：按命令行里的端口匹配
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    pids = [ln.split()[0] for ln in out.splitlines()
            if "llama-server" in ln and f"--port {port}" in ln]
    for pid in pids:
        subprocess.run(["kill", "-9", pid], capture_output=True)
    print(f"端口 {port}: 已停止 {len(pids)} 个进程" if pids else f"端口 {port} 上没有服务")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081,
                    help="监听端口，要和 config.GLM_BASE 一致（默认 8081）")
    ap.add_argument("--ngl", type=int, default=99, help="offload 到 GPU 的层数")
    ap.add_argument("--ctx", type=int, default=8192,
                    help="上下文长度。宽表会被截断，读 30 列以上折页统计表时开 16384")
    ap.add_argument("--threads", type=int, default=12, help="CPU 线程数")
    ap.add_argument("--models", help="模型目录（覆盖自动解析）")
    ap.add_argument("--bin", help="llama-server 所在目录（覆盖自动解析）")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.stop:
        stop(args.port)
        return

    s = probe(args.port)
    if s:
        print(f"服务已在运行: http://127.0.0.1:{args.port}  ({s.get('status')})")
        return
    if args.check:
        print(f"端口 {args.port} 没有服务")
        return

    mdir = Path(args.models) if args.models else MODEL_DIR
    bdir = Path(args.bin) if args.bin else BIN_DIR
    main_model, mmproj = mdir / MAIN.name, mdir / MMPROJ.name
    for f, label in ((main_model, "主模型"), (mmproj, "视觉投影 mmproj")):
        if not f.exists():
            sys.exit(f"{label}缺失: {f}\n"
                     f"  下载 GLM-OCR 的 GGUF 权重放进 {mdir}，或用 --models 指定目录。")
        print(f"  {label}: {f.name}  {f.stat().st_size / 1024 ** 2:.0f} MB")

    exe = find_server() if not args.bin else next(
        iter(sorted(glob.glob(str(bdir / "**" / "llama-server*"), recursive=True))), None)
    if not exe:
        exe = find_server()

    cmd = [str(exe), "-m", str(main_model), "--mmproj", str(mmproj), "--jinja",
           "-ngl", str(args.ngl), "-c", str(args.ctx), "-np", "1",
           "-t", str(args.threads), "--repeat-penalty", "1.0",
           "--host", "127.0.0.1", "--port", str(args.port)]
    print(f"\n启动: {' '.join(cmd[:4])} …\n")
    t0 = time.time()
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"\n服务已退出（运行 {time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
