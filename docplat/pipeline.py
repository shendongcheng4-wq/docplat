"""编排层：入库 → 路由 → 识别 → 质检 → 落盘 → 报表。

一条铁律（v1 的教训）：**正文文件里只放识别结果**。
「待复核」「低置信」这些状态一律写数据库，绝不能写进 md ——
v1 把提示写进正文，导致唯一的评测基准被污染，模型判分从 100% 掉到 2.3%。
"""
import hashlib
import re
import time
from pathlib import Path

from . import config as C
from . import imgio, store, toc
from .legacy import redundancy_of, vl_only_fragments
from .models import get as get_engine
from .models.base import EngineError
from .router import BLANK, IMAGE, TABLE, TEXT, Classifier, KnownLayout, evaluate
from .router.quality import DONE, REVIEW, Verdict

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# 导出时判定「这页等于没字」的门槛：去掉空白后不足这么多字，就当空白页不出。
# 取 10 是因为模型对空白/残页的典型输出是「markdown」「恭」「!」这种 1~8 个字，
# 而真正的正文页极少短于 10 个字；真漏了还有 pages.status='review' 兜着。
BLANK_MIN = 10

# 短页里的「篇名下界」（2026-11-04 用户定：图版篇名要进正文）。
# 图版页的篇名典型 4~8 字（「名胜与历史」「名人与溧阳」「民俗与特产」「天目湖——我们共」），
# 而模型对残页吐的杂音是 1~3 个孤立字（族谱卷一 p15~p18 的「恭」「而」「封」「命」）。
# 所以 4~9 字的纯中文照正文输出，1~3 字仍当噪声跳过。
# 注：透印页（纸张太薄、背面文字透过来）常被读成几个不成词的淡字（溧阳旅游文化 p16
# 「客旅昌固史」），这类要靠人工判，代码分不出来 —— 判成透印的页直接清空文件即可。
TITLE_MIN = 4

# 模型对模糊/空白影像常回一句英文说明（"The image provided is too blurry ..."），
# 那是它在解释自己没读出来，不是族谱正文。整页没有一个汉字、又命中这些说法 → 不出。
META_EN = re.compile(r"blurry|low[- ]resolution|unable to|cannot recognize|"
                     r"cannot provide|cannot read|not clearly visible|not visible|"
                     r"unreadable|no visible text|too small to|I'?m sorry|I cannot", re.I)


# ---------- 工具 ----------

def natural_key(p: Path):
    return [int(x) if x.isdigit() else x.lower()
            for x in re.split(r"(\d+)", p.name)]


def sha1_16(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def out_path(h: str) -> Path:
    return C.PAGES / h[:C.SHARD] / f"{h}.md"


def route_of(layout: str) -> str:
    if layout in (BLANK, IMAGE):
        return "human"
    if layout == TABLE:
        return "vl"
    return "glm"


def _dims(p: Path):
    # 倒置/侧躺的扫描件按显示方向记宽高（90/270 度会交换），否则版式判断拿到反的尺寸
    return imgio.dims(p)


# ---------- 入库 ----------

def ingest(doc_id: str, src_dir: str, name: str = "") -> dict:
    store.init()
    src = Path(src_dir)
    if not src.exists():
        raise FileNotFoundError(src)

    files = sorted((p for p in src.rglob("*") if p.suffix.lower() in IMG_EXT),
                   key=natural_key)
    store.upsert_doc(doc_id, name or doc_id, str(src))

    seen, dup = set(), 0
    for i, p in enumerate(files, 1):
        h = sha1_16(p)
        if h in seen:
            dup += 1          # 同一份文件重复上传，只保留一条
            continue
        seen.add(h)
        w, hgt = _dims(p)
        store.add_page(doc_id, h, i, p, w, hgt)

    return {"doc_id": doc_id, "files": len(files), "pages": len(seen), "dup": dup}


# ---------- 导入 ----------

_SAFE_ID = re.compile(r'[\\/:*?"<>|#?&\s]+')


def doc_id_for(src) -> str:
    """从导入目录推一个文档号：取卷名，去掉 URL / 文件名里不能用的字符。"""
    _, book = book_of(src)
    return _SAFE_ID.sub("", book or "") or "doc"


def find_doc_by_src(src) -> str:
    """同一个来源目录重复导入时复用已有文档号。"""
    try:
        target = Path(str(src)).resolve()
    except Exception:
        return ""
    for d in store.list_docs():
        sd = d.get("src_dir") or ""
        if not sd:
            continue
        try:
            if Path(sd).resolve() == target:
                return d["id"]
        except Exception:
            continue
    return ""


def _guard_rescan(doc_id: str, src: Path, n_img: int):
    """防止「同一本书的图片被重新压缩过」导致一页变两页。

    页的身份是内容 hash。同一个文件夹如果只是重新导出过（桌面原图 vs 工作副本
    实测 sha1 完全不同），重复导入不会命中去重，而是把整本书再添一遍，
    待人工队列直接翻倍 —— 这种错发现得晚，代价是几百页白看。

    判定：库里已有页 >= 20，这次的文件数差不多，但一张都对不上 → 拦住。
    真要继续（比如整批换成了高清重扫），走命令行并用另一个文档号。
    """
    have = store.page_hashes(doc_id)
    if len(have) < 20:
        return
    files = [x for x in src.rglob("*") if x.suffix.lower() in IMG_EXT]
    if len(files) < len(have) * 0.5:
        return                      # 明显是「补几页」，不是「整本换一遍」
    same = sum(1 for x in files if sha1_16(x) in have)
    if same >= max(2, len(have) * 0.05):
        return
    raise ValueError(
        f"「{src.name}」里的图片和库里文档 {doc_id} 已有的 {len(have)} 页一张都对不上"
        f"（可能重新导出/压缩过）。这样导入会让这本书变成两套页面。\n"
        f"要另建一本请填别的文档号（如 {doc_id}2）；不要往已有文档里追加。")


def import_dir(src, doc_id: str = "", name: str = "") -> dict:
    """把「导入一个文件夹」做完整：校验 → 认文档号 → 入库 → 预路由。

    命令行和工作台的「导入」按钮共用这一个入口，行为才不会分叉。
    文档号按来源目录去重：同一个文件夹重复导入，落回原来的文档，
    不会一卷建出两个 id（page 的身份是内容 hash，本来也重复不了）。
    """
    raw = str(src or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("请填写图片所在的文件夹路径")
    p = Path(raw)
    if not p.exists():
        raise FileNotFoundError(f"路径不存在：{p}")
    if not p.is_dir():
        raise ValueError(f"这是文件不是文件夹：{p}")

    n_img = sum(1 for x in p.rglob("*") if x.suffix.lower() in IMG_EXT)
    if not n_img:
        raise ValueError(f"文件夹里没有图片（支持 {', '.join(sorted(IMG_EXT))}）")

    doc_id = doc_id or find_doc_by_src(p) or doc_id_for(p)
    _guard_rescan(doc_id, p, n_img)
    series, book = book_of(p)
    r = ingest(doc_id, str(p), name or book or doc_id)
    r["doc_id"] = doc_id
    r["name"] = book
    r["series"] = series
    r["routed"] = preroute(doc_id)
    r["target"] = str(export_target(str(p)))
    return r


# ---------- 识别 ----------

def preroute(doc_id: str) -> int:
    """给还没有版面的页判版面，并写入 channel。

    有了这一步，才能按通道分批跑（先 glm 阶段，再停模型服务跑 vl 阶段）——
    否则 pending(channel='glm') 永远查不到，因为入库时 channel 是空的。
    """
    store.init()
    clf = Classifier(KnownLayout([C.DATA / f for f in C.TRUTH_FILES]))
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT hash, src_path FROM pages WHERE doc_id=? AND layout IS NULL",
            (doc_id,))]
    for r in rows:
        img = Path(r["src_path"])
        layout = clf.classify(img, page_key=img.stem)
        store.set_page(doc_id, r["hash"], layout=layout, channel=route_of(layout))
    return len(rows)


