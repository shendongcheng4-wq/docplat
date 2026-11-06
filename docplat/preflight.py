"""导出前体检：算清楚「这一导，会缺什么、会漏什么」。

单独一个模块而不是写在 `web/app.py` 里，是为了能脱离 Flask 单测 ——
这段判断错一次，用户就会被莫名弹窗拦一次。

三档口径（2026-11-05 用户抱怨「待人工 0 了还弹提示」之后分的）：

- `miss`：out_path 没有文件，导出时这页直接消失。**这是真风险**，一定要拦。
- `zero`：有文件但 0 字，且**没有任何空白页判定**，也没人核对过。
  说明这页没读出东西又没说清为什么，**要拦**。
- `blank`：有文件、0 字，但 err 里明确写了「空白页…」（墨淡 / 透印 / 人工判空白）。
  流程已经给出了结论，再弹窗纯属噪音 —— 只在导出成功后提一句。
  想恢复成「一律拦」就把 `config.PREFLIGHT_WARN_BLANK` 置 True。
"""
from pathlib import Path

from . import config as C

# 判定「这页已被结论为空白」的标记。pipeline 写过的几种措辞都含这两个字：
# 空白页，无正文 / 空白页（墨淡且无字）/ 空白页（背面透印）/
# 透印近空白页，模型幻觉输出已清空 / 空白页（人工判定）
BLANK_MARK = "空白页"


def classify(pages, verified) -> dict:
    """pages: store.queue(...) 的行；verified: 已核对 hash 的集合。"""
    miss, zero, blank = [], [], []
    for pg in pages:
        p = pg.get("out_path")
        # 「会不会缺」只看导出真正读的东西：out_path 有没有文件。
        # 不能按 status 判 —— 等换 VL 的页 status 仍是 pending，但上一轮的
        # 结果还在（商业史料 p226 就是这样：status=pending，文件里 97 字），
        # 按 status 判会把它误报成「导出会缺」。
        if not p or not Path(p).exists():
            miss.append(pg["page_no"])
            continue
        if (pg.get("chars") or 0) != 0:
            continue
        if pg.get("hash") in verified:
            continue                       # 人已经看过并接受了
        err = pg.get("err") or ""
        if BLANK_MARK in err:
            blank.append(pg["page_no"])
        else:
            zero.append(pg["page_no"])
    return {"miss": miss, "zero": zero, "blank": blank}


def blocking(verdict: dict) -> bool:
    """要不要弹窗拦住用户。全是「机器判空白」的话不拦。"""
    if verdict["miss"] or verdict["zero"]:
        return True
    if verdict["blank"] and C.PREFLIGHT_WARN_BLANK:
        return True
    return False
