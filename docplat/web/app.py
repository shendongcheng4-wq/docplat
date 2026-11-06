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
from .. import pipeline, store, preflight

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
    from .. import config as C
    from ..legacy import render_text
    # 竖排族谱（config.is_vertical）表格按列读、从右往左 —— 否则一页世系图
    # 会摊成「次子 平長子 次子 …」这种横拼的行，列序整个反了
    return render_text(_read_text(pg), "flat",
                       vertical=C.is_vertical(pg.get("doc_id")))


def _read_display(pg: dict, render: str) -> str:
    """页面只读视图：跟导出的 txt 同一套排版（段首缩进、段间不空行）。

    校对时看到的形态要和交付的 txt 一致，否则「页面上顶格、导出缩进两边对不上」。
    编辑态仍给 render 原文 —— 缩进是展示层的事，不能写回数据库。
    """
    from ..legacy import book_layout
    return book_layout(render)


def _brief(pg: dict, verified: dict, notes: dict | None = None) -> dict:
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
        # 备注只带内容，前端用 title 显示；不占版面
        "note": (notes or {}).get(pg["hash"], ""),
    }


# ---------- 页面 ----------

@app.get("/")
def index():
    # 静态资源带版本号：/static/* 给了 max_age=3600，改了 app.js 之后不光刷
    # 浏览器直接拿缓存里的旧脚本（改了半天界面没变）。版本号取两个文件的最新
    # mtime，改过就换 URL，浏览器自然会重新取。
    static = Path(__file__).resolve().parent / "static"
    try:
        ver = int(max((static / n).stat().st_mtime for n in ("app.js", "app.css")))
    except OSError:
        ver = 0
    return render_template("index.html", asset_ver=ver)


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
    notes = store.note_map(doc)
    return jsonify({"doc": doc, "kind": kind, "total": total,
                    "offset": offset, "limit": limit,
                    "pages": [_brief(r, ver, notes) for r in rows]})


@app.get("/api/search")
def api_search():
    """全书正文搜索（不是文件名/页号筛选）。

    以前想找「某句话在第几页」只能一页页点开看。正文都在本地 md 里，
    几百页扫一遍是百毫秒级，没有理由不做。
    """
    doc = _doc()
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify({"doc": doc, "total": 0, "pages": [], "terms": []})
    limit = min(int(request.args.get("limit", 200)), 500)
    return jsonify({"doc": doc, **pipeline.search_text(doc, q, limit)})


@app.get("/api/page")
def api_page():
    doc = _doc()
    h = request.args.get("hash", "")
    pg = _page_or_404(doc, h)
    v = store.verification_of(doc, h)
    text = _read_text(pg)
    render = _read_render(pg)
    note = store.get_note(doc, h)

    # 相邻页：跨页接续的句子要对照上一页末尾，族谱/扫描歪页特别需要。
    # 按 page_no 查而不是按当前列表的下标 —— 队列里这一批页是不连续的。
    prev = store.neighbor(doc, pg["page_no"], -1)
    nxt = store.neighbor(doc, pg["page_no"], +1)

    def _nb(x, tail):
        return ({"page_no": x["page_no"], "hash": x["hash"],
                 "peek": pipeline.page_peek(x, tail)} if x else None)

    return jsonify({
        **_brief(pg, {h: v} if v else {}, {h: note.get("body", "")}),
        "text": text,
        "render": render,
        "display": _read_display(pg, render),
        "width": pg["width"], "height": pg["height"],
        "cover": pg["cover"],
        "status": pg["status"],
        "verified_at": v.get("created", ""),
        "verified_note": v.get("note", ""),
        "verified_verdict": v.get("verdict", ""),
        "retries": pg["retries"],
        "note_at": note.get("updated", ""),
        "prev": _nb(prev, True),
        "next": _nb(nxt, False),
    })


@app.get("/api/history")
def api_history():
    """这一页被改过几次、每份是什么样。没有它，改错一次就找不回来。"""
    doc = _doc()
    h = request.args.get("hash", "")
    _page_or_404(doc, h)
    return jsonify({"doc": doc, "hash": h,
                    "versions": pipeline.undo_history(doc, h)})


@app.post("/api/restore")
def api_restore():
    d = request.get_json(force=True) or {}
    doc, h = _doc(d.get("doc", "")), d.get("hash", "")
    if not h:
        abort(400, "缺少 hash")
    try:
        n = pipeline.undo_restore(doc, h, d.get("name", ""))
    except KeyError as e:
        abort(404, str(e))
    except ValueError as e:
        abort(400, str(e))
    return jsonify({"ok": True, "chars": n})