def _paused() -> bool:
    return C.PAUSE_FLAG.exists()


def _wait_if_paused(quiet: bool = False, i: int = 0, n: int = 0):
    """跑到页与页之间，发现暂停标志就原地等，标志清掉再继续。

    为什么不用进程挂起：Windows 没有 SIGSTOP，强挂住只会把这个页的 HTTP 请求
    晾在一边，等恢复时模型那边已经超时了。按页暂停最稳，也最符合直觉 ——
    当前这一页跑完就停，已经落盘的结果一定保住。
    """
    if not _paused():
        return False
    if not quiet:
        print(f"  ⏸ 已暂停，停在 {i}/{n} 页 —— 去掉暂停标志就接着跑")
    while _paused():
        time.sleep(1)
    if not quiet:
        print(f"  ▶ 继续，从 {i}/{n} 页接着跑")
    return True


_PIPE_ONLY = re.compile(r"^[\s|｜]+$")
_DOTS_RUN = re.compile(r"[…\.．·]{2,}")


def _dots_degenerate(text: str, min_run: int = 40) -> bool:
    """输出是不是「点线型退化」—— 目录页读到引导符后一路吐省略号。

    水西颂 p0014：32596 字里 3 万字是一串「…」，条目只开到「战略部署的历史贡献」
    就断了。质量闸门靠压缩冗余抓到了它（0.99），但抓到了也没用：光重读一遍
    结果一样。真正的区别在**提示词**，不在图（见 config.GLM_TOC_PROMPT）。

    判据取「最长连续点号串 ≥ 40」：正常目录页每行也就 3~30 个点，模型死循环
    时是一条几百上千个点的长串，两者差一个数量级，不会误判。
    """
    if not text:
        return False
    return max((len(m.group()) for m in _DOTS_RUN.finditer(text)), default=0) >= min_run


def _pipe_degenerate(text: str, min_lines: int = 50) -> bool:
    """输出是不是退化成了「一整片空单元格」—— 整行只有竖线、没有字。

    先进性教育活动 p192 就是这样：标题、表头、第一行都读对了，之后一路吐
    `|`，4142 个字符里 4058 个是竖线、4071 行每行一个字。质量闸门（压缩冗余）
    抓不到它 —— 每行都不一样，只是都是空的；结构闸门倒是抓到了（判成分格条目
    页升级 vl），但那等于把一页本来能读的表推给 Paddle。

    判据是「整行只有竖线/空白的行 ≥ 50」：正常表格的行里总有字，一行都碰不到。
    """
    n = 0
    for ln in (text or "").split("\n"):
        if _PIPE_ONLY.match(ln):
            n += 1
            if n >= min_lines:
                return True
    return False


def _reread_degenerate(eng, img: Path, layout: str, key: str, rec, v,
                       stat: dict, quiet: bool = False, tag: str = ""):
    """GLM 输出退化时重读一页，返回 (rec, v)。三招，按「可靠度」从高到低：

    1) 目录页点线（p9/p10、水西颂 p0014）：抹掉点线再读。GLM 读到第一个「……」
       就停不下来，一路吐到 6000 token 上限（3.2 万字、冗余 1.00）。
       抹点线是确定性解法：p0014 从 32596 字死循环 → 578 字 DONE，24 条点线全抹净、
       字一个不错。实现见 toc.find_dots（2026-11-06 改成「定带 + 只抹带内小连通域」）。
    2) 目录提示词（`GLM_TOC_PROMPT`）：不动图，只换问法。同一张原图有时 3.4 秒出
       721 字，但**不稳定**（连跑 3 次只中 1 次），所以排在抹图之后当兜底。
    3) 复杂表格（p192）：换 `GLM_TABLE_PROMPT` 再读。合并单元格 + 竖排表头会把
       GLM 带进「吐空单元格」的坑；换成一句中文表格指令，它反而吐完整 HTML 表。

    判定退化的口径：输出是一片空单元格，或者质量闸门判了「重复生成」。
    接受重读结果的门槛是「判 DONE **且** 不再退化」，否则保留原结果不动。
    """
    if not (_pipe_degenerate(rec.text)
            or (v.status != DONE and "重复生成" in (v.reason or ""))):
        return rec, v

    def _try(target, prompt: str = ""):
        try:
            r = eng.recognize(target, hint=layout, prompt=prompt)
        except Exception:
            return None, None
        return r, evaluate(r, layout)

    def _ok(r, vv):
        return (vv is not None and vv.status == DONE
                and not _pipe_degenerate(r.text))

    def _take(r, vv, what: str):
        if not quiet:
            print(f"  {tag}{img.name}  ⤺ {what}：{rec.chars} → {r.chars} 字")
        return r, Verdict(vv.status, vv.score, f"正常（{what}）")

    # ---- 1) 抹掉目录点线（确定性的一招，先走）----
    clean = toc.strip_dot_leaders(img, C.DATA / "_toc", name=key)
    if clean is not None:
        r1, v1 = _try(clean)
        if _ok(r1, v1):
            stat["toc"] += 1
            return _take(r1, v1, "已抹目录点线重读")

    # ---- 2) 换目录提示词（不动图，兜抹图的漏）----
    if _dots_degenerate(rec.text):
        r2, v2 = _try(img, C.GLM_TOC_PROMPT)
        if _ok(r2, v2):
            stat["toc_prompt"] += 1
            return _take(r2, v2, "改用目录提示词重读")

    # ---- 3) 换表格提示词 ----
    r3, v3 = _try(img, C.GLM_TABLE_PROMPT)
    if _ok(r3, v3):
        stat["table"] += 1
        return _take(r3, v3, "改用表格提示词重读")

    return rec, v


def _slow_note(sec: float, chars: int) -> str:
    """这一页是不是「干等」，而不是「字多所以慢」。

    单页耗时 ≈ 1.6s 预填充 + 0.002s/字（2026-11-05 按 2500+ 页拟合）。
    只有绝对值过 SLOW_PAGE_SEC、**并且**超过按字数预期的 SLOW_FACTOR 倍，
    才认为请求被晾在服务侧（GPU 停顿 / 服务重启）—— 实测那四页各 67 秒，
    却只有 630~690 字。反过来，3000 字的页跑 8 秒是正常的，不该标。
    """
    if sec < C.SLOW_PAGE_SEC:
        return ""
    expect = C.SLOW_BASE_SEC + chars * C.SLOW_SEC_PER_CHAR
    if sec < expect * C.SLOW_FACTOR:
        return ""
    return f"  ⏱ 慢（{chars} 字预期约 {expect:.1f}s，多半在等服务，不是模型慢）"


