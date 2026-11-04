"""校对工作台 HTTP 层。

只做前端需要的事，四个核心接口：
  queue   队列（默认看「有问题」的页，而不是全部页）
  page    单页正文 + 全部元数据
  save    保存人工修改
  verify  标记/取消「已核对」

设计约束（v1 的教训）：
  · 不再一次返回全部页 —— 几十万页时那条 JSON 会把浏览器打死
  · 不做进程管理 —— 跑批走命令行，工作台只读结果
  · 「已核对」是 verifications 表的一条记录，不改 pages.status，可逆
"""
from pathlib import Path

from flask import (Flask, abort, jsonify, render_template, request, send_file,
                   send_from_directory)

from .. import config as C
from .. import auto as _auto
from .. import pipeline, store

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_AS_ASCII"] = False


# ---------- 工具 ----------

def _doc(doc_id: str = "") -> str:
    doc_id = doc_id or request.args.get("doc", "")
    if doc_id:
        return doc_id
    docs = store.list_docs()
    if not docs:
        abort(404, "还没有任何文档，先用 plat.bat ingest 入库")
    return docs[0]["id"]


def _page_or_404(doc_id: str, h: str) -> dict:
    pg = store.get_page(doc_id, h)
    if not pg:
        abort(404, f"没有这一页：{h}")
    return pg


def _read_text(pg: dict) -> str:
    p = pg.get("out_path")
    if p and Path(p).exists():
        return Path(p).read_text(encoding="utf-8").rstrip("\n")
    return ""


def _read_render(pg: dict) -> str:
    """给人看的版本：HTML 表格摊成分行文字（复核时看格子不如看行）。"""
    from ..legacy import render_text
    return render_text(_read_text(pg), "flat")


def _brief(pg: dict, verified: dict) -> dict:
    """队列里一行需要的信息，不包含正文（正文按页单独取）。"""
    src = Path(pg["src_path"]).name
    v = verified.get(pg["hash"])
    return {
        "hash": pg["hash"],
        "page_no": pg["page_no"],
        "src": src,
        "status": pg["status"],
        "layout": pg["layout"],
        "channel": pg["channel"],
        "engine": pg["engine"],
        "chars": pg["chars"] or 0,
        "sec": pg["sec"],
        "score": pg["score"],
        "err": pg["err"] or "",
        "verified": v["verdict"] if v else "",
    }


# ---------- 页面 ----------

@app.get("/")
def index():
    return render_template("index.html")


# ---------- API ----------

@app.get("/api/docs")
def api_docs():
    store.init()
    docs = store.list_docs()
    for d in docs:
        d["counts"] = store.queue_counts(d["id"])
    return jsonify({"docs": docs})


@app.post("/api/ingest")
def api_ingest():
    """工作台的「导入」。填本地文件夹路径即可，不上传图片。

    为什么不做上传：这是本机单人工具，图片本来就在磁盘上，
    走 HTTP 把 242 张图再复制一份，只多了等待和一份垃圾副本。
    文档号按来源目录去重，同一个文件夹重复导入不会建出第二个文档。
    """
    data = request.get_json(silent=True) or {}
    try:
        r = pipeline.import_dir(data.get("src"), (data.get("doc_id") or "").strip())
    except (FileNotFoundError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, **r})


@app.get("/api/queue")
def api_queue():
    doc = _doc()
    kind = request.args.get("kind", "review")
    limit = min(int(request.args.get("limit", 50)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    q = request.args.get("q", "").strip()
    if kind not in ("review", "escalated", "failed", "lowscore", "sample",
                    "done", "all"):
        abort(400, f"未知队列：{kind}")

    rows = store.queue(doc, kind, limit, offset, q)
    total = store.queue_count(doc, kind, q)
    with store.conn() as c:
        ver = {r["hash"]: {"verdict": r["verdict"], "created": r["created"]}
               for r in c.execute(
                   "SELECT hash, verdict, created FROM verifications WHERE doc_id=?",
                   (doc,))}
    return jsonify({"doc": doc, "kind": kind, "total": total,
                    "offset": offset, "limit": limit,
                    "pages": [_brief(r, ver) for r in rows]})


@app.get("/api/page")
def api_page():
    doc = _doc()
    h = request.args.get("hash", "")
    pg = _page_or_404(doc, h)
    v = store.verification_of(doc, h)
    return jsonify({
        **_brief(pg, {h: v} if v else {}),
        "text": _read_text(pg),
        "render": _read_render(pg),
        "width": pg["width"], "height": pg["height"],
        "cover": pg["cover"],
        "status": pg["status"],
        "verified_at": v.get("created", ""),
        "verified_note": v.get("note", ""),
        "verified_verdict": v.get("verdict", ""),
        "retries": pg["retries"],
    })


@app.post("/api/page")
def api_save():
    d = request.get_json(force=True) or {}
    doc, h = _doc(d.get("doc", "")), d.get("hash", "")
    if not h:
        abort(400, "缺少 hash")
    try:
        n = pipeline.save_text(doc, h, d.get("text", ""))
    except KeyError as e:
        abort(404, str(e))
    return jsonify({"ok": True, "chars": n})


@app.post("/api/verify")
def api_verify():
    d = request.get_json(force=True) or {}
    doc, h = _doc(d.get("doc", "")), d.get("hash", "")
    if not h:
        abort(400, "缺少 hash")
    _page_or_404(doc, h)
    if d.get("verified", True):
        store.verify(doc, h, d.get("verdict", "ok"), d.get("note", ""))
    else:
        store.unverify(doc, h)
    return jsonify({"ok": True, "counts": store.queue_counts(doc)})


@app.post("/api/rerun")
def api_rerun():
    """重跑单页。只支持走 HTTP 的 GLM 通道 —— PaddleOCR-VL 要占满显存，
    在 Web 进程里加载它会把整台机器拖住，那种情况请用命令行分批跑。
    """
    d = request.get_json(force=True) or {}
    doc, h = _doc(d.get("doc", "")), d.get("hash", "")
    pg = _page_or_404(doc, h)
    if pg.get("channel") == "vl":
        return jsonify({"error": "这一页规划给 PaddleOCR-VL，需占满显存；"
                                 "请停掉 GLM 服务后命令行跑 plat.bat run --doc "
                                 f"{doc} --channel vl"}), 409
    try:
        st = pipeline.rerun_one(doc, h)
    except Exception as e:                      # 模型服务没起等
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "stat": st})