@app.post("/api/note")
def api_note():
    """页级备注：随时能写，不改变这一页的核对状态。

    以前备注只能塞进 verifications.note，那等于「写备注 = 已核对」，
    会把待人工的页悄悄移出队列，所以单独开一张表。
    """
    d = request.get_json(force=True) or {}
    doc, h = _doc(d.get("doc", "")), d.get("hash", "")
    if not h:
        abort(400, "缺少 hash")
    _page_or_404(doc, h)
    store.set_note(doc, h, d.get("note", ""))
    return jsonify({"ok": True, "note": store.get_note(doc, h).get("body", "")})


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


@app.post("/api/batch")
def api_batch():
    """一次处理多页：批量核对 / 撤销 / 判为空白页 / 转 VL。

    为什么要有它：待人工最多的一卷有 43 页，一页一次点击、还要等自动跳下一条，
    核完一本书就是几百次点击。勾选 + 一次提交把这件事变成两三下。

    「转 VL」（2026-11-06 用户要的）：待人工里 GLM 根本读不了的页（目录点线
    死循环、图版当表格拆），勾一批直接排进 VL 队列 —— 之前只能一页页去命令行。
    """
    d = request.get_json(force=True) or {}
    doc = _doc(d.get("doc", ""))
    action = d.get("action", "")
    hashes = [h for h in (d.get("hashes") or []) if isinstance(h, str) and h][:2000]
    if action not in ("verify", "unverify", "blank", "to_vl"):
        abort(400, f"未知批量操作：{action}")
    if not hashes:
        abort(400, "没有勾选任何页")
    try:
        r = pipeline.batch_action(doc, hashes, action)
    except ValueError as e:
        abort(400, str(e))
    return jsonify({"ok": True, **r, "counts": store.queue_counts(doc)})


@app.post("/api/rerun")
def api_rerun():
    """重跑单页。可选换模型（2026-11-06 用户要的：待人工的页重跑要能换模型）。

    · channel=glm（或留空 = 这一页原来的通道）：当场在这个进程里跑，几秒到几十秒。
    · channel=vl：**不当场跑**。Paddle 要占满显存（~11GB），在 Web 进程里加载它
      会把整台机器拖住；这里只把这页排进 VL 队列（status=pending、channel=vl），
      前端提示「先停掉 GLM 服务，再点顶部『换 VL 重跑 N』」。
    """
    d = request.get_json(force=True) or {}
    doc, h = _doc(d.get("doc", "")), d.get("hash", "")
    pg = _page_or_404(doc, h)
    want = (d.get("channel") or "").strip()
    if want and want not in ("glm", "vl"):
        return jsonify({"error": f"不支持的通道：{want}"}), 400
    ch = want or pg.get("channel") or pipeline.route_of(pg.get("layout") or "")
    if ch == "vl":
        n = store.to_channel(doc, [h], "vl", why="工作台「重跑」选了 PaddleOCR-VL")
        return jsonify({"ok": True, "queued": "vl", "pages": n,
                        "counts": store.queue_counts(doc)})
    try:
        st = pipeline.rerun_one(doc, h, ch)
    except Exception as e:                      # 模型服务没起等
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "stat": st, "channel": ch})


@app.get("/api/preflight")
def api_preflight():
    """导出体检：先算清楚「这一导，会缺什么」。

    为什么需要：`pipeline.export` 对还没跑出结果的页是**静默跳过**的
    （out_path 不存在 → 当成没字 → 不计入正文），而 txt 里不写页数，
    少了几页肉眼看不出来。商业史料 316 页里有 1 页没跑，导出来就是 315 页
    的内容，界面上却只显示「已导出」。

    三档口径在 `docplat/preflight.py`：`miss` 无结果、`zero` 有文件但 0 字且
    没有空白页判定、`blank` 0 字但流程已判空白页。**只有前两档拦人**，
    否则族谱里几十页真空白会让每次导出都弹窗（2026-11-05 用户反馈）。
    """
    doc = _doc()
    store.init()
    pages = store.queue(doc, "all", limit=10 ** 9)
    got = preflight.classify(pages, store.verified_set(doc))
    counts = store.queue_counts(doc)
    return jsonify({
        "doc": doc,
        "total": len(pages),
        "miss": got["miss"],
        "zero": got["zero"],
        "blank": got["blank"],
        "blocking": preflight.blocking(got),
        "counts": counts,
    })