def run(doc_id: str, channel: str = "", limit: int = 0, quiet: bool = False,
        stop_on_engine_error: bool = True, only: list = None,
        engines: dict = None, allow_escalate: bool = True) -> dict:
    """跑待处理页。

    channel 留空 = 自动路由（glm/vl/human 都走）；指定则只跑该通道。
    human 通道不调模型，直接标 needs_review，等人来看。
    only 指定 hash 列表时只跑这几页（工作台的「重跑本页」用）。
    engines 传入一个字典即可跨调用复用引擎、且不由本次调用关闭 —— 批量跑多卷的
    vl 阶段要用它：Paddle 加载一次一分多钟，每卷重载一遍纯属浪费。
    allow_escalate=False 关掉结构闸门：单页「重跑」已经由人指定了模型，见 rerun_one。
    """
    store.init()
    # 上一次被 Ctrl+C / 杀进程打断时，正在跑的那一页会停在 'running'，
    # 而待跑查询只收 pending/failed —— 这些页就永远轮不到了，静默丢字。
    # run 是单进程串行的，进到这里时不可能有真的在跑的页，统一放回队列。
    if not only:
        with store.conn() as c:
            n = c.execute("UPDATE pages SET status='pending' WHERE doc_id=?"
                          " AND status='running'", (doc_id,)).rowcount
        if n and not quiet:
            print(f"[恢复] {n} 页上次没跑完，已放回队列")

    n_new = preroute(doc_id)
    if n_new and not quiet:
        print(f"[预路由] {n_new} 页已判定版面")

    pages = store.pending(doc_id, channel or None, limit, hashes=only)
    if not pages:
        if not quiet:
            print("没有待处理的页")
        return {"pages": 0, "done": 0, "review": 0, "failed": 0, "slow": 0,
                "toc": 0, "toc_prompt": 0, "table": 0, "sec": 0}

    clf = Classifier(KnownLayout([C.DATA / f for f in C.TRUTH_FILES]))
    shared = engines is not None
    engines = engines if engines is not None else {}
    stat = {"pages": 0, "done": 0, "review": 0, "failed": 0, "escalate": 0,
            "slow": 0, "toc": 0, "toc_prompt": 0, "table": 0, "sec": 0.0}
    t_all = time.time()

    for i, pg in enumerate(pages, 1):
        h = pg["hash"]
        img = Path(pg["src_path"])
        _wait_if_paused(quiet, i, len(pages))
        if not img.exists():
            store.bump_retry(doc_id, h, "原图不存在")
            stat["failed"] += 1
            continue
        stat["pages"] += 1

        # 版面（已判过就复用）
        layout = pg["layout"] or clf.classify(img, page_key=Path(img).stem)
        ch = channel or route_of(layout)
        store.set_page(doc_id, h, layout=layout, channel=ch, status="running")

        # 人工通道：不调模型
        if ch == "human":
            # 空白页不写、也不进复核队列（2026-11-03 用户定）：人看一眼也只能
            # 确认「确实没字」，纯属浪费。影像页仍要人工 —— 那是有内容要录。
            if layout == BLANK:
                store.set_page(doc_id, h, status=DONE, engine="", err="空白页，无正文")
                stat["done"] += 1
                if not quiet:
                    print(f"  [{i}/{len(pages)}] {img.name}  空白页，跳过复核")
                continue
            store.set_page(doc_id, h, status="review", engine="",
                           err="转人工：" + ("影像页" if layout == IMAGE else "大字/草书/空白页"))
            stat["review"] += 1
            if not quiet:
                print(f"  [{i}/{len(pages)}] {img.name}  {layout}  → 人工")
            continue

        if ch not in engines:
            engines[ch] = get_engine(ch)
        eng = engines[ch]

        try:
            rec = eng.recognize(img, hint=layout)
        except EngineError as e:
            store.set_page(doc_id, h, status="pending")
            stat["failed"] += 1
            if not quiet:
                print(f"  [{i}/{len(pages)}] {img.name}  引擎不可用：{e}")
            if stop_on_engine_error:
                break
            continue
        except Exception as e:                       # 单页异常不影响整批
            store.bump_retry(doc_id, h, f"{type(e).__name__}: {e}")
            stat["failed"] += 1
            if not quiet:
                print(f"  [{i}/{len(pages)}] {img.name}  失败：{e}")
            continue

        v = evaluate(rec, layout)

        # 退化重读：目录点线（p9/p10）、复杂表格吐空单元格（p192）都在这里救。
        # 只在判成退化时才动手，正常页零开销。
        if ch == "glm":
            rec, v = _reread_degenerate(eng, img, layout, h, rec, v, stat,
                                        quiet=quiet, tag=f"[{i}/{len(pages)}] ")

        # 空白页（2026-11-03 用户两次确认）：模型没读出一句像样的东西，就是空白页，
        # 不管图像看起来是白纸还是一团灰影 —— 原书空白页拍下来常常透出背面印的
        # 字（透印），图像侧分不开「透印的白页」和「真拍糊的正文」，所以不拿清晰度
        # 当判据。这类页既不出正文，也不进复核队列：人看一眼也只能确认「没字」。
        # 判据和 export 里「跳过空白页」用的是同一把尺，免得队列和导出各说一套。
        # 表格页不参与：表格页字少是图内小字漏识，那是真漏字，要留给人看。
        is_blank = False          # 真空白：连一个汉字都没有 → 落空文件、不进正文
        if layout != TABLE:
            bare = re.sub(r"\s", "", rec.text or "")
            has_cn = bool(re.search(r"[\u4e00-\u9fff]", bare))
            if not has_cn and (
                    not bare
                    or META_EN.search(rec.text or "")
                    or re.match(r"^(markdown|Markdown:)", (rec.text or "").strip())
                    or re.fullmatch(r"[\d\W_]+", bare)):
                v = Verdict(DONE, 1.0, "空白页，无正文")
                is_blank = True
            elif has_cn and len(bare) < BLANK_MIN:
                # 中文只剩几个字：多半是插图页上的篇名（「名人与溧阳」「民俗与特产」，
                # 2026-11-04 调原图确认是黑底白字/淡色竖排的图版标题），
                # 或者是跨页断句的前半（「天目湖——我们共」）。
                # **不能当空白清掉**——这些是真内容。
                # 4 字以上照正文收、不进复核队列（2026-11-04 用户定：篇名要进正文）；
                # 1~3 字眼看不出来是什么，留给人工。
                if len(bare) >= TITLE_MIN:
                    v = Verdict(DONE, 0.9, f"短页：图版篇名/断句残行（{len(bare)} 字）")
                else:
                    v = Verdict(REVIEW, 0.5, f"仅 {len(bare)} 字，疑模型杂音")

        # 落盘：只写正文（先写，升级到 vl 后会被覆盖；中途失败不至于丢内容）
        # 空白页落空文件：模型那句 "The image is too blurry to recognize" 是它在
        # 解释自己没读到字，不是内容。留着它，导出层虽然会过滤（见 export 的
        # META_EN 分支），但库和界面上一直挂着一句英文，用户翻工作台会以为识别错了
        # （2026-11-04 连问两次「p10 为什么显示这个、不能直接空白吗」）。
        op = out_path(h)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text("" if is_blank else (rec.text or "").rstrip() + "\n",
                      encoding="utf-8")

        # ---- 结构闸门：跑完 glm 才判得出「这页是不是分格条目」----
        # allow_escalate=False 只在「人点名了模型」的单页重跑里用：我选了 GLM，
        # 结果就别再自作主张把这页改成 pending+vl 丢进「升级」队列。
        # 想换 Paddle 有两条显式入口：重跑时选 VL、或勾几页按「转 VL」
        # （store.to_channel，2026-11-06 用户要的）。
        new_ch, why = (clf.escalate(rec.text, layout, ch) if allow_escalate
                       else ("", ""))
        if new_ch:
            store.set_page(doc_id, h, layout=TABLE, channel=new_ch, status="pending",
                           engine=rec.engine, chars=rec.chars,
                           sec=round(rec.sec, 2), out_path=str(op),
                           err=f"结构闸门升级：{why}")
            stat["escalate"] += 1
            slow = _slow_note(rec.sec, rec.chars)
            if slow:
                stat["slow"] += 1
            if not quiet:
                print(f"  [{i}/{len(pages)}] {img.name}  glm {rec.sec:5.1f}s "
                      f"{rec.chars:>4}字  ⤴ 升级 {new_ch}：{why}{slow}")
            continue

        store.set_page(doc_id, h, status=v.status, engine=rec.engine,
                       chars=0 if is_blank else rec.chars, sec=round(rec.sec, 2),
                       cover=v.score if v.status == DONE else None,
                       score=v.score, out_path=str(op), err=v.reason)
        stat["done" if v.status == DONE else "review"] += 1
        slow = _slow_note(rec.sec, rec.chars)
        if slow:
            stat["slow"] += 1
        if not quiet:
            flag = "" if v.status == DONE else "  ⚠"
            print(f"  [{i}/{len(pages)}] {img.name}  {layout:<5} {rec.engine:<4} "
                  f"{rec.sec:5.1f}s {rec.chars:>4}字  {v.reason}{flag}{slow}")

    stat["sec"] = round(time.time() - t_all, 1)
    if not shared:                      # 外部传进来的引擎由外部负责关
        for e in engines.values():
            e.close()
    # 慢页单独点一句：不然用户只看到「跑了很多秒」，分不清是模型慢还是干等
    if stat["slow"] and not quiet:
        print(f"\n  {stat['slow']} 页单页耗时远超按字数的预期（≥{C.SLOW_PAGE_SEC:.0f}s）——"
              f"一般是请求在服务侧干等（GPU 停顿/服务重启），这些页的内容通常没问题")
    if stat["escalate"] and not quiet:
        print(f"\n  {stat['escalate']} 页被结构闸门升级到 {C.ESCALATE_TO} 通道。"
              f"先停掉 GLM 服务（显存要让出来），再跑："
              f"python -m docplat run --doc {doc_id} --channel {C.ESCALATE_TO}")
    # vl 跑完要知道还有一步：图页被拆成空壳/漏识的，可以用 GLM 列成纯文本清单
    if channel == C.ESCALATE_TO and stat["review"] and not quiet:
        print(f"  {stat['review']} 页待复核。其中「表格页仅 N 字 / 拆格失败」的图页，"
              f"可以换纯文本重读：python -m docplat retext --doc {doc_id}")
    return stat


