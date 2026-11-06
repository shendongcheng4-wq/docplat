"""SQLite 状态库 —— v2 的地基。

v1 用「磁盘上有没有那个 md 文件」判断是否已完成，由此带来两个问题：
  · 序号命名 p{i:04d}，中途补传一张照片，后面所有页号整体错位，全书跟着乱
  · 想按「待复核 / 失败 / 低置信」筛选时，只能全量扫目录

v2 用内容 hash 做页的身份，page_no 只是展示顺序；一切查询走 SQL。
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config as C

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    src_dir  TEXT,
    created  TEXT NOT NULL,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id   TEXT NOT NULL,
    hash     TEXT NOT NULL,          -- 文件内容 sha1[:16]，页的唯一身份
    page_no  INTEGER NOT NULL,       -- 展示顺序，允许重排
    src_path TEXT NOT NULL,
    width    INTEGER, height INTEGER,
    layout   TEXT,                   -- text / table / image / blank
    status   TEXT NOT NULL,          -- pending/running/done/failed/review
    channel  TEXT,                   -- glm / vl / human
    engine   TEXT,
    cover    REAL,                   -- 内容覆盖率（有基准时）
    score    REAL,                   -- 自评质量分（无基准时）
    chars    INTEGER,
    sec      REAL,
    retries  INTEGER NOT NULL DEFAULT 0,
    err      TEXT,
    out_path TEXT,
    updated  TEXT,
    PRIMARY KEY (doc_id, hash)
);

CREATE INDEX IF NOT EXISTS idx_pages_order   ON pages(doc_id, page_no);
CREATE INDEX IF NOT EXISTS idx_pages_status  ON pages(doc_id, status);
CREATE INDEX IF NOT EXISTS idx_pages_channel ON pages(doc_id, channel);

CREATE TABLE IF NOT EXISTS verifications (
    doc_id   TEXT NOT NULL,
    hash     TEXT NOT NULL,
    reviewer TEXT NOT NULL DEFAULT 'local',
    verdict  TEXT NOT NULL,          -- ok / edited / reject
    note     TEXT,
    created  TEXT NOT NULL,
    PRIMARY KEY (doc_id, hash, reviewer)
);

CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   TEXT NOT NULL,
    channel  TEXT,
    started  TEXT NOT NULL,
    finished TEXT,
    pages    INTEGER DEFAULT 0,
    failed   INTEGER DEFAULT 0,
    sec      REAL
);

-- 页级备注：跟「核对记录」分开存。
-- 记在 verifications.note 里会让「写备注」等于「已核对」，把待人工的页
-- 悄悄移出队列；备注应该随时能写，不改变这一页的核对状态。
CREATE TABLE IF NOT EXISTS notes (
    doc_id   TEXT NOT NULL,
    hash     TEXT NOT NULL,
    body     TEXT NOT NULL,
    updated  TEXT NOT NULL,
    PRIMARY KEY (doc_id, hash)
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def conn():
    C.DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(C.DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)


# ---------- docs ----------

def upsert_doc(doc_id: str, name: str, src_dir: str = "", note: str = ""):
    with conn() as c:
        c.execute(
            "INSERT INTO docs(id,name,src_dir,created,note) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, src_dir=excluded.src_dir",
            (doc_id, name, src_dir, now(), note))


def get_doc(doc_id: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else {}


def set_doc_src(doc_id: str, src_dir: str, note: str = ""):
    """补记导入来源目录。

    导出时的文件名和交付目录是从这里推出来的（见 pipeline.export_target）。
    早期入库时 src_dir 指向的是 work/pages 这类工作副本，名字对不上书，
    所以允许事后改成真正的导入目录。
    """
    with conn() as c:
        if note:
            c.execute("UPDATE docs SET src_dir=?, note=? WHERE id=?",
                      (src_dir, note, doc_id))
        else:
            c.execute("UPDATE docs SET src_dir=? WHERE id=?", (src_dir, doc_id))


def list_docs() -> list:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM pages p WHERE p.doc_id=d.id) AS pages "
            "FROM docs d ORDER BY d.created")]


# ---------- pages ----------

def add_page(doc_id: str, h: str, page_no: int, src: Path, w=None, hgt=None):
    """幂等：同一张图重复上传只更新序号，不重置已完成状态。"""
    with conn() as c:
        old = c.execute("SELECT page_no FROM pages WHERE doc_id=? AND hash=?",
                        (doc_id, h)).fetchone()
        if old is None:
            c.execute(
                "INSERT INTO pages(doc_id,hash,page_no,src_path,width,height,"
                "status,retries,updated) VALUES(?,?,?,?,?,?,'pending',0,?)",
                (doc_id, h, page_no, str(src), w, hgt, now()))
        elif old["page_no"] != page_no:
            c.execute("UPDATE pages SET page_no=? WHERE doc_id=? AND hash=?",
                      (page_no, doc_id, h))


def page_hashes(doc_id: str) -> set:
    with conn() as c:
        return {r["hash"] for r in c.execute(
            "SELECT hash FROM pages WHERE doc_id=?", (doc_id,))}


def pending(doc_id: str, channel: str = None, limit: int = 0,
            hashes: list = None) -> list:
    # 只收 pending/failed。**待人工（status='review'）不会自动被批量带走** ——
    # 那页是给人看的，跑批不该自己决定「再换一个模型试试」。要换模型得人显式
    # 动手：工作台「重跑」选 PaddleOCR-VL，或者勾几页按「转 VL」，
    # 走 store.to_channel() 把它放回 pending + channel=vl（2026-11-06 用户要的）。
    sql = ("SELECT * FROM pages WHERE doc_id=? AND status IN ('pending','failed') "
           "AND retries < ?")
    args = [doc_id, C.MAX_RETRY]
    if channel:
        sql += " AND channel=?"
        args.append(channel)
    if hashes:
        sql += f" AND hash IN ({','.join('?' * len(hashes))})"
        args.extend(hashes)
    else:
        # 人工核对过的页不要再跑：升级队列里点过「核对」的页状态仍是 pending
        # （这样取消核对能回到队列），但整批换 Paddle 重跑时必须跳过它，
        # 否则会把人工确认过的正文冲掉。显式指定 hashes（工作台「重跑本页」）
        # 属于例外，那条路就是要强制重读。
        sql += f" AND {_VERIFIED}"
    sql += " ORDER BY page_no"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def set_page(doc_id: str, h: str, **kw):
    if not kw:
        return
    kw["updated"] = now()
    cols = ", ".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE pages SET {cols} WHERE doc_id=? AND hash=?",
                  (*kw.values(), doc_id, h))


def bump_retry(doc_id: str, h: str, err: str = ""):
    with conn() as c:
        c.execute("UPDATE pages SET retries=retries+1, status='failed', err=?, updated=? "
                  "WHERE doc_id=? AND hash=?", (err[:500], now(), doc_id, h))


def to_channel(doc_id: str, hashes, channel: str = "vl", why: str = "") -> int:
    """把选中的页转成「等这个通道重跑」：status=pending、channel=<channel>。

    为什么要这个（2026-11-06 用户要的）：待人工里有一类页是「GLM 就是读不了」
    —— 目录页点线死循环吐出三万字「……」、图版页被当表格拆。人在工作台一眼
    看出该换 PaddleOCR-VL，但原来没有任何入口能把它们塞进 VL 队列：待人工页
    的状态是 review，而批量跑只收 pending/failed，于是只能一页页去命令行。
    现在人勾几页按「转 VL」，就等于替流水线做这个决定。

    注意两件事：
      · 核对记录要一并删掉。待跑查询带 _VERIFIED 过滤（「整批别冲掉人工确认过的
        正文」那道保护），留着记录这页转过去也永远跑不到。
      · 正文不删。新结果写盘前，旧内容还在，中途失败不至于把字弄丢。
    """
    hashes = [h for h in (hashes or []) if h]
    if not hashes:
        return 0
    n = 0
    with conn() as c:
        for h in hashes:
            r = c.execute("SELECT status, channel, err FROM pages "
                          "WHERE doc_id=? AND hash=?", (doc_id, h)).fetchone()
            if not r:
                continue
            old = (r["err"] or "").replace("\n", " ")[:120]
            err = f"人工转{channel}重跑" + (f"（原判：{old}）" if old else "")
            if why:
                err = f"人工转{channel}重跑：{why}" + (f"（原判：{old}）" if old else "")
            c.execute("UPDATE pages SET status='pending', channel=?, retries=0, "
                      "err=?, updated=? WHERE doc_id=? AND hash=?",
                      (channel, err[:500], now(), doc_id, h))
            c.execute("DELETE FROM verifications WHERE doc_id=? AND hash=?",
                      (doc_id, h))
            n += 1
    return n


def get_page(doc_id: str, h: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM pages WHERE doc_id=? AND hash=?",
                      (doc_id, h)).fetchone()
        return dict(r) if r else {}


def stats(doc_id: str) -> dict:
    with conn() as c:
        rows = c.execute(
            "SELECT status, channel, layout, COUNT(*) n, "
            "       AVG(sec) sec, SUM(chars) chars "
            "FROM pages WHERE doc_id=? GROUP BY status, channel, layout",
            (doc_id,)).fetchall()
        total = c.execute("SELECT COUNT(*) n FROM pages WHERE doc_id=?",
                          (doc_id,)).fetchone()["n"]
        ver = c.execute("SELECT COUNT(*) n FROM verifications WHERE doc_id=?",
                        (doc_id,)).fetchone()["n"]
    return {"total": total, "verified": ver, "groups": [dict(r) for r in rows]}


# 「核对了没有」不写回 pages.status，而是查 verifications 表。
# 这样取消核对不需要恢复任何状态，天然可逆；多人场景下每人也各自一条记录。

_VERIFIED = ("NOT EXISTS (SELECT 1 FROM verifications v "
             "WHERE v.doc_id=pages.doc_id AND v.hash=pages.hash "
             "AND v.verdict IN ('ok','edited'))")


def _queue_cond(kind: str) -> str:
    return {
        # 待人工 = 闸门判 review，且还没有人核对过
        "review": f"status='review' AND {_VERIFIED}",
        "failed": "status='failed'",
        # 还没跑的（含上次中断卡在 running 的）。要排除等着换 VL 的页 ——
        # 那些页在 glm 阶段已经跑过了，算进来会让「待跑」和「待VL」重复。
        "pending": ("status IN ('pending','running')"
                    " AND (channel IS NULL OR channel<>'vl')"),
        # 升级 = 结构闸门判为分格条目、等着换 PaddleOCR-VL 重跑的页。
        # 已核对过的要退出：人在升级队列里点「核对」，意思就是「这页 GLM 的结果
        # 我认了，不用再换 Paddle」—— 还挂着就是队列只增不减（2026-11-05 用户问
        # 「点了核对怎么还在升级里」。取消核对会自然回到队列，因为这里查的是
        # verifications 表，没有改 pages.status）。
        "escalated": f"channel='vl' AND status='pending' AND {_VERIFIED}",
        # 低分 = 过了闸门但分不高（自评分 < 1），抽查看这些。
        # 已人工核验过的要退出——否则「低分」清单只增不减，人就不看了。
        "lowscore": f"status='done' AND COALESCE(score,1) < 1 AND {_VERIFIED}",
        # 抽检 = 已识别且没人看过的页里随机取 —— 几十万页时唯一的现实兜底。
        # 不是「抽查有问题的那几页」，而是「随机看几页，验证闸门本身没漏」。
        "sample": ("status='done' AND NOT EXISTS ("
                   "SELECT 1 FROM verifications v WHERE v.doc_id=pages.doc_id "
                   "AND v.hash=pages.hash)"),
        "done": "status='done'",
        "all": "1=1",
    }[kind]


def _queue_filter(kind: str, q: str = ""):
    cond, args = _queue_cond(kind), []
    if q:
        cond += " AND (src_path LIKE ? OR CAST(page_no AS TEXT)=?)"
        args = [f"%{q}%", q.lstrip("p0") or q]
    return cond, args


def queue(doc_id: str, kind: str, limit: int = 50, offset: int = 0,
          q: str = "") -> list:
    """人工队列 —— 前端要的就是这个，而不是全部页。

    kind: review=待人工 / failed=失败 / lowscore=低分 / sample=随机抽检 / done / all
    """
    cond, extra = _queue_filter(kind, q)
    if kind == "sample":
        order = "RANDOM()"
    elif kind == "review":
        # 待复核按「模型最可能读错」排前面，而不是按页码：
        # vl 通道的表格页（图内小字漏识、拆格失败）风险最高，
        # 其次是 glm 的小字页（多数只是题签大字，一眼就能确认），
        # 最后是影像页（模型完全没读，要人工逐字录）。
        order = ("CASE channel WHEN 'vl' THEN 0 WHEN 'glm' THEN 1 ELSE 2 END, page_no")
    else:
        order = "page_no"
    with conn() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM pages WHERE doc_id=? AND {cond} "
            f"ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}",
            [doc_id, *extra])]


def queue_count(doc_id: str, kind: str, q: str = "") -> int:
    cond, extra = _queue_filter(kind, q)
    with conn() as c:
        return c.execute(f"SELECT COUNT(*) n FROM pages WHERE doc_id=? AND {cond}",
                         [doc_id, *extra]).fetchone()["n"]


def queue_counts(doc_id: str) -> dict:
    return {k: queue_count(doc_id, k)
            for k in ("review", "escalated", "failed", "lowscore", "sample",
                      "done", "pending", "all")}


# ---------- 核对 ----------

# 升级页（channel=vl、status=pending）被人工核对 = 「GLM 的这版结果我认了，不用再换
# Paddle」。这句话要落到 pages.status 上，否则那页永远停在 pending：进度条永远差
# 这几页跑不满，队列里又来回来去（2026-11-05 用户问「点了核对怎么还在升级里」）。
# 落成 done 之后还能撤销 —— 理由原文存在 err 里，撤销时按前缀还原。
VERIFIED_ACCEPT = "人工核对：采用当前结果，不再换 Paddle"


def verify(doc_id: str, h: str, verdict: str, note: str = "",
           reviewer: str = C.REVIEWER):
    """记录人工核对结论。verdict: ok=原样确认 / edited=改过正文。

    普通页不改 pages.status —— 「已核对」只是加一条记录，取消核对时删掉即可，
    页面自动回到原队列。**升级页是例外**：核对意味着放弃换 Paddle，直接把
    这页收成 done（见 VERIFIED_ACCEPT），否则它会永远挂在升级队列里。
    """
    with conn() as c:
        c.execute(
            "INSERT INTO verifications(doc_id,hash,reviewer,verdict,note,created) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(doc_id,hash,reviewer) DO UPDATE SET "
            "verdict=excluded.verdict, note=excluded.note, created=excluded.created",
            (doc_id, h, reviewer, verdict, note, now()))
        pg = c.execute("SELECT status, channel, err FROM pages "
                       "WHERE doc_id=? AND hash=?", (doc_id, h)).fetchone()

    if pg and pg["status"] == "pending" and pg["channel"] == "vl":
        old = (pg["err"] or "").strip()
        set_page(doc_id, h, status="done",
                 err=VERIFIED_ACCEPT + (f"（原判：{old}）" if old else ""))


def unverify(doc_id: str, h: str, reviewer: str = C.REVIEWER):
    with conn() as c:
        c.execute("DELETE FROM verifications WHERE doc_id=? AND hash=? AND reviewer=?",
                  (doc_id, h, reviewer))
        pg = c.execute("SELECT status, channel, err FROM pages "
                       "WHERE doc_id=? AND hash=?", (doc_id, h)).fetchone()

    # 撤销「升级页的核对」→ 放回升级队列，并把被覆盖的原始判定理由还原
    if (pg and pg["status"] == "done" and (pg["err"] or "").startswith(VERIFIED_ACCEPT)):
        old = (pg["err"] or "")[len(VERIFIED_ACCEPT):].strip()
        if old.startswith("（原判：") and old.endswith("）"):
            old = old[len("（原判："):-1]
        else:
            old = ""
        set_page(doc_id, h, status="pending", err=old)


def verification_of(doc_id: str, h: str, reviewer: str = C.REVIEWER) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM verifications WHERE doc_id=? AND hash=? AND reviewer=?",
                      (doc_id, h, reviewer)).fetchone()
        return dict(r) if r else {}


def verified_set(doc_id: str) -> set:
    with conn() as c:
        return {r["hash"] for r in c.execute(
            "SELECT hash FROM verifications WHERE doc_id=?", (doc_id,))}


# ---------- 备注 ----------
# 校对时想到的话（「原图此处模糊」「疑为『戊』」）需要一个去处，
# 但它不能改变这一页的核对状态，也不该进正文。

def set_note(doc_id: str, h: str, body: str):
    body = (body or "").strip()
    with conn() as c:
        if not body:
            c.execute("DELETE FROM notes WHERE doc_id=? AND hash=?", (doc_id, h))
            return
        c.execute(
            "INSERT INTO notes(doc_id,hash,body,updated) VALUES(?,?,?,?) "
            "ON CONFLICT(doc_id,hash) DO UPDATE SET body=excluded.body, "
            "updated=excluded.updated", (doc_id, h, body, now()))


def get_note(doc_id: str, h: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM notes WHERE doc_id=? AND hash=?",
                      (doc_id, h)).fetchone()
        return dict(r) if r else {}


def note_map(doc_id: str) -> dict:
    """整卷的备注，一次查完 —— 队列每行要标一个小点，不能一行一次查询。"""
    with conn() as c:
        return {r["hash"]: r["body"] for r in c.execute(
            "SELECT hash, body FROM notes WHERE doc_id=?", (doc_id,))}


# ---------- 相邻页 ----------
# 跨页接续的句子（族谱、扫描歪页）要对照上一页末尾，所以右栏要能
# 直接取出「前一页 / 后一页」是哪一页。按 page_no 查而不是按列表下标：
# 队列里当前这批页是不连续的，下标做邻居会跳错。

def neighbor(doc_id: str, page_no: int, delta: int) -> dict:
    op = "<" if delta < 0 else ">"
    order = "DESC" if delta < 0 else "ASC"
    with conn() as c:
        r = c.execute(
            f"SELECT * FROM pages WHERE doc_id=? AND page_no {op} ? "
            f"ORDER BY page_no {order} LIMIT 1", (doc_id, page_no)).fetchone()
        return dict(r) if r else {}
