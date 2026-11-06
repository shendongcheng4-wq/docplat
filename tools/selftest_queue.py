"""核对 / 升级队列的自检（队列语义，跑在临时库上，不碰真实数据）。

要挡住的回归（2026-11-05 用户报的：「在升级里面点了核对，还是会出现在升级里面」）：

1. 升级页（channel=vl、status=pending）被核对后必须离开升级队列 ——
   人点「核对」的意思就是「这页 GLM 的结果我认了，不用再换 Paddle」。
   同时 status 要落成 done，否则进度条永远差这几页跑不满。
2. 撤销核对要能原样回去：status 回 pending、升级队列里重新出现、
   被覆盖的「原判」理由要还原。
3. 整批换 Paddle 重跑时不能把已核对过的正文冲掉，但工作台「重跑本页」
   （显式指定 hash）必须还能取到 —— 那是强制重读。
4. 普通待人工页、已识别页的核对行为不许改坏。
5. **待人工页不会被批量跑顺手带走**，但人可以显式把它转成 VL 重跑
   （2026-11-06 用户要的「待人工的能批量加进 vl，重跑的话要换模型」）：
   整批跑只收 pending/failed；`store.to_channel()` 才把页放回 pending+vl，
   并顺手清掉核对记录（否则 _VERIFIED 会把它滤掉，转了等于没转）。
   单页重跑可以点名换通道，但 vl 一律转队列，不在服务进程里加载 Paddle。

跑法：python tools/selftest_queue.py
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docplat import config as C  # noqa: E402

# 指到临时库，绝不能碰 data/docplat.db
TMP = Path(__file__).resolve().parents[1] / "_tmp" / "_queue_test"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True)
C.DATA = TMP
C.DB = TMP / "queue.db"

from docplat import pipeline, store  # noqa: E402

DOC = "测试书"


def _seed():
    store.init()
    with store.conn() as c:
        c.execute("DELETE FROM pages")
        c.execute("DELETE FROM verifications")
        rows = [
            # hash, page_no, status, channel, err
            ("h_esc", 1, "pending", "vl", "结构闸门升级：行长结构异常：19 行"),
            ("h_rev", 2, "review", "glm", "疑似重复生成"),
            ("h_done", 3, "done", "glm", "正常"),
            ("h_esc2", 4, "pending", "vl", "结构闸门升级：列数对不齐"),
            # 待人工 + vl 通道：换不换模型都不该被「换 VL 重跑 N」批量带走
            ("h_revvl", 5, "review", "vl", "表格页仅 3 字，拆格失败"),
        ]
        for h, pn, st, ch, err in rows:
            c.execute(
                "INSERT INTO pages(doc_id,hash,page_no,src_path,status,channel,"
                "engine,chars,score,retries,err,updated) "
                "VALUES(?,?,?,?,?,?,'glm',100,1.0,0,?,'2026-11-05T00:00:00')",
                (DOC, h, pn, f"C:/x/{pn}.jpg", st, ch, err))


def main() -> int:
    stat = {"n": 0, "bad": 0}
    ok = True

    def check(name, got, want):
        nonlocal ok
        stat["n"] += 1
        flag = "✓" if got == want else "✗"
        print(f"  {flag} {name}: {got}（期望 {want}）")
        if got != want:
            ok = False
            stat["bad"] += 1

    def page(h):
        return store.get_page(DOC, h) or {}

    def escalated_pages():
        return sorted(p["page_no"] for p in store.queue(DOC, "escalated", limit=99))

    def review_pages():
        return sorted(p["page_no"] for p in store.queue(DOC, "review", limit=99))

    _seed()
    check("初始升级队列", escalated_pages(), [1, 4])
    check("初始待人工", review_pages(), [2, 5])

    # ---- 1. 升级页核对 ----
    store.verify(DOC, "h_esc", "ok", "看着没问题")
    check("核对后升级队列", escalated_pages(), [4])
    check("核对后页状态", page("h_esc")["status"], "done")
    check("err 记下人工结论", page("h_esc")["err"].startswith(store.VERIFIED_ACCEPT), True)
    check("原判理由保留在 err 里", "结构闸门升级" in page("h_esc")["err"], True)
    check("升级计数", store.queue_count(DOC, "escalated"), 1)
    check("已识别计数", store.queue_count(DOC, "done"), 2)

    # ---- 2. 撤销核对要原样回去 ----
    store.unverify(DOC, "h_esc")
    check("撤销后回升级队列", escalated_pages(), [1, 4])
    check("撤销后状态", page("h_esc")["status"], "pending")
    check("撤销后 err 还原", page("h_esc")["err"], "结构闸门升级：行长结构异常：19 行")

    # ---- 3. 整批重跑跳过已核对，显式重跑能取到 ----
    store.verify(DOC, "h_esc", "ok", "")
    ids = [p["hash"] for p in store.pending(DOC, "vl")]
    check("整批 vl 重跑跳过已核对的", "h_esc" in ids, False)
    check("整批 vl 重跑仍取到没核对的", "h_esc2" in ids, True)
    # 待人工页（哪怕通道就是 vl）不在整批 vl 的取页集合里
    check("整批 vl 不带走待人工页", "h_revvl" in ids, False)
    # 「重跑本页」走 rerun_one：先把状态放回 pending，再按 hash 强制取。
    # 已核对的记录还在（所以整批会跳过它），显式重跑必须照跑 —— 那是用户点名要重读。
    store.set_page(DOC, "h_esc", status="pending")
    forced = [p["hash"] for p in store.pending(DOC, "vl", hashes=["h_esc"])]
    check("显式「重跑本页」仍能取到", forced, ["h_esc"])

    # ---- 4. 普通页的核对行为不变 ----
    store.verify(DOC, "h_rev", "ok", "")
    check("待人工核对后离开队列", review_pages(), [5])
    check("待人工页状态不被改成 done", page("h_rev")["status"], "review")
    store.unverify(DOC, "h_rev")
    check("撤销后回到待人工", review_pages(), [2, 5])

    store.verify(DOC, "h_done", "ok", "")
    check("已识别页核对不改状态", page("h_done")["status"], "done")
    check("已识别页核对不改 err", page("h_done")["err"], "正常")

    # ---- 5. 单页「重跑」可以换模型，但不许被闸门偷偷改通道 ----
    # 替身挂在 pipeline.run 上：这里要验的是 rerun_one 传了什么，不真跑模型。
    seen = {}
    orig_run = pipeline.run

    def fake_run(doc_id, channel="", **kw):
        seen.update(doc_id=doc_id, channel=channel, **kw)
        return {"pages": 0, "done": 0, "review": 1, "failed": 0, "escalate": 0,
                "slow": 0, "sec": 0.0}

    try:
        pipeline.run = fake_run
        # 留空 = 用这一页原来的通道
        pipeline.rerun_one(DOC, "h_rev")
        check("重跑默认用原通道", seen.get("channel"), "glm")
        check("重跑关掉结构闸门", seen.get("allow_escalate"), False)
        check("重跑只跑这一页", seen.get("only"), ["h_rev"])
        # 点名换模型（2026-11-06 用户要的「重跑要能换模型」）
        pipeline.rerun_one(DOC, "h_rev", "glm")
        check("重跑可以点名通道", seen.get("channel"), "glm")
        # vl 不能在服务进程里跑，必须转队列
        try:
            pipeline.rerun_one(DOC, "h_rev", "vl")
            check("vl 不在服务进程里重跑", "没报错", "报错")
        except ValueError as e:
            check("vl 不在服务进程里重跑", "Paddle" in str(e), True)
    finally:
        pipeline.run = orig_run
    check("重跑后通道没被改成 vl", page("h_rev")["channel"], "glm")

    # ---- 6. 待人工可以批量转 VL（2026-11-06 用户要的） ----
    store.set_page(DOC, "h_rev", status="review", channel="glm", err="疑似重复生成")
    n = store.to_channel(DOC, ["h_rev"], "vl", why="测试转 VL")
    check("转 VL 命中页数", n, 1)
    check("转 VL 后通道", page("h_rev")["channel"], "vl")
    check("转 VL 后状态", page("h_rev")["status"], "pending")
    check("转 VL 后离开待人工", 2 in review_pages(), False)
    check("转 VL 后进升级队列", 2 in escalated_pages(), True)
    check("转 VL 后能被 vl 批量取到",
          "h_rev" in [p["hash"] for p in store.pending(DOC, "vl")], True)
    check("err 留了原判可追溯", "原判" in page("h_rev")["err"], True)

    # 已核对过的页也要能转：不然 _VERIFIED 会把它从待跑里滤掉，转了等于没转
    store.to_channel(DOC, ["h_done"], "vl")
    check("已核对页转 VL 后能被取到",
          "h_done" in [p["hash"] for p in store.pending(DOC, "vl")], True)
    check("已核对页转 VL 后核对记录清掉",
          store.verification_of(DOC, "h_done"), {})
    check("批量入口认得 to_vl",
          pipeline.batch_action(DOC, ["h_revvl"], "to_vl")["done"], 1)

    print(f"全部通过（{stat['n']}/{stat['n']}）" if ok
          else f"有失败项（{stat['bad']}/{stat['n']} 未过）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
