# -*- coding: utf-8 -*-
"""丛书级批量驱动：扫目录 → 逐卷导入 → 逐卷识别 → 逐卷导出。

一条命令把一整套书跑完：

    plat.bat auto --root "D:\\books\\某氏宗谱"

为什么要「逐卷」而不是「整批塞一起」：
  · import 是幂等的（同一文件夹重复导入落回同一个文档），中断后重跑不会翻倍
  · run 按卷统计，一卷跑砸不影响下一卷；导出本来就是 per-doc 的
  · 每卷跑完立刻能拿到 txt，不用等整套跑完

为什么要分「阶段」而不是一卷到底：
  GLM（4.5GB）和 PaddleOCR-VL（14.8GB）在 16GB 卡上不能同时驻留。
  所以 vl 页必须等所有卷的 glm 都跑完、把显存让出来，再统一处理：

      阶段 glm     所有卷：识别正文页 + 标转人工 + 导出
      阶段 vl      停 GLM → 所有卷：跑升级页
      阶段 retext  起 GLM → 所有卷：vl 失败的图页换纯文本重读 → 重新导出

默认只跑 glm（安全、无人值守）。要一口气跑完用 --stages all。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config as C
from . import pipeline, store

# GLM 服务的启动脚本（有 --stop / --check，参数默认值与人工启动的一致）
# 默认用项目自带的 scripts/glm_serve.py；指向别处用 DOCPLAT_GLM_SERVE。
GLM_SERVE = Path(os.environ.get("DOCPLAT_GLM_SERVE")
                 or (C.ROOT / "scripts" / "glm_serve.py"))

STAGES = ("glm", "vl", "retext")
LOCK = C.DATA / "auto.lock"


# ---------- 卷序 ----------

_CN = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_VOL_RE = re.compile(r"卷\s*([〇零一二三四五六七八九十百\d]+)\s*[）)]?\s*$")


def cn2int(s: str) -> int:
    """中文数字转整数。支持 十一 / 二十三 / 一百零八 / 纯阿拉伯数字。"""
    s = s.strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if s in _CN:
        return _CN[s]
    if "百" in s:
        a, _, b = s.partition("百")
        n = (cn2int(a) if a else 1) * 100
        b = b.lstrip("零")
        return n + (cn2int(b) if b else 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return (cn2int(a) if a else 1) * 10 + (_CN.get(b, 0) if b else 0)
    return 0


def vol_key(name: str):
    """按卷号排序。没有「卷N」字样的排最后，按名字兜底。"""
    m = _VOL_RE.search(name or "")
    return (cn2int(m.group(1)) if m else 9999, name or "")


def _count_img(d: Path) -> int:
    try:
        return sum(1 for x in d.iterdir()
                   if x.is_file() and x.suffix.lower() in pipeline.IMG_EXT)
    except OSError:
        return 0


def _is_vol(d: Path) -> bool:
    """这一层是不是「一卷」。

    判据：图片就摆在它自己身上，或者它有一个「图片堆放层」子目录（JPG/图片/images）。
    两条都要求「卷目录 → 图片目录」只差一层，所以丛书目录不会被误判成卷，
    而卷里再往下的那层（tif 堆放目录叫卷名）也不会被当成新卷。
    """
    if _count_img(d):
        return True
    try:
        subs = [x for x in d.iterdir()
                if x.is_dir() and x.name.lower() in C.IMG_DIR_NAMES]
    except OSError:
        return False
    return any(_count_img(x) for x in subs)


def _find_vols(d: Path, depth: int = 0) -> list:
    """自顶向下找卷：这一层就是卷就停，不是就往下钻一层层找。"""
    if _is_vol(d):
        return [d]
    if depth > 4:
        return []
    out = []
    try:
        subs = sorted((x for x in d.iterdir() if x.is_dir()), key=lambda p: str(p))
    except OSError:
        return []
    for s in subs:
        out += _find_vols(s, depth + 1)
    return out


def pick_src(vol: Path):
    """在一卷里挑出「要跑的那一套图」。

    同一批页经常有两套：tif 原扫（一页 6MB）+ jpg 压缩（一页 0.5MB）。
    混着导入会让这本书变成两倍页数，所以必须先挑一套：

      1. 有整理好的图片子目录（JPG / 图片 / images…）→ 只用它，卷根部散图不要
      2. 没有子目录 → 用卷根部的图

    多个图片子目录都装图时取最大的一份，其余记下来提示，避免悄悄漏页。
    """
    subs = []
    try:
        for x in vol.iterdir():
            if x.is_dir() and x.name.lower() in C.IMG_DIR_NAMES:
                n = _count_img(x)
                if n:
                    subs.append((x, n))
    except OSError:
        pass
    subs.sort(key=lambda t: -t[1])
    if subs:
        src, n = subs[0]
        return src, n, [f"{x.name}({m})" for x, m in subs[1:]]
    n = _count_img(vol)
    if n:
        return vol, n, []
    return None, 0, []


def discover(root) -> list:
    """扫一个目录，返回要跑的卷。

    root 既可以是丛书目录（下面多个卷），也可以直接是某一卷（含 JPG）。
    判定不靠名字，靠「哪些目录里直接躺着图片」。
    """
    root = Path(str(root).rstrip("\\/"))
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")

    books = []
    for vol in _find_vols(root):
        src, n, others = pick_src(vol)
        if not src:
            continue
        books.append({
            "vol": str(vol),
            "src": str(src),
            "n": n,
            "book": vol.name,
            "series": vol.parent.name or vol.name,
            "doc_id": pipeline.find_doc_by_src(src) or pipeline.doc_id_for(vol),
            "others": others,
        })
    books.sort(key=lambda b: vol_key(b["book"]))
    return books


# ---------- 模型服务 ----------

def glm_up(timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(f"{C.GLM_BASE}/health", timeout=timeout):
            return True
    except Exception:
        return False


# llama-server 命令行里的模型标记：只认挂着 GLM 的那个进程，
# 别把机器上别的 llama 也杀了（同一台机器可能还有别的模型在服务）。
GLM_TAG = "glm-ocr"

# 「同一时刻只准一个人起 GLM」的锁文件。写成模块变量是为了让自检能换成临时路径。
GLM_LOCK = C.DATA / "_glm_start.lock"


def glm_pids() -> list:
    """本机在跑的 GLM llama-server，按启动时间从早到晚。

    psutil 没装就返回空 —— 那只是没法查重，不影响起服务本身。
    """
    try:
        import psutil
    except ImportError:
        return []
    rows = []
    for p in psutil.process_iter(["pid", "name", "create_time", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "llama-server.exe":
                continue
            if GLM_TAG not in " ".join(p.info["cmdline"] or []).lower():
                continue
            rows.append((p.info["create_time"], p.info["pid"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return [pid for _, pid in sorted(rows)]


def glm_prune(keep: int = 1) -> list:
    """杀掉多余的 GLM 实例，只留最早的 keep 个；返回被杀掉的 PID。

    为什么需要这一步：glm_start 是「先查 glm_up() 再 Popen」，两步之间不是原子的。
    2026-11-06 09:02 工作台重启时两个调用方同时进来（页面加载的自动拉起 + 手动点
    「开始识别」），各起了一个 llama-server，相隔 1 秒，各吃 4.5GB 显存。
    表面上一切正常（GLM 能用、跑批也不报错），但后面 Paddle 一加载就 OOM，而且
    两个实例都在 LISTENING 8081，请求落到哪个看运气。
    保留最早的那个：它通常已经绑住端口、模型也热了。
    """
    pids = glm_pids()
    if len(pids) <= keep:
        return []
    try:
        import psutil
    except ImportError:
        return []
    killed = []
    for pid in pids[keep:]:
        try:
            psutil.Process(pid).kill()
            killed.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed


@contextlib.contextmanager
def _spawn_lock(wait: float = 300):
    """跨进程的「同一时刻只准一个人起 GLM」锁。

    用锁文件当互斥量（O_CREAT|O_EXCL 在 Windows 上也是原子的），不用 threading.Lock：
    起 GLM 的可能是工作台进程，也可能是命令行跑批的进程，两拨人不开在同一个进程里。

    yield True  = 我拿到锁，我去起；
    yield False = 等的时候别人已经起好了，直接收工，不用陪它把 240 秒等完。
    """
    lock = GLM_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:                                   # 持有者被杀的残留锁，过期作废
                stale = time.time() - lock.stat().st_mtime > wait
            except OSError:
                stale = False
            if stale:
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            if glm_up(1):                          # 别人起好了，没必要再抢
                yield False
                return
            if time.time() - t0 > wait:
                raise TimeoutError(f"等 GLM 启动锁超时（{lock}），删掉它再试")
            time.sleep(1)
    try:
        yield True
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def glm_stop(quiet=False):
    if not GLM_SERVE.exists():
        raise FileNotFoundError(f"找不到 GLM 启动脚本：{GLM_SERVE}")
    subprocess.run([sys.executable, str(GLM_SERVE), "--stop"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):                    # 等显存真的还回来
        if not glm_up(1):
            if not quiet:
                print("  GLM 服务已停，显存已让出")
            return
        time.sleep(1)
    raise RuntimeError("GLM 服务停不下来（端口 8081 仍在响应），手停一下再跑")


def glm_start(wait: int = 240, quiet=False):
    killed = glm_prune()                # 先清掉上次遗留的重复实例
    if killed and not quiet:
        print(f"  已清掉多余的 GLM 实例：{killed}")
    if glm_up():
        if not quiet:
            print("  GLM 服务已在线")
        return
    if not GLM_SERVE.exists():
        raise FileNotFoundError(f"找不到 GLM 启动脚本：{GLM_SERVE}")
    # 「先查再起」不是原子的，两个调用方同时进来会各起一个 llama-server。
    # 用跨进程锁把这一段串起来，等锁的人看到服务已就绪就直接返回。
    with _spawn_lock(wait + 60) as mine:
        if not mine:
            if not quiet:
                print("  GLM 服务已就绪（由别的调用拉起）")
            return
        if glm_up():                   # 拿到锁的这一刻可能刚好被别人起好了
            if not quiet:
                print("  GLM 服务已在线")
            return
        # DETACHED_PROCESS(0x8) 不能省：只加 CREATE_NO_WINDOW 的话，GLM 仍是调用方的
        # 子进程，调用方（一次性的命令行、工作台重启、工具里起的临时 shell）一退，
        # Windows 会把整棵进程树收走 —— 表现为「跑到一半 llama-server 突然没了，
        # 跑批子进程跟着静默消失，run.log 卡在某页、没有 traceback」（2026-11-05 就是这样）。
        # 脱离之后 GLM 自己活，谁起的都不影响它。
        flags = (0x08000000 | 0x00000008) if os.name == "nt" else 0
        subprocess.Popen([sys.executable, str(GLM_SERVE)], cwd=str(GLM_SERVE.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, close_fds=True,
                         creationflags=flags)
        t0 = time.time()
        while time.time() - t0 < wait:
            if glm_up(2):
                if not quiet:
                    print(f"  GLM 服务已就绪（{time.time() - t0:.0f}s）")
                return
            time.sleep(2)
    raise RuntimeError(f"GLM 服务 {wait}s 内没起来，检查 {GLM_SERVE}")


# ---------- 锁（防止工作台同时点「开始识别」） ----------

def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259               # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def lock_holder() -> int:
    """返回正在跑批的 pid，没有则 0。工作台也用它拦重复启动。"""
    try:
        pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0
    if _alive(pid):
        return pid
    LOCK.unlink(missing_ok=True)
    return 0


def lock_acquire():
    pid = lock_holder()
    if pid:
        raise RuntimeError(f"已经有一批在跑（pid {pid}），同一时间只能跑一批："
                           f"批量跑批和工作台的「开始识别」会抢同一张显卡。")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")


def lock_release():
    LOCK.unlink(missing_ok=True)


# ---------- 排队标记（--wait 在等锁） ----------
# 工作台据此显示「已排队：下一阶段 vl」，不然 glm 刚结束、vl 还没起来的
# 那几十秒里进度条会突然消失，看着像跑完了。

QUEUE = C.DATA / "auto.queue.json"


def _write_queue(stages):
    try:
        QUEUE.write_text(json.dumps(
            {"pid": os.getpid(), "stages": list(stages),
             "since": time.strftime("%H:%M")}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _clear_queue():
    QUEUE.unlink(missing_ok=True)


def queued() -> dict:
    try:
        d = json.loads(QUEUE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not _alive(int(d.get("pid") or 0)):
        QUEUE.unlink(missing_ok=True)
        return {}
    return d


def _queued_after(pid: int):
    """正在跑的这一批后面还排着别的阶段吗（glm 跑完自动接 vl 就是它）。"""
    q = queued()
    return q if q and int(q.get("pid") or 0) != int(pid or 0) else None


# ---------- 当前阶段的状态（进度条要用） ----------
# 只靠库里的瞬时统计做进度会出两个岔子：
#   1. glm 阶段边入库边跑，总量一路在涨，百分比往回跳
#   2. vl 阶段所有页在 glm 阶段就「有结论」了，按 done/all 算不动，一直是满格
# 所以在每个阶段开始时把「这一阶段要跑多少页」记下来，进度 = 目标 - 还剩。

STATE = C.DATA / "auto.state.json"


def _write_state(**kw):
    try:
        STATE.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _clear_state():
    STATE.unlink(missing_ok=True)


def _left_in(stage: str) -> int:
    """库里这个阶段还有多少页没跑（含正在跑的）。"""
    if stage == "retext":
        return _retext_pages()
    ch = "vl" if stage == "vl" else "glm"
    with store.conn() as c:
        r = c.execute("SELECT COUNT(*) n FROM pages WHERE status IN ('pending','running')"
                      " AND channel=?", (ch,)).fetchone()
        return r["n"]


# ---------- 实时状态（工作台顶栏用） ----------

def snapshot() -> dict:
    """把正在跑的批量进度翻译成工作台认识的格式。

    不靠日志解析，全部从库里算 —— 命令行被重定向到哪个文件都不影响。
    """
    pid = lock_holder()
    if not pid:
        return {"running": False}

    st = _read_state()
    stage = st.get("stage") or "glm"
    label = {"glm": "正文识别", "vl": "升级页识别", "retext": "纯文本重读"}.get(stage, stage)

    if st.get("target"):
        target = st["target"]
        done = max(0, target - _left_in(stage))
        counts = {"all": target, "done": done, "review": 0, "failed": 0, "escalated": 0}
    else:                                   # 没有阶段快照（老进程）→ 按全库凑合
        with store.conn() as c:
            rows = [dict(r) for r in c.execute("""
                SELECT COUNT(*) all_pages, SUM(status='done') done,
                       SUM(status='review') review, SUM(status='failed') failed,
                       SUM(status='pending' AND channel='vl') escalated
                FROM pages""")]
        r = rows[0] if rows else {}
        counts = {k: (r.get(v) or 0) for k, v in
                  (("all", "all_pages"), ("done", "done"), ("review", "review"),
                   ("failed", "failed"), ("escalated", "escalated"))}
        target = counts["all"]
        done = counts["done"] + counts["review"] + counts["failed"] + counts["escalated"]

    # 正在跑哪一卷（按最后写入时间挑，僵尸 running 页时间戳是旧的）
    with store.conn() as c:
        cur = [dict(x) for x in c.execute(
            "SELECT doc_id, MAX(updated) m FROM pages WHERE status='running'"
            " GROUP BY doc_id ORDER BY m DESC LIMIT 1")]
    tail = "切换卷中…"
    if cur:
        cd = cur[0]["doc_id"]
        c2 = store.queue_counts(cd)
        tail = f"{cd} · {c2['done'] + c2['review'] + c2['escalated']}/{c2['all']}"

    return {"running": True, "batch": True, "doc": "整套书", "channel": stage,
            "stage_label": label, "done": done, "left": max(0, target - done),
            "counts": counts, "tail": tail,
            "paused": C.PAUSE_FLAG.exists(), "sec": 0, "pid": pid,
            "queued": _queued_after(pid)}


# ---------- 主流程 ----------

def _pending_n(doc_id: str, channel: str) -> int:
    with store.conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n FROM pages WHERE doc_id=? AND status IN ('pending','failed')"
            " AND retries < ? AND (channel=? OR channel IS NULL)",
            (doc_id, C.MAX_RETRY, channel)).fetchone()
        return r["n"]


# 能靠「换 GLM 读纯文本」救回来的 vl 失败页（与 pipeline._vl_failed 同一口径）。
# 单独写一份而不是 import 那个私有函数：这里是纯 SQL 数数，不读页内容。
_VL_BAD = ("err LIKE '表格页仅%' OR err LIKE '拆出%' OR err LIKE '疑似重复生成%'")


def _retext_pages(doc_id: str = "") -> int:
    sql = ("SELECT COUNT(*) n FROM pages WHERE channel='vl'"
           f" AND status IN ('review','failed') AND ({_VL_BAD})")
    args = ()
    if doc_id:
        sql = ("SELECT COUNT(*) n FROM pages WHERE doc_id=? AND channel='vl'"
               f" AND status IN ('review','failed') AND ({_VL_BAD})")
        args = (doc_id,)
    with store.conn() as c:
        return c.execute(sql, args).fetchone()["n"]


def _stage_n(doc_id: str, stage: str) -> int:
    """这一阶段在这卷上还要跑多少页。retext 没有 pending 状态，按「待重读」数。"""
    if stage in ("glm", "vl"):
        return _pending_n(doc_id, stage)
    if stage == "retext":
        return _retext_pages(doc_id)
    return 0


def _fmt_sec(s: float) -> str:
    m = int(round(s / 60))
    if m < 1:
        return f"{s:.0f}s"
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _bar(i: int, n: int, width: int = 14) -> str:
    filled = int(round(width * i / n)) if n else width
    return "█" * filled + "·" * (width - filled)


def run_batch(root, stages=("glm",), do_export=True, dry=False,
              only_missing=True, quiet=False, wait=False) -> dict:
    """跑一整套书。返回汇总数据。"""
    store.init()
    root = str(root or "").strip().strip('"').strip("'")
    books = discover(root)
    if not books:
        raise ValueError(f"「{root}」下面没找到任何装图片的文件夹")

    total_img = sum(b["n"] for b in books)
    print(f"扫描 {Path(str(root)).name}：{len(books)} 卷，共 {total_img} 张图")
    docs = {d["id"]: d for d in store.list_docs()}
    for b in books:
        have = docs.get(b["doc_id"])
        n_db = have["pages"] if have else 0
        flag = f"库里已有 {n_db} 页" if n_db else "未入库"
        extra = ""
        if b.get("others"):
            extra = f"   （另有 {', '.join(b['others'])} 未使用）"
        print(f"  · {b['book']:<28} {Path(b['src']).name}/ {b['n']:>4} 图   "
              f"{flag}   → {b['doc_id']}{extra}")

    if dry:
        print("\n（预演，没有真的动手。去掉 --dry 就跑）")
        return {"books": books, "dry": True}

    # --wait：排在正在跑的那批后面。glm 跑完自动接 vl 就是这么串起来的 ——
    # 不用人盯着命令行，也不用在 glm 结束的一瞬间手快敲第二条命令。
    if wait:
        pid = lock_holder()
        if pid:
            print(f"\n  已有批次在跑（pid {pid}），等它结束后自动接着跑"
                  f" {'→'.join(stages)}")
            _write_queue(stages)
            t_w = time.time()
            tick = 0
            while lock_holder():
                time.sleep(5)
                tick += 1
                if tick % 12 == 0:               # 每分钟报一次，证明没死等
                    print(f"    仍在等…（已等 {_fmt_sec(time.time() - t_w)}）")
            _clear_queue()
            print(f"  上一批已结束（等了 {_fmt_sec(time.time() - t_w)}），开始跑"
                  f" {'→'.join(stages)}")

    lock_acquire()
    t_all = time.time()
    rows = []
    try:
        # 阶段间要切显存，先确保起始状态对
        if "glm" in stages or "retext" in stages:
            glm_start(quiet=quiet)

        # 先把所有卷入库（只算 hash + 判版面，不碰模型），再开始跑。
        # 这样每个阶段的「要跑多少页」一开始就是准的，工作台的进度条
        # 不会因为中途又有新卷入册而往回跳。
        for b in books:
            b["skip"] = False
            doc_id = b["doc_id"]
            if (only_missing and store.get_doc(doc_id)
                    and _pending_n(doc_id, "glm") == 0
                    and _count_img(Path(b["src"])) == b["n"]):
                b["skip"] = True
                continue
            try:
                b["doc_id"] = pipeline.import_dir(b["src"])["doc_id"]
            except Exception as e:
                b["skip"] = True
                b["err"] = str(e)
                print(f"  ✗ {b['book']} 入库失败：{e}")

        for stage in stages:
            if stage == "vl":
                print(f"\n===== 阶段 vl：{len(books)} 卷的升级页 =====")
                print("  这一步要独占显存，先停 GLM")
                glm_stop(quiet=quiet)
            elif stage == "retext" and "vl" in stages:
                print(f"\n===== 阶段 retext：图页换纯文本重读 =====")
                glm_start(quiet=quiet)

            # 阶段各自的待跑数：glm 完的卷在 vl 阶段还有活（升级页），
            # 所以这里不能沿用入库时算出来的「整卷跳过」，必须按阶段重新数。
            target = sum(_stage_n(b["doc_id"], stage)
                         for b in books if not b.get("err"))
            _write_state(stage=stage, target=target, t0=t_all, books=len(books))
            print(f"  本阶段待跑 {target} 页")

            # vl 阶段可以让所有卷共用一个引擎，省掉每卷重载 Paddle 的一分多钟
            shared = {} if stage == "vl" else None
            try:
                for i, b in enumerate(books, 1):
                    row = _one_book(b, stage, i, len(books), do_export,
                                    only_missing, quiet, shared)
                    if row:
                        rows.append(row)
            except Exception as e:
                # 一个阶段炸了不该带走后面所有阶段：vl 显存不够挂掉，
                # retext（换 GLM 读纯文本）往往还能救回一部分页。
                print(f"  ✗ 阶段 {stage} 中断：{e}")
                rows.append({"book": f"阶段 {stage}", "doc_id": "", "err": str(e),
                             "all": 0, "done": 0, "review": 0, "escalated": 0,
                             "failed": 0, "sec": 0.0})
            finally:
                for e in (shared or {}).values():
                    try:
                        e.close()
                    except Exception:
                        pass
    finally:
        _clear_state()
        # 收尾一定要把 GLM 拉回来：vl 阶段把它停了，中途异常退出的话
        # 工作台和下一轮都会连不上模型，而没人值守时不会有人发现。
        if "vl" in stages:
            try:
                if not glm_up():
                    print("  收尾：把 GLM 服务重新拉起来")
                    glm_start(quiet=quiet)
            except Exception as e:
                print(f"  ⚠ GLM 没能重新启动：{e}")
        lock_release()

    _summary(rows, books, stages, time.time() - t_all)
    if rows:
        _write_last(rows, time.time() - t_all)
    return {"rows": rows, "sec": round(time.time() - t_all, 1)}


def _write_last(rows, sec):
    """留一份「上一次批量跑完的结果」，工作台打开时能告诉你刚才干了什么。"""
    last = {}
    for r in rows:
        last[r["doc_id"]] = {k: r.get(k, 0) for k in
                             ("book", "all", "done", "review", "escalated", "failed")}
    try:
        (C.DATA / "last_batch.json").write_text(json.dumps(
            {"at": time.strftime("%Y-%m-%d %H:%M"), "sec": round(sec),
             "docs": list(last.values())}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _one_book(b, stage, i, n, do_export, only_missing, quiet, shared) -> dict:
    t0 = time.time()
    tag = f"[{i}/{n}] {b['book']}"
    doc_id = b["doc_id"]
    blank = {"all": 0, "done": 0, "review": 0, "escalated": 0, "failed": 0, "sec": 0.0}

    if b.get("err"):
        return {"book": b["book"], "doc_id": doc_id, "err": b["err"], **blank}

    # 入库在 run_batch 开头已经统一做过了（见那里的注释），这里只管跑。
    # 注意「这一卷 glm 跑完了」不等于「vl 阶段不用跑」——升级页正是留在后面的活，
    # 所以跳不跳只看本阶段还有多少页，不看入库时的整卷 skip 标记。
    left = _stage_n(doc_id, stage) if only_missing else 1
    stat = {}
    if left == 0 and only_missing:
        note = "已跑完，跳过"
        c = store.queue_counts(doc_id)
        print(f"{tag}  {note}  → 已识别 {c['done']}/{c['all']}"
              f"  待人工 {c['review']}  待VL {c['escalated']}")
    else:
        if stage == "retext":
            r = pipeline.fallback_text(doc_id, quiet=True)
            note = f"重读 {r['pages']} 页，替换 {r['replaced']}"
        else:
            if not quiet:
                print(f"{tag}  开始 {stage}（{left} 页待跑）"
                      + ("  [共用引擎]" if shared is not None else ""))
            kw = {"quiet": True}
            if shared is not None:
                kw["engines"] = shared
            stat = pipeline.run(doc_id, stage, **kw)
            note = (f"{stage} 跑 {stat.get('pages', 0)} 页："
                    f"正常 {stat.get('done', 0)} 待复核 {stat.get('review', 0)} "
                    f"升级 {stat.get('escalate', 0)} 失败 {stat.get('failed', 0)}")

    out = ""
    if do_export and stage in ("glm", "vl", "retext"):
        try:
            p = pipeline.export(doc_id)
            out = f"{p.stat().st_size / 1024:.0f}KB"
        except Exception as e:
            out = f"导出失败({e})"

    c = store.queue_counts(doc_id)
    row = {"book": b["book"], "doc_id": doc_id, "note": note, "out": out,
           "all": c["all"], "done": c["done"], "review": c["review"],
           "escalated": c["escalated"], "failed": c["failed"],
           "sec": round(time.time() - t0, 1)}
    print(f"{tag}  {note}  → 已识别 {c['done']}/{c['all']}"
          f"  待人工 {c['review']}  待VL {c['escalated']}"
          + (f"  导出 {out}" if out else "")
          + f"  {_fmt_sec(row['sec'])}")
    return row


def _summary(rows, books, stages, sec):
    if not rows:
        return
    # 每卷只保留最后一条（多阶段时后面的覆盖前面的）
    last = {}
    for r in rows:
        last[r["doc_id"]] = r

    # 合计必须只加「每卷最后一条」。早期版本直接遍历 rows，
    # 多阶段跑一遍就把同一卷加了三次（8 卷 1691 页的丛书，合计显示 5073 页），
    # 上面的表格却是去重后的 8 行 —— 表里一行行加起来对不上合计，很误导。
    agg = {k: 0 for k in ("all", "done", "review", "escalated", "failed")}
    for r in last.values():
        for k in agg:
            agg[k] += r.get(k, 0)
    print("\n" + "═" * 74)
    print(f"{'卷':<30}{'页':>6}{'已识别':>7}{'待人工':>7}{'待VL':>6}{'失败':>6}  导出")
    print("─" * 74)
    for r in last.values():
        # 用 get 兜底：某卷在导入阶段就失败时那一行没有统计字段，
        # 早期版本会在这里 KeyError，整批都跑完了却崩在最后的表格上。
        print(f"{str(r.get('book', '?'))[:28]:<30}{r.get('all', 0):>6}"
              f"{r.get('done', 0):>7}{r.get('review', 0):>7}"
              f"{r.get('escalated', 0):>6}{r.get('failed', 0):>6}  {r.get('out', '')}")
    print("─" * 74)
    print(f"{'合计':<30}{agg['all']:>6}{agg['done']:>7}{agg['review']:>7}"
          f"{agg['escalated']:>6}{agg['failed']:>6}"
          f"   用时 {_fmt_sec(sec)}")
    print("═" * 74)

    if agg["escalated"] and "vl" not in stages:
        print(f"\n还有 {agg['escalated']} 页在 vl 通道等着（要先停 GLM 换 Paddle）。")
        print("  plat.bat auto --root \"<丛书目录>\" --stages glm,vl")
        print("  或者直接 --stages all 连重读一起做完。")