def rerun_one(doc_id: str, h: str, channel: str = "") -> dict:
    """重跑单页。工作台「重跑」用。

    先把状态重置为 pending（清空重试计数），否则 pending() 查不到它。
    原正文保留到新结果写盘为止 —— 中途失败不至于把已有内容弄丢。

    channel 留空 = 用这一页原来的通道；给了就是「换模型重跑」（2026-11-06 用户
    要的：待人工的页重跑时要能换模型）。**换到 vl 不走这里** —— Paddle 要占满
    显存，得由 run 子进程在命令行/批跑里加载，`api_rerun` 会把那种请求改写成
    `store.to_channel()`，只把这页排进 VL 队列。

    allow_escalate=False：人已经点名用哪个模型了，结构闸门别再自作主张把它
    改成 pending+vl（「我选了 GLM，结果你又给我换 Paddle」）。
    """
    pg = store.get_page(doc_id, h)
    if not pg:
        raise KeyError(f"没有这一页：{h}")
    ch = channel or pg.get("channel") or route_of(pg.get("layout") or "")
    if ch == C.ESCALATE_TO:
        # 只有 API 会调到这里，而 api_rerun 对 vl 已经先转成队列了。
        # 留着是为了万一以后有别的调用方：绝不能在 Web 进程里加载 Paddle。
        raise ValueError(f"这一页要跑 {C.ESCALATE_TO}（PaddleOCR-VL），"
                         f"不能在服务进程里重跑；请用命令行 "
                         f"plat.bat run --doc {doc_id} --channel {ch}")
    store.set_page(doc_id, h, status="pending", retries=0, err="")
    return run(doc_id, ch, quiet=True, only=[h], allow_escalate=False)


def _vl_failed(err: str) -> bool:
    """vl 通道的失败是不是「能靠换模型救回来」的那一类。

    三种都算：图页当表格拆（漏识 / 拆格崩）、模型卡死后反复输出同一片段。
    真正的空页/透印页（本来就该没字）不在其中。
    """
    e = err or ""
    return (e.startswith("表格页仅") or e.startswith("拆出")
            or e.startswith("疑似重复生成"))


def fallback_text(doc_id: str, quiet: bool = False, only_vl: bool = True) -> dict:
    """对 vl 判定失败的页，用 GLM 以纯文本方式重读，值得就用它替换。

    为什么需要这一步：世系图（吊线图）、服制图这类版面，字是图内的小字标注，
    VL 会把整页当表格拆，结果一边是「2730 个空格子」，一边是「只吐出 3 个字」。
    GLM 不按表格理解，一行行列字，实测：

        p0131 世系图   VL 8 字   → GLM 78 字
        p0205 族服圖   VL 3 字   → GLM 182 字
        p0164 版心页   VL 6 字（臆造"官门陞印宗言"）→ GLM 11 字（某氏宗訓 卷一 某堂）

    代价是丢掉表格结构。对图页无所谓，对真表格页才要紧 ——
    所以只在 vl 判定失败时才回退，状态仍留 review 让人确认。
    """
    store.init()
    cand = [p for p in store.queue(doc_id, "review", limit=10 ** 9)
            if (not only_vl or p["channel"] == "vl") and _vl_failed(p.get("err"))]
    if not cand:
        if not quiet:
            print("没有需要回退重读的页")
        return {"pages": 0, "replaced": 0, "sec": 0.0}

    from .legacy import effective_len, text_len

    eng = get_engine("glm")
    st = {"pages": 0, "replaced": 0, "sec": 0.0}
    t_all = time.time()
    try:
        for i, pg in enumerate(cand, 1):
            h, img = pg["hash"], Path(pg["src_path"])
            if not img.exists():
                continue
            st["pages"] += 1
            old_path = Path(pg["out_path"]) if pg["out_path"] else None
            old_txt = (old_path.read_text(encoding="utf-8")
                       if old_path and old_path.exists() else "")
            try:
                rec = eng.recognize(img, hint="image")
            except Exception as e:
                if not quiet:
                    print(f"  [{i}/{len(cand)}] {img.name}  GLM 回退失败：{e}")
                continue

            n_old, n_new = effective_len(old_txt), text_len(rec.text)
            # 走到这里的页，vl 已经判定失败（漏识 / 拆格崩），原样留着只剩空格子和碎格子，
            # 而这些版面本来就不需要表格结构 —— 统一换成 GLM 的纯文本清单。
            # 唯一的门槛是「换了不能更少」：拆格崩的页放宽到 0.8 倍（格子本身占位虚高）。
            #
            # 但「重复生成」是另一回事：旧结果的长度是模型卡死的产物（p0138 vl 吐 3460 字，
            # 全是「襲位二十三年王」），比长度毫无意义。这类页只要求新结果不是退化产物。
            if redundancy_of(old_txt) >= C.DEGEN_REDUNDANCY:
                if redundancy_of(rec.text) >= C.DEGEN_REDUNDANCY:
                    if not quiet:
                        print(f"  [{i}/{len(cand)}] {img.name}  旧结果重复生成，"
                              f"GLM 也一样退化 → 保留原样")
                    continue
                need = 0.0
            else:
                broken = (pg.get("err") or "").startswith("拆出")
                need = C.FALLBACK_MIN_KEEP if broken else C.FALLBACK_MIN_KEEP_LEAK
            if n_new < n_old * need:
                if not quiet:
                    print(f"  [{i}/{len(cand)}] {img.name}  VL {n_old} 字 / GLM {n_new} 字"
                          f"  → 保留 VL 结果")
                continue

            op = out_path(h)
            op.parent.mkdir(parents=True, exist_ok=True)
            # 旧输出留一份 .vl.md：换掉之后原始 vl 结果就没了，
            # 而「哪几段是 vl 读到、换文本后丢掉的」只能靠它算出来。
            if old_txt:
                bak = op.with_suffix(".vl.md")
                if not bak.exists():
                    bak.write_text(old_txt, encoding="utf-8")
            lost = vl_only_fragments(old_txt, rec.text or "")
            hint = ""
            if lost:
                shown = " / ".join(lost[:8])
                more = f" 等 {len(lost)} 段" if len(lost) > 8 else ""
                hint = f"；VL 另读到但未并入：{shown}{more}"
            op.write_text((rec.text or "").rstrip() + "\n", encoding="utf-8")
            store.set_page(doc_id, h, status=REVIEW, engine=rec.engine, chars=n_new,
                           layout="text", sec=round(rec.sec, 2), out_path=str(op),
                           score=0.5,
                           err=f"已改用 GLM 纯文本清单（{n_old}→{n_new} 字，"
                               f"表格结构未保留）{hint}；原判：{pg.get('err') or ''}")
            st["replaced"] += 1
            if not quiet:
                print(f"  [{i}/{len(cand)}] {img.name}  VL {n_old} 字 / GLM {n_new} 字"
                      f"  → 已替换为纯文本" + (f"（漏 {len(lost)} 段）" if lost else ""))
    finally:
        eng.close()
        st["sec"] = round(time.time() - t_all, 1)
    return st