@app.get("/api/export")
def api_export():
    """合并全书，直接写到「桌面\\书籍导出\\<丛书>\\<卷>.txt」。

    不再走浏览器下载：文件已经落在用户要的位置，再下一份到「下载」文件夹
    只会多出一份不知道哪份是新的副本。返回路径让人直接去开。
    """
    doc = _doc()
    try:
        p = pipeline.export(doc)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "path": str(p), "size": p.stat().st_size})


# ---------- 跑批（从页面点「开始识别」）----------
# 识别必须在**子进程**里跑，不能挂在 Flask 进程上：
#   · VL 通道要在自己的进程里加载 Paddle（约 11.5GB），和服务进程共用会互相拖死
#   · 一批 266 页要跑十几分钟，HTTP 请求早就超时了
# 所以这里只做起停和状态查询，真正的活由 `python -m docplat run` 干 ——
# 也就是命令行那条路，页面只是替用户按了回车。

import json
import os
import subprocess
import sys
import time

_RUN = {"proc": None, "doc": "", "channel": "", "t0": 0.0, "log": None}


def _alive(p) -> bool:
    return bool(p and p.poll() is None)


def _glm_up(timeout: float = 2.0) -> bool:
    import urllib.request
    try:
        url = C.GLM_BASE.rstrip("/") + "/v1/models"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _tail(path: Path, n: int = 1) -> str:
    """日志末尾那一行 —— 进度条上显示「当前跑到哪一页」。

    必须过滤掉 sitecustomize 那两行：这台机器的 Python 启动时总会打一条
    PermissionError（WorkBuddy 的 shim 目录不可读），它恰好是日志第一行，
    缓冲区一没刷新就会被当成「最后一行」显示出来。
    """
    try:
        lines = [x.strip() for x in path.read_text(encoding="utf-8",
                                                    errors="replace").splitlines()]
    except Exception:
        return ""
    noise = ("sitecustomize", "PYTHONVERBOSE", "PermissionError")
    lines = [x for x in lines if x and not any(k in x for k in noise)]
    return lines[-1] if lines else ""


@app.post("/api/run")
def api_run():
    data = request.get_json(silent=True) or {}
    doc = _doc(data.get("doc", ""))
    channel = (data.get("channel") or "glm").strip()
    if channel not in ("glm", "vl"):
        return jsonify({"error": f"不支持的通道：{channel}"}), 400
    if _alive(_RUN["proc"]):
        return jsonify({"error": f"{_RUN['doc']} 还在跑，等它跑完或先停掉"}), 409
    # 命令行那边可能在跑整批（plat.bat auto）。两边都往同一张显卡上压，
    # 同一个文档还会重复识别同一页，所以这里认锁直接拦住。
    pid = _auto.lock_holder()
    if pid:
        return jsonify({"error": f"正在跑整批（进程 {pid}），等它跑完再手动启动；"
                                 f"想中途停下，用命令行的 Ctrl+C"}), 409
    if channel == "glm" and not _glm_up():
        # 整批跑完那一刻 GLM 常跟着一起没了（批跑是一棵子进程树，Windows 上
        # 树倒的时候孙进程一起走），用户回来点「开始识别」只会看到一句
        # 「先把模型服务起来」，还得自己开命令行。这里顺手拉起来，约 4 秒。
        try:
            _auto.glm_start(wait=180, quiet=True)
        except Exception as e:
            return jsonify({"error": f"GLM 服务没在 127.0.0.1:8081 上，"
                                     f"自动拉起也失败了：{e}"}), 400
        if not _glm_up(5):
            return jsonify({"error": "GLM 服务没在 127.0.0.1:8081 上，"
                                     "自动拉起后仍连不上，看一下 _glm_test 的日志"}), 400
    if channel == "vl" and _glm_up(0.8):
        return jsonify({"error": "VL 要占满显存：先停掉 GLM 服务再跑"}), 400

    C.PAUSE_FLAG.unlink(missing_ok=True)      # 上次的暂停别带到这一轮
    log = (C.DATA / "run.log").open("w", encoding="utf-8")
    flags = 0x08000000 if os.name == "nt" else 0        # CREATE_NO_WINDOW
    try:
        # -u 必须有：stdout 是文件时 Python 会攒满一块才刷，
        # 进度条上的「当前页」就会一直卡在十几页前。
        p = subprocess.Popen(
            [sys.executable, "-u", "-m", "docplat", "run",
             "--doc", doc, "--channel", channel],
            cwd=str(C.ROOT), stdout=log, stderr=subprocess.STDOUT,
            creationflags=flags)
    except Exception as e:
        log.close()
        return jsonify({"error": f"起不来子进程：{e}"}), 500
    _RUN.update(proc=p, doc=doc, channel=channel, t0=time.time(), log=log)
    return jsonify({"ok": True, "doc": doc, "channel": channel})