@app.get("/api/export")
def api_export():
    """合并全书，直接写到「桌面\\书籍导出\\<丛书>\\<卷>.txt」。

    不再走浏览器下载：文件已经落在用户要的位置，再下一份到「下载」文件夹
    只会多出一份不知道哪份是新的副本。返回路径让人直接去开。
    """
    doc = _doc()
    try:
        p = pipeline.export(doc)
    except PermissionError as e:
        # 目录建不出来是最常见的一种「导出失败」，而原始报错只是一句
        # `WinError 5 拒绝访问`，看不出是权限问题还是代码问题（2026-11-06）。
        # 典型诱因：桌面目录的 ACL 里有一条显式的「本账户 RX」排在继承的
        # 完全控制之前，导致能读不能建 —— 修法见这条提示。
        return jsonify({"error": (
            f"没有权限写导出目录（{e}）。\n"
            "桌面\\书籍导出 被限制了“新建文件/文件夹”权限时会这样。\n"
            "修复：在文件资源管理器里右键该文件夹 → 属性 → 安全 → 高级，"
            "把当前账户的权限改为“完全控制”，或执行：\n"
            '  icacls "%USERPROFILE%\\Desktop" /grant "Administrator:(OI)(CI)(F)"'
        )}), 500
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
    """探活 + 服务灯。

    `glm` 是识别服务（127.0.0.1:8081）当前通不通：以前只能靠点「开始识别」
    等它自动拉起、或者去翻日志才知道它死了。查询参数 ?glm=0 可跳过这次探测
    （脚本轮询时不必每次都等那 0.6 秒超时）。
    """
    try:
        store.init()
        docs = store.list_docs()
        out = {"ok": True, "docs": len(docs),
               "pages": sum(d["pages"] for d in docs)}
        if request.args.get("glm", "1") != "0":
            out["glm"] = _glm_up(0.6)
            out["glm_base"] = C.GLM_BASE
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.post("/api/glm/start")
def api_glm_start():
    """把识别服务拉起来（顶栏那盏灯红了时用）。

    为什么要在服务进程里做：GLM 必须挂在**常驻**的父进程下。
    从一次性的命令行/脚本里起，调用方一退，Windows 就把整棵进程树收掉 ——
    llama-server 消失、正在跑的批跟着静默中断（2026-11-05 就是这么断的）。
    serve 是常驻的，它起的子进程才活得久。
    """
    if _glm_up(0.6):
        return jsonify({"ok": True, "already": True, "glm": True})
    try:
        _auto.glm_start(wait=180, quiet=True)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    up = _glm_up(5)
    if not up:
        return jsonify({"error": "拉起后仍连不上 127.0.0.1:8081，"
                                 "看 scripts/glm_serve.py 的输出"}), 500
    return jsonify({"ok": True, "already": False, "glm": True})


@app.post("/api/glm/stop")
def api_glm_stop():
    """停掉识别服务，把显存让给 PaddleOCR-VL。

    为什么需要：换 VL 跑必须先腾显存（GLM 4.5GB + Paddle 14.8GB，16GB 卡装不下
    两个），以前这条只能在命令行干 —— 用户在工作台里点了「转 VL」，还得自己去
    找命令行停服，等于把最麻烦的一步留给人。这里替他把这一步做了（等显存真的
    还回来才返回，约 30 秒上限）。
    """
    if not _glm_up(0.6):
        return jsonify({"ok": True, "already": True})
    try:
        _auto.glm_stop(quiet=True)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"ok": True, "glm": _glm_up(1.0)})


@app.get("/api/log")
def api_log():
    """识别日志尾部。跑批出错时不用再去翻文件。"""
    n = max(1, min(int(request.args.get("lines", 120)), 800))
    for p in (C.DATA / "run.log", C.ROOT / "_serve_run.log"):
        try:
            if not p.is_file() or p.stat().st_size == 0:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        return jsonify({"path": str(p), "lines": lines[-n:]})
    return jsonify({"path": "", "lines": []})


def serve(host="127.0.0.1", port=7788, debug=False):
    store.init()
    print(f"校对工作台：http://{host}:{port}")
    app.run(host=host, port=port, debug=debug, threaded=True)
