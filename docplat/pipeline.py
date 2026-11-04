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
from . import store
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
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.size
    except Exception:
        return (None, None)


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


def run(doc_id: str, channel: str = "", limit: int = 0, quiet: bool = False,
        stop_on_engine_error: bool = True, only: list = None,
        engines: dict = None) -> dict:
    """跑待处理页。

    channel 留空 = 自动路由（glm/vl/human 都走）；指定则只跑该通道。
    human 通道不调模型，直接标 needs_review，等人来看。
    only 指定 hash 列表时只跑这几页（工作台的「重跑本页」用）。
    engines 传入一个字典即可跨调用复用引擎、且不由本次调用关闭 —— 批量跑多卷的
    vl 阶段要用它：Paddle 加载一次一分多钟，每卷重载一遍纯属浪费。
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
        return {"pages": 0, "done": 0, "review": 0, "failed": 0, "sec": 0}

    clf = Classifier(KnownLayout([C.DATA / f for f in C.TRUTH_FILES]))
    shared = engines is not None
    engines = engines if engines is not None else {}
    stat = {"pages": 0, "done": 0, "review": 0, "failed": 0, "escalate": 0, "sec": 0.0}
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
        new_ch, why = clf.escalate(rec.text, layout, ch)
        if new_ch:
            store.set_page(doc_id, h, layout=TABLE, channel=new_ch, status="pending",
                           engine=rec.engine, chars=rec.chars,
                           sec=round(rec.sec, 2), out_path=str(op),
                           err=f"结构闸门升级：{why}")
            stat["escalate"] += 1
            if not quiet:
                print(f"  [{i}/{len(pages)}] {img.name}  glm {rec.sec:5.1f}s "
                      f"{rec.chars:>4}字  ⤴ 升级 {new_ch}：{why}")
            continue

        store.set_page(doc_id, h, status=v.status, engine=rec.engine,
                       chars=0 if is_blank else rec.chars, sec=round(rec.sec, 2),
                       cover=v.score if v.status == DONE else None,
                       score=v.score, out_path=str(op), err=v.reason)
        stat["done" if v.status == DONE else "review"] += 1
        if not quiet:
            flag = "" if v.status == DONE else "  ⚠"
            print(f"  [{i}/{len(pages)}] {img.name}  {layout:<5} {rec.engine:<4} "
                  f"{rec.sec:5.1f}s {rec.chars:>4}字  {v.reason}{flag}")

    stat["sec"] = round(time.time() - t_all, 1)
    if not shared:                      # 外部传进来的引擎由外部负责关
        for e in engines.values():
            e.close()
    if stat["escalate"] and not quiet:
        print(f"\n  {stat['escalate']} 页被结构闸门升级到 {C.ESCALATE_TO} 通道。"
              f"先停掉 GLM 服务（显存要让出来），再跑："
              f"python -m docplat run --doc {doc_id} --channel {C.ESCALATE_TO}")
    # vl 跑完要知道还有一步：图页被拆成空壳/漏识的，可以用 GLM 列成纯文本清单
    if channel == C.ESCALATE_TO and stat["review"] and not quiet:
        print(f"  {stat['review']} 页待复核。其中「表格页仅 N 字 / 拆格失败」的图页，"
              f"可以换纯文本重读：python -m docplat retext --doc {doc_id}")
    return stat


def rerun_one(doc_id: str, h: str) -> dict:
    """重跑单页。工作台「重跑本页」用。

    先把状态重置为 pending（清空重试计数），否则 pending() 查不到它。
    原正文保留到新结果写盘为止 —— 中途失败不至于把已有内容弄丢。
    """
    pg = store.get_page(doc_id, h)
    if not pg:
        raise KeyError(f"没有这一页：{h}")
    store.set_page(doc_id, h, status="pending", retries=0, err="")
    return run(doc_id, quiet=True, only=[h])


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


# 人工改过的正文直接落盘，同时记一条 edited 核对记录
def save_text(doc_id: str, h: str, text: str) -> int:
    pg = store.get_page(doc_id, h)
    if not pg:
        raise KeyError(f"没有这一页：{h}")
    text = (text or "").replace("\r\n", "\n").strip()
    op = C.PAGES / h[:C.SHARD] / f"{h}.md"
    op.parent.mkdir(parents=True, exist_ok=True)
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
    if series.strip().lower() in C.SERIES_ROOT_NAMES:
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
    from .legacy import render_text

    store.init()
    doc = store.get_doc(doc_id)
    src_dir = src or doc.get("src_dir") or ""

    if out_name:
        dst = Path(out_name)
        if not dst.is_absolute():
            dst = export_target(src_dir, root, ext).parent / out_name
    else:
        dst = export_target(src_dir, root, ext)

    suffix = dst.suffix.lower()
    if not tables:
        tables = "auto" if suffix == ".md" else "flat"

    pages = store.queue(doc_id, "all", limit=10 ** 9)
    pages.sort(key=lambda r: r["page_no"])
    buf, used, empty, img = [], 0, 0, 0
    for pg in pages:
        raw, t = "", ""
        p = pg.get("out_path")
        if p and Path(p).exists():
            raw = Path(p).read_text(encoding="utf-8")
            t = render_text(raw, tables).strip()
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
            buf.append("影像" + ("\n" + t if keep else ""))
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
            buf.append(t)
            used += 1
            continue
        if bare < BLANK_MIN or (not has_cn and META_EN.search(t)):
            empty += 1
            continue
        buf.append(t)
        used += 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n\n".join(buf) + "\n", encoding="utf-8")
    if report:
        print(f"  合并 {used} 页"
              + (f"（其中影像 {img} 页）" if img else "")
              + (f"，跳过 {empty} 页（空白）" if empty else "")
              + f"，{len(''.join(buf))} 字")
    return dst