@app.get("/api/run")
def api_run_status():
    p = _RUN["proc"]
    running = _alive(p)
    # 不是工作台起的活，也可能是命令行的整批跑批（plat.bat auto）——
    # 那是另一个进程，工作台看不到它的 stdout，所以进度全从库里算。
    if not running and _auto.lock_holder():
        return jsonify(_auto.snapshot())
    # 排队的下一批（plat.bat auto --wait）：glm 刚结束、vl 还没起来的那几十秒，
    # 不告诉前端一声，进度条会突然消失，看着像整批跑完了。
    if not running:
        q = _auto.queued()
        if q:
            return jsonify({"running": False, "queued": q})
    doc = _RUN["doc"] if p else ""
    out = {"running": running, "doc": doc, "channel": _RUN["channel"],
           "paused": running and C.PAUSE_FLAG.exists(),
           "sec": round(time.time() - _RUN["t0"], 0) if running else 0}
    if doc:
        c = store.queue_counts(doc)
        out["counts"] = c
        # 进度按「已有结论的页」算，pending 才是真正还没跑的
        out["done"] = c["done"] + c["review"] + c["failed"]
        out["left"] = max(0, c["all"] - out["done"])
        out["tail"] = _tail(C.DATA / "run.log")
        if not running and p is not None:
            out["code"] = p.returncode
    return jsonify(out)


@app.get("/api/last-batch")
def api_last_batch():
    """上一次批量跑完的结果。页面打开时提示一句，不用盯着命令行。"""
    p = C.DATA / "last_batch.json"
    try:
        return jsonify(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return jsonify({})


@app.post("/api/run/pause")
def api_run_pause():
    """软暂停：置一个标志文件，跑批在**下一页开始前**看到它就原地等。

    粒度是一页 —— 当前这页会跑完并落盘。比强挂进程稳：Windows 没有 SIGSTOP，
    挂住了这一页的 HTTP 请求会晾到超时。
    """
    C.PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    C.PAUSE_FLAG.write_text("1", encoding="utf-8")
    return jsonify({"ok": True, "paused": True})


@app.post("/api/run/resume")
def api_run_resume():
    C.PAUSE_FLAG.unlink(missing_ok=True)
    return jsonify({"ok": True, "paused": False})


@app.post("/api/run/stop")
def api_run_stop():
    p = _RUN["proc"]
    C.PAUSE_FLAG.unlink(missing_ok=True)      # 暂停中也能停，别把标志留到下一轮
    if _alive(p):
        p.terminate()               # 收到 SIGTERM 后 run() 会正常收尾（已跑完的页保住）
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
        return jsonify({"ok": True, "running": _alive(p)})

    # 命令行整批：杀进程树。已经落盘的页都在库里，重跑会从剩下的接着来。
    pid = _auto.lock_holder()
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, creationflags=0x08000000)
        _auto.lock_release()
    return jsonify({"ok": True, "running": False})


@app.get("/img/<doc>/<h>")
def img(doc, h):
    pg = _page_or_404(doc, h)
    p = Path(pg["src_path"])
    if not p.exists():
        abort(404, "原图不存在")
    return send_file(p, conditional=True, max_age=3600)


@app.route("/static/<path:fn>")
def static_file(fn):
    return send_from_directory(app.static_folder, fn)


@app.get("/api/health")
def health():
    """给命令行/脚本探活用。"""
    try:
        store.init()
        docs = store.list_docs()
        return jsonify({"ok": True, "docs": len(docs),
                        "pages": sum(d["pages"] for d in docs)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def serve(host="127.0.0.1", port=7788, debug=False):
    store.init()
    print(f"校对工作台：http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)
