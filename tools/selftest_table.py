"""复杂表格 / 空白表单两条判据的自检。

背景（2026-11-05，都是用真实页调出来的）：

1) 先进性教育活动 p192「'抓好整改、巩固提高'阶段工作考核细则」——
   合并单元格 + 竖排表头的考核表。默认提示词下 GLM 读对表头和第一行之后，
   一路吐空单元格：4071 行、4058 个竖线、正文只有开头那几行。换
   `config.GLM_TABLE_PROMPT` 重读，同一张图 6.2 秒吐完整 HTML 表。
   判退化靠 `pipeline._pipe_degenerate`：整行只有竖线/空白的行 ≥ 50。

2) p58 / p132「市机关部门领导干部领导联系点登记表」「党内民主评议表」——
   原图是**没填写的空表单**，格子天然是空的，GLM 连吐 9~13 行 `| | | |`。
   这不是退化，是这张表本来的样子；`quality._empty_grid` 认它，判 DONE，
   不再报「整块重复生成」。

跑法：python tools/selftest_table.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docplat import pipeline  # noqa: E402
from docplat.models.base import Recognition  # noqa: E402
from docplat.router.quality import _empty_grid, evaluate  # noqa: E402

# p192 的真实退化形态：表头读对，之后全是空单元格
DEGEN = ("目录\n内容\n考核内容\n分值\n评分标准\n得分\n\n"
         "营造氛围(10分)\n宣传氛围(5分)\n23\n"
         "各镇（区）活动简报编发正常，每周不少于1期\n2\n少1期扣0.5分\n"
         + "|\n" * 400)

# p192 换提示词后的真实输出（截断）
GOOD_HTML = (
    '<table><thead><tr><th colspan="3">目录</th><th>考核内容</th><th>分值</th></tr></thead>'
    '<tbody><tr><td rowspan="2">营造氛围(10分)</td><td>宣传氛围(5分)</td><td>23</td>'
    '<td>各镇（区）活动简报编发正常，每周不少于1期</td><td>2</td></tr>'
    '<tr><td>24</td><td>各镇（区）根据实际情况，利用有线电视、广播、政务公开栏等进行宣传报道</td>'
    '<td>3</td></tr></tbody></table>')

# p58 的真实形态：空白表单（表头 + 一片空格子）
BLANK_FORM = ("市机关部门领导干部领导联系点登记表\n\n单位名称 ___\n\n"
              "领导姓名 联系镇（区） 联系站所\n"
              + "| | | |\n| :--- | :--- | :--- |\n" + "| | | |\n" * 9 +
              "\n注：本表由各部门认真填写后，于4月16日前报市保持共产党员党员先进性教育活动"
              "领导小组办公室（市委组织部）。\n")

# 真填了数据的表：不能判成空白表单
FILLED = ("推荐常州、溧阳两级先进性教育活动先进集体名额分配表\n\n"
          "单位\t常州市级\t溧阳市级\n溧城镇\t2\t4\n埭头镇\t1\t3\n合计\t25\t50\n\n"
          "| 溧城镇 | 2 | 4 |\n| 埭头镇 | 1 | 3 |\n| 上黄镇 | 1 | 2 |\n"
          "| 戴埠镇 | 2 | 3 |\n| 合计 | 25 | 50 |\n")


def main() -> int:
    ok = True
    stat = {"n": 0, "bad": 0}

    def check(name, got, want):
        nonlocal ok
        stat["n"] += 1
        flag = "✓" if got == want else "✗"
        print(f"  {flag} {name}: {got}（期望 {want}）")
        if got != want:
            ok = False
            stat["bad"] += 1

    # ---- 1. 退化判定 ----
    check("p192 退化文本 → 判退化", pipeline._pipe_degenerate(DEGEN), True)
    check("完整 HTML 表 → 不判退化", pipeline._pipe_degenerate(GOOD_HTML), False)
    check("空白表单（9 行空格子）→ 不判退化",
          pipeline._pipe_degenerate(BLANK_FORM), False)

    # ---- 2. 空白表单 ----
    check("p58 空白表单 → 认作空表", _empty_grid(BLANK_FORM), True)
    check("填了数据的表 → 不认作空表", _empty_grid(FILLED), False)
    check("正常正文 → 不认作空表", _empty_grid("这是一段普通正文，没有表格。\n" * 5), False)
    v = evaluate(Recognition(text=BLANK_FORM, engine="glm", sec=1.0), "text")
    check("空白表单判 done", v.status, "done")
    check("空白表单的理由写明白了", "空白表格" in v.reason, True)

    # 拆格崩的页（上千空格子）不能被当成空白表单放过
    huge = "表头\n" + "| |\n" * 600
    check("上千空格子 → 不认作空表", _empty_grid(huge), False)

    print(f"全部通过（{stat['n']}/{stat['n']}）" if ok
          else f"有失败项（{stat['bad']}/{stat['n']} 未过）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