def _quarantine(doc_id: str, page_no: int, data: bytes) -> Path:
    """清空一页正文前，把原文存一份到 data/_quarantine/<文档号>/。

    文件名按 pNNN.md 起，重名就往后编号。为什么必须这样：同一页可能被清两次
    （先被透印规则清空、后来人工又判一次空白），直接覆盖的话前一份备份就没了，
    「删错了能还原」这句承诺也就不成立。
    """
    q = C.DATA / "_quarantine" / doc_id
    q.mkdir(parents=True, exist_ok=True)
    p = q / f"p{page_no:03d}.md"
    i = 1
    while p.exists():
        p = q / f"p{page_no:03d}.{i}.md"
        i += 1
    p.write_bytes(data)
    return p


def batch_action(doc_id: str, hashes, action: str) -> dict:
    """界面上一次勾多页要做的事：批量核对 / 撤销核对 / 判为空白页 / 转 VL。

    「判为空白」的落库口径跟 bleed_scan 完全一致（status=done、chars=0、
    err 写明原因），区别只在判据 —— 这里是人工看过图定的，不是规则算的。
    原文照旧备份到 data/_quarantine，误点一页也能捞回来。

    「转 VL」（2026-11-06 用户要的）：待人工里那类 GLM 根本读不了的页（目录点线
    死循环、图版当表格拆），勾一批直接排进 VL 队列，不用一页页去命令行。
    """
    if action not in ("verify", "unverify", "blank", "to_vl"):
        raise ValueError(f"未知批量操作：{action}")
    if action == "to_vl":
        n = store.to_channel(doc_id, hashes, "vl", why="工作台批量转 VL")
        gone = [h for h in hashes if not store.get_page(doc_id, h)]
        return {"done": n, "skipped": gone, "channel": "vl"}
    ok, skipped = 0, []
    for h in hashes:
        pg = store.get_page(doc_id, h)
        if not pg:
            skipped.append(h)
            continue
        if action == "verify":
            store.verify(doc_id, h, "ok", "批量核对")
        elif action == "unverify":
            store.unverify(doc_id, h)
        else:
            p = pg.get("out_path")
            if p and Path(p).exists() and Path(p).stat().st_size > 0:
                _quarantine(doc_id, pg["page_no"], Path(p).read_bytes())
            if p:
                Path(p).write_text("", encoding="utf-8")
            store.set_page(doc_id, h, status="done", chars=0, cover=None,
                           err="空白页（人工判定）")
            store.verify(doc_id, h, "ok", "人工判为空白页")
        ok += 1
    return {"done": ok, "skipped": skipped}


# 人工改过的正文直接落盘，同时记一条 edited 核对记录
def _undo_dir(doc_id: str, h: str) -> Path:
    return C.DATA / "_undo" / doc_id / h


# 一页留几份历史：够回退到「改坏之前」就行，留太多只会占盘
UNDO_KEEP = 20


def _undo_snapshot(doc_id: str, h: str, data: bytes) -> Path | None:
    """覆盖一页正文之前，把旧内容存一份到 data/_undo/<文档号>/<hash>/。

    为什么必须有：save_text 是**直接覆盖** out_path，一次改错就再也回不去
    （旧版连 .bak 都没有，只有透印清理那条路会备份）。文件名用时间戳，
    同一秒内连改两次就往后编号 —— 覆盖式备份等于没备份。
    """
    if not data:
        return None
    d = _undo_dir(doc_id, h)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p = d / f"{stamp}.md"
    i = 1
    while p.exists():
        p = d / f"{stamp}-{i}.md"
        i += 1
    p.write_bytes(data)
    # 只留最近若干份，老的删掉（按文件名排序即时间序）
    for old in sorted(d.glob("*.md"))[:-UNDO_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return p


def undo_history(doc_id: str, h: str) -> list:
    """这一页的历史版本，新的在前。"""
    from .legacy import render_text, text_len
    d = _undo_dir(doc_id, h)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md"), reverse=True):
        try:
            t = p.read_text(encoding="utf-8")
        except OSError:
            continue
        body = t.strip()
        # 预览要给人看：库里存的是 markdown/HTML，直接截前 120 字会看到
        # `<table border=1 style='margin: auto; ...'>`，等于没预览
        flat = re.sub(r"\s+", " ", render_text(
            t, "flat", vertical=C.is_vertical(doc_id))).strip()
        if not flat:
            # 渲染出来是空的（残缺的 HTML 片段、只有表格标签）——退一步去掉标签再截，
            # 总比给用户看一行空白强
            flat = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", t)).strip()
        out.append({
            "name": p.name,
            "size": p.stat().st_size,
            "chars": text_len(body),
            "head": flat[:120],
        })
    return out


def undo_restore(doc_id: str, h: str, name: str) -> int:
    """把某一页恢复成历史版本。恢复前同样先快照当前内容 —— 恢复也能后悔。"""
    from .legacy import text_len
    # 只认目录里真实存在的文件名，挡掉 ../ 之类的路径穿越
    if not re.fullmatch(r"[\w\-]+\.md", name or ""):
        raise ValueError(f"历史文件名不合法：{name}")
    src = _undo_dir(doc_id, h) / name
    if not src.is_file():
        raise ValueError(f"找不到这份历史：{name}")
    pg = store.get_page(doc_id, h)
    if not pg:
        raise KeyError(f"没有这一页：{h}")

    op = Path(pg["out_path"]) if pg.get("out_path") else C.PAGES / h[:C.SHARD] / f"{h}.md"
    if op.exists():
        _undo_snapshot(doc_id, h, op.read_bytes())
    text = src.read_text(encoding="utf-8").strip()
    op.parent.mkdir(parents=True, exist_ok=True)
    op.write_text(text + "\n", encoding="utf-8")
    n = text_len(text)
    store.set_page(doc_id, h, out_path=str(op), chars=n)
    store.verify(doc_id, h, "edited", f"从历史恢复（{name}）")
    return n


def save_text(doc_id: str, h: str, text: str) -> int:
    pg = store.get_page(doc_id, h)
    if not pg:
        raise KeyError(f"没有这一页：{h}")
    text = (text or "").replace("\r\n", "\n").strip()
    op = C.PAGES / h[:C.SHARD] / f"{h}.md"
    op.parent.mkdir(parents=True, exist_ok=True)
    # 覆盖前留底：这是唯一能「撤回上一次修改」的东西
    if op.exists():
        try:
            _undo_snapshot(doc_id, h, op.read_bytes())
        except OSError:
            pass
    op.write_text(text + "\n", encoding="utf-8")
    from .legacy import text_len
    n = text_len(text)                 # 表格页的标签不算字
    store.set_page(doc_id, h, out_path=str(op), chars=n)
    store.verify(doc_id, h, "edited", f"人工修改，{n} 字")
    return n


# ---------- 报表 ----------
def report(doc_id: str):
    s = store.stats(doc_id)
    print(f"文档 {doc_id}   共 {s['total']} 页   已核对 {s['verified']} 页")
    if not s["groups"]:
        return
    print(f"\n  {'状态':<8}{'通道':<8}{'版面':<8}{'页数':>6}{'平均耗时':>10}{'总字数':>10}")
    for g in s["groups"]:
        print(f"  {g['status'] or '-':<8}{g['channel'] or '-':<8}{g['layout'] or '-':<8}"
              f"{g['n']:>6}{(g['sec'] or 0):>9.1f}s{(g['chars'] or 0):>10}")

    # 按已处理页的平均耗时对比基准，避免「只跑了 5 页却报 900 倍」这种误导
    done = [g for g in s["groups"] if g["status"] in ("done", "review") and g["sec"]]
    n_done = sum(g["n"] for g in done)
    if n_done:
        avg = sum(g["sec"] * g["n"] for g in done) / n_done
        print(f"\n  已处理 {n_done} 页，平均 {avg:.1f} 秒/页"
              f"（全量 PaddleOCR-VL 基准 35.2 秒/页 → {35.2/avg:.1f}x）")
    # 未处理要按「有没有判定结论」算，不能按「有没有耗时」算：
    # 人工通道的页不调模型、sec 恒为 0，按耗时算会虚报成未处理。
    judged = sum(g["n"] for g in s["groups"] if g["status"] in ("done", "review"))
    if s["total"] > judged:
        print(f"  未处理 {s['total'] - judged} 页")


def layout_check(doc_id: str):
    """把结构闸门和已验证清单一页一页对一遍 —— 用于评估分类器，不参与生产。

    对每一页：读已落盘的正文 → 跑结构闸门 → 和清单里的 kind 比。
    清单里没有的页不参与（没有真值）。
    """
    from .router import structure_of
    store.init()
    known = KnownLayout([C.DATA / f for f in C.TRUTH_FILES])

    pages = store.queue(doc_id, "all", limit=10 ** 9)
    hit = {"table": [0, 0], "text": [0, 0]}
    fp_ids, fn_ids, skipped = [], [], 0
    for pg in pages:
        key = Path(pg["src_path"]).stem
        truth = known.map.get(key)
        if truth not in hit:
            continue
        p = pg.get("out_path")
        txt = Path(p).read_text(encoding="utf-8") if p and Path(p).exists() else ""
        if not txt.strip():
            skipped += 1          # 还没跑过的页不能参与评估，否则召回会被算成 0
            continue
        got = structure_of(txt)
        hit[truth][1] += 1
        if got.structured:
            hit[truth][0] += 1
        if truth == "table" and not got.structured:
            fn_ids.append((key, got.brief()))
        if truth == "text" and got.structured:
            fp_ids.append((key, got.brief()))

    n_eval = sum(v[1] for v in hit.values())
    print(f"页数 {len(pages)}，有真值 {n_eval} 页"
          + (f"（{skipped} 页还没有正文，跳过）" if skipped else ""))
    if not n_eval:
        print("  ⚠ 没有可评估的页 —— 先跑一轮识别再来看")
        return hit
    print("结构闸门召回：")
    for k, (a, t) in hit.items():
        if t:
            print(f"  {k:<6} 判为分格 {a}/{t} = {a/t*100:5.1f}%")
    tp, fp = hit["table"][0], hit["text"][0]
    print(f"  精确率 {tp/max(1,tp+fp)*100:.1f}%")
    if fn_ids:
        print(f"\n漏检 {len(fn_ids)} 页：" + ", ".join(f"{k}({b})" for k, b in sorted(fn_ids)))
    if fp_ids:
        print(f"\n误报 {len(fp_ids)} 页：" + ", ".join(f"{k}({b})" for k, b in sorted(fp_ids)))
        print("  ⚠ 先人工看图 —— 2026-10-30 核对过 10 个「误报」，9 个其实是"
              "世系行传/目录/服制图，是清单标签错了。")
    return hit


def _page_text(pg) -> str:
    p = pg.get("out_path")
    if p and Path(p).exists():
        try:
            return Path(p).read_text(encoding="utf-8")
        except OSError:
            return ""
    return ""


# 平铺文本缓存：搜索要逐页读文件，316 页每次重读约 100ms，够用但不该反复算。
# key 用 hash、值里带 mtime —— 页面被重跑/改动后自动失效。
_FLAT_CACHE: dict = {}


def page_flat(pg) -> str:
    """这一页给人看的纯文本（表格摊平、去 markdown 记号）。

    搜索和「上一页末尾/下一页开头」都用它：正文原文里的 <td style=...> 既搜不到
    也读不了，赔率比搜原文大得多。
    """
    from .legacy import render_text
    p = pg.get("out_path")
    if not p:
        return ""
    path = Path(p)
    try:
        mt = path.stat().st_mtime
    except OSError:
        return ""
    key = pg.get("hash") or str(path)
    hit = _FLAT_CACHE.get(key)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    t = render_text(raw, "flat", vertical=C.is_vertical(pg.get("doc_id")))
    if len(_FLAT_CACHE) > 6000:          # 换过的书太多就把缓存丢掉重来
        _FLAT_CACHE.clear()
    _FLAT_CACHE[key] = (mt, t)
    return t


def _squash_ws(s: str) -> str:
    return re.sub(r"[\s\u3000]+", " ", s or "").strip()


def search_text(doc_id: str, q: str, limit: int = 200) -> dict:
    """全书正文搜索：返回命中的页 + 每页第一处命中的上下文片段。

    为什么不走 SQL：正文在 md 文件里，库中只有字数。几百页全读一遍是百毫秒级，
    换来的能力是「这句话在第几页」——以前只能一页页点。
    多个关键词用空格分开，要求全部命中（AND），符合「我记得几个词」的检索习惯。
    """
    terms = [t for t in re.split(r"[\s\u3000]+", (q or "").strip()) if t]
    if not terms:
        return {"total": 0, "pages": [], "terms": []}

    pages = store.queue(doc_id, "all", limit=10 ** 9)
    ver = store.verified_set(doc_id)
    hits = []
    for pg in pages:
        flat = page_flat(pg)
        if not flat:
            continue
        # 先按原文匹配；宽表会被渲染成「网 具 名 称」（每字之间一个空格），
        # 搜「网具」在原文里永远中不了，所以再来一遍「去掉全部空白」的匹配。
        body = None
        for cand in (flat, re.sub(r"\s+", "", flat)):
            cl = cand.lower()
            if all(t.lower() in cl for t in terms):
                body = cand
                break
        if body is None:
            continue
        low = body.lower()
        first = low.find(terms[0].lower())
        # 片段按原始长度取窗口：上下文用原文算，显示时再压空白
        pos = max(0, first - 24)
        snip = _squash_ws(body[pos:first + 90])
        hits.append({
            "hash": pg["hash"], "page_no": pg["page_no"],
            "src": Path(pg["src_path"]).name,
            "status": pg["status"], "layout": pg["layout"],
            "channel": pg["channel"], "chars": pg["chars"] or 0,
            "err": pg["err"] or "",
            "verified": "ok" if pg["hash"] in ver else "",
            "snip": ("…" if pos > 0 else "") + snip,
            "hits": low.count(terms[0].lower()),
        })
    hits.sort(key=lambda x: (-x["hits"], x["page_no"]))
    return {"total": len(hits), "pages": hits[:limit], "terms": terms}


def page_peek(pg, tail: bool, n: int = 110) -> str:
    """相邻页的一小段文字：上一页取结尾、下一页取开头。"""
    t = _squash_ws(page_flat(pg))
    if not t:
        return ""
    return ("…" + t[-n:]) if tail else (t[:n] + "…")


def bleed_flags(doc_id: str, pages=None) -> dict:
    """全书逐页判「原本空白、只是背面透印」。返回 {页号: True/False}。

    判据在 docplat/bleed.py 里 —— 核心是「这页有没有正印的深墨」，
    与模型读出什么无关（模型对透印页会读成乱码，也会干脆编一段）。
    读图失败或拿不准的页一律给 False：宁可漏删，不能把真正文误当空白。
    影像页不参与判定（返回里没有它们，调用方 get 到 None 即「不是空白」）：
    这页本来就没有正印文字，判成空白会把该出的「影像」占位一起吞掉。
    """
    from . import bleed

    pages = pages or store.queue(doc_id, "all", limit=10 ** 9)
    # 影像页不参与判定：这页本来就该没有正印文字，bleed 的「深墨」判据对它
    # 没有意义。让它参与会把「影像」占位一起吞掉 —— zu 的 p241/p242 就是这么
    # 在导出里消失的（两页人工核过、layout=image，被当成透印空白跳过）。
    rows = [(pg["page_no"], pg["src_path"], _page_text(pg))
            for pg in pages if pg.get("layout") != IMAGE]
    return {pn: (v[0] is True) for pn, v in bleed.scan(rows).items()}


def bleed_scan(doc_id: str, apply: bool = False, include_review: bool = False,
               quiet: bool = False) -> dict:
    """把「本来是空白、只是纸太薄透了背面」的页结清为空白页。

    为什么要单独做这一步：这类页的**图像**上有字（透印），模型会读成乱码
    （`业业业业`、`址 址 址`）甚至编造内容，于是它一路绿灯进正文；
    而它其实是空白页，应该像空白页一样不出。判据见 docplat/bleed.py。

    apply=False 只报告（默认，安全）；apply=True 才清空正文文件、改库，
    原文备份到 data/_quarantine/<文档号>/pNNN.md，删错了能还原。

    只动 status='done' 的页；include_review=True 时连 review/escalated 一起
    （那些页本来就等人看，若图像上没有正印深墨，清掉正好省人工）。
    已人工确认过的页（verify）永远不动。
    """
    from . import bleed

    store.init()
    pages = store.queue(doc_id, "all", limit=10 ** 9)
    pages.sort(key=lambda r: r["page_no"])
    try:
        verified = store.verified_set(doc_id)
    except Exception:
        verified = set()
    # 影像页和 bleed_flags 一样排除：这页本来就没有正印文字，
    # 判成「透印空白」会把该保留的影像页一起清掉。
    rows = [(pg["page_no"], pg["src_path"], _page_text(pg))
            for pg in pages if pg.get("layout") != IMAGE]
    res = bleed.scan(rows)
    todo_ok = {"done"} | ({"review", "escalated"} if include_review else set())

    hits, unsure = [], []
    for pg in pages:
        v, why = res.get(pg["page_no"], (None, ""))
        if v is None:
            unsure.append((pg, why))
            continue
        if not v or pg["status"] not in todo_ok or pg["hash"] in verified:
            continue
        hits.append((pg, why))

    if not quiet:
        print(f"[{doc_id}] 判为透印/空白 {len(hits)} 页，拿不准 {len(unsure)} 页")
        for pg, why in hits[:40]:
            n = len(re.sub(r"\s", "", _page_text(pg)))
            head = re.sub(r"\s", " ", _page_text(pg)).strip()[:30]
            print(f"   p{pg['page_no']:<4d} 原 {n:>5d} 字  {why:<34s} {head!r}")
        if len(hits) > 40:
            print(f"   …另有 {len(hits) - 40} 页")

    if not apply:
        return {"hits": [pg["page_no"] for pg, _ in hits], "applied": 0}

    q = C.DATA / "_quarantine" / doc_id
    q.mkdir(parents=True, exist_ok=True)
    moved = 0
    for pg, why in hits:
        p = pg.get("out_path")
        if p and Path(p).exists() and Path(p).stat().st_size > 0:
            _quarantine(doc_id, pg["page_no"], Path(p).read_bytes())
            moved += 1
        if p:
            Path(p).write_text("", encoding="utf-8")
        store.set_page(doc_id, pg["hash"], status="done", chars=0, cover=None,
                       err=f"空白页（{why}）")
    if not quiet:
        print(f"   已清空 {len(hits)} 页，备份 {moved} 个文件 → {q}")
    return {"hits": [pg["page_no"] for pg, _ in hits], "applied": len(hits)}


def book_of(src_dir) -> tuple:
    """由「导入目录」推出 (丛书名, 卷名)。

    导入时的目录形如  书籍\\某氏宗谱\\某氏宗谱（卷一）\\JPG，
    中间那层 JPG / 图片 / images 只是图片堆放层，不代表书本身，先剥掉。
    这样「导入选了 JPG」和「导入直接选了卷目录」两种习惯得到同一个结果。
    """
    d = Path(str(src_dir).rstrip("\\/")) if src_dir else Path("")
    while d.name and d.name.lower() in C.IMG_DIR_NAMES and d.parent != d:
        d = d.parent
    book = d.name                      # 卷名
    series = (d.parent.name or book)   # 丛书名
    # 单卷书直接摊在「书籍」这种收集根下（源目录就是 书籍\书名\图片）：
    # 上上级只是收集目录、不是丛书名，拿它当外层会白多一层「书籍」。
    # 口径（2026-11-03）：这种情况外层目录用书名自己 —— 书籍导出\书名\书名.txt
    # 按日期分堆的批次目录（书籍\1105\书名\图片）同理，1105 是批次不是丛书（11-05 补）
    if (series.strip().lower() in C.SERIES_ROOT_NAMES
            or C.BATCH_DIR_RE.match(series.strip())):
        series = book
    return series, book


def export_target(src_dir, root=None, ext: str = "") -> Path:
    """交付路径：<根>/<丛书名>/<卷名>.<ext>

    用户要的结果是  书籍导出\\某氏宗谱\\某氏宗谱（卷一）.txt
    即文件名取图片文件夹的上一级，外层文件夹取上上一级。
    """
    root = Path(root) if root else C.EXPORT_ROOT
    series, book = book_of(src_dir)
    if not book:                       # 兜底：没有来源目录
        return root / f"未命名.{ext or C.EXPORT_FORMAT}"
    return root / series / f"{book}.{ext or C.EXPORT_FORMAT}"


def _mkdirs(p) -> Path:
    """建导出目录，失败时把「哪个目录、为什么、怎么修」讲清楚。

    原始错误只有一句 `PermissionError: [WinError 5] 拒绝访问`，看不出是权限
    问题还是路径写错。2026-11-06 水西颂导出失败就是这么来的：桌面目录的 ACL
    里有一条显式的「本账户 RX」排在继承的「完全控制」之前 —— 能读、能遍历，
    就是不能新建子目录，于是每本还没有导出目录的新书都导不出来。
    """
    p = Path(p)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise PermissionError(
            f"建不了导出目录：{p}（{e}）。当前账户在这里需要「完全控制」；"
            f'可以执行 icacls "{p.parent}" /grant "Administrator:(OI)(CI)(F)" 后重试'
        ) from e
    return p



def export(doc_id: str, out_name: str = "", src: str = "", root=None,
           ext: str = "", tables: str = "", report: bool = False) -> Path:
    """合并全书。按 page_no 顺序，只取有正文的页。

    正文文件存的是模型原始输出（含 HTML 表格），导出时统一渲染成可读文本：
    · .txt（默认）→ 一律摊成分行文字，不留 markdown 记号（Notepad 打开即读）
    · .md          → 规整分格表还原成管道表
    见 legacy.render_text。

    两类「没有字」的页按不同方式处理（2026-10-31 用户定的口径）：
    · 影像页 → 出一行「影像」占位。族谱里缺一页是信息，读者得知道这页在。
    · 空白页 → 直接不出。别让「markdown」「恭」这种模型杂音混进正文。
    「有无正文」的门槛是 BLANK_MIN 个字。

    out_name 给了就按它走（绝对路径直接用），否则按 export_target 推。
    report=True 时把「用了多少页、跳了多少页」打到 stdout。
    """
    from .legacy import book_layout_pages, page_mark, render_text

    store.init()
    doc = store.get_doc(doc_id)
    src_dir = src or doc.get("src_dir") or ""

    if out_name:
        dst = Path(out_name)
        if not dst.is_absolute():
            # 只给文件名（`试读.txt`）→ 放在导出目录旁边，方便翻交付目录时看到。
            # 给了相对路径（`_tmp/exp/x.txt`）→ 按当前目录解析。以前一律拼到
            # 导出目录，结果在桌面每本书下面都建出一个 _tmp/exp（2026-11-04 踩过）。
            dst = (dst.resolve() if dst.parent != Path(".")
                   else export_target(src_dir, root, ext).parent / dst)
    else:
        dst = export_target(src_dir, root, ext)

    suffix = dst.suffix.lower()
    if not tables:
        tables = "auto" if suffix == ".md" else "flat"

    # 先把目录建好再干活：一整本渲染要几秒到几十秒，目录建不出来（桌面权限、
    # 盘没挂上）就该当场报错。以前放在最后，白跑一遍才炸，报错还只有一句
    # 「WinError 5 拒绝访问」，看不出是权限问题（2026-11-06 水西颂导出失败）
    _mkdirs(dst.parent)

    pages = store.queue(doc_id, "all", limit=10 ** 9)
    pages.sort(key=lambda r: r["page_no"])
    # 透印页：这页原本空白，纸太薄把另一面（紧邻页）的字透了过来，
    # 而且左右镜像 —— 模型只能读成乱码，或者干脆编一段。它不算正文。
    # 判据见 bleed.py：看「这页有没有正印的深墨」，不看文本像不像话。
    # 这里只跳过、不改库；要清库跑 `plat.bat bleed --doc ... --apply`。
    ink_blank = bleed_flags(doc_id, pages)
    buf, used, empty, img, chars = [], 0, 0, 0, 0

    def _push(pg, text: str):
        """一页正文进缓冲：前面加一行页码（全局规则，见 config.PAGE_MARKS）。

        形态 【第11页 / Image00011】 —— 页码对原书，文件名对图库。
        """
        nonlocal chars
        chars += len(re.sub(r"\s", "", text))
        mark = ""
        if C.PAGE_MARKS:
            src = pg.get("src_path") or ""
            mark = page_mark(pg["page_no"], Path(src).stem if src else "")
        buf.append(f"{mark}\n{text}" if mark else text)

    for pg in pages:
        if ink_blank.get(pg["page_no"]):
            empty += 1
            continue
        raw, t = "", ""
        p = pg.get("out_path")
        if p and Path(p).exists():
            raw = Path(p).read_text(encoding="utf-8")
            t = render_text(raw, tables, vertical=C.is_vertical(doc_id)).strip()
        bare = len(re.sub(r"\s", "", t))

        # 影像页（整页就是照片/拓片/插图，本来没有字）留一行「影像」：
        # 族谱里缺一页是信息，读者得知道这页在里面、只是没有文字可录。
        # 两种情况都算影像页：① 版面闸门判成 image；② 模型只回了一个 <img>
        # 引用（把图里某块框出来、没有文字）—— 后者 layout 会被判成 table/text，
        # 光看版面认不出来，但正文里除了 <img> 什么都没有。
        is_img = pg.get("layout") == IMAGE or (bare < BLANK_MIN and "<img" in raw.lower())
        if is_img:
            # 影像页偶尔会带回一段文字：是正文就跟着留（一个字都别丢），
            # 但模型对模糊影像常回一句英文 _"too blurry to recognize"_ ——
            # 那是它在解释自己没读，不是族谱内容，别往正文里塞。
            keep = bare >= TITLE_MIN and re.search(r"[\u4e00-\u9fff]", t)
            _push(pg, "影像" + ("\n" + t if keep else ""))
            used += 1
            img += 1
            continue

        # 空白页直接不出：模型对空白/残页只会吐一个 markdown、或一两个字
        # （卷一 p15~p18 就是「恭」「而」「封」「命」），留在正文里纯属噪音。
        # 整页英文「读不出来」的说明同样清掉（卷七 p3 就是一句 too blurry）。
        # 注：原书空白页拍成照片常常是一团灰影（背面透印），跟「真拍糊的正文页」
        # 在图像上分不开，所以这里不按清晰度留占位 —— 上海溧商（上）那 12 页
        # 灰影经用户确认就是空白页（2026-11-03）。
        #
        # 例外（2026-11-04 用户定）：4~9 字的纯中文页要出 —— 那是图版页上的篇名
        # （「名胜与历史」「名人与溧阳」「民俗与特产」）或跨页断句的前半
        # （「天目湖——我们共」）。之前被 BLANK_MIN=10 一并当空白跳过，
        # 正文里只剩图没有题，用户明确要求补回来。
        has_cn = bool(re.search(r"[\u4e00-\u9fff]", t))
        if has_cn and bare >= TITLE_MIN:
            _push(pg, t)
            used += 1
            continue
        if bare < BLANK_MIN or (not has_cn and META_EN.search(t)):
            empty += 1
            continue
        _push(pg, t)
        used += 1

    dst.parent.mkdir(parents=True, exist_ok=True)   # 已在开头建过，这里只是兜底
    # 交付 txt 照纸书排：段首缩进两格、段间不空行（用户 2026-11-04 定）。
    # .md 保留空行/管道表，那是 markdown 的语法，不能按书排。
    body = (book_layout_pages(buf) if tables == "flat"
            else "\n\n".join(buf))
    dst.write_text(body + "\n", encoding="utf-8")
    if report:
        print(f"  合并 {used} 页"
              + (f"（其中影像 {img} 页）" if img else "")
              + (f"，跳过 {empty} 页（空白）" if empty else "")
              + f"，{chars} 字")
    return dst
