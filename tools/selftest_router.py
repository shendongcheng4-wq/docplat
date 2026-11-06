"""路由链路自检 —— 不用 GPU、不用真模型，验证「升级」这条控制流是对的。

为什么需要它：结构闸门改的是「跑完之后要不要换通道」这条路径，写错了的表现是
「页面永远停在 pending」或者「无限升级」，而且要真机跑几百页才看得出来。
这里用假引擎把链路走通，十几秒出结果。

  python tools/selftest_router.py

坑：pipeline 里是 `from .models import get as get_engine`，导入时就绑定了，
所以替身必须挂在 `pipeline.get_engine` 上，改 `docplat.models.get` 不生效。
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from docplat import config as C, pipeline, store          # noqa: E402
from docplat.models.base import Engine, Recognition      # noqa: E402
from docplat.router import structure_of                  # noqa: E402

# 版面真值清单随仓库提供（tests/fixtures/），只含「第几页是什么版面」和字符统计，
# 不含任何书页正文，所以可以公开。
FIXTURES = ROOT / "tests" / "fixtures"


def _blank_png(path: Path, w: int = 1000, h: int = 1400, mark: int = 0):
    """写一张近似纯白的 PNG，第 mark 行压一道浅灰。

    自检用不到图像内容：版面结论来自真值清单（按文件名 p0050/p0146/p0153 命中），
    正文来自 FakeEngine。所以不必往仓库里放真实扫描件 —— 那是有版权的书页。
    那道浅灰只是为了让三张图的 sha1 不同：入库会按内容去重，全白会被判成重复页。
    """
    import struct
    import zlib
    rows = []
    for y in range(h):
        px = b"\xc8" * w if y == mark % h else b"\xff" * w
        rows.append(b"\x00" + px)
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9))
                     + chunk(b"IEND", b""))

# 下面是合成文本，不是任何真实书页的内容（原版抄的是测试书的正文，涉版权）。
# 只保留「形态」——结构闸门看的是行长中位数、行数、短行占比，不看字义。
#
# 分格条目形态：每行 5 字、共 32 行（世系行传/目录就是这种排法）
_CELLS = ("甲乙丙丁戊", "己庚辛壬癸", "子丑寅卯辰", "巳午未申酉")
TABLE_TEXT = "\n".join([_CELLS[i % 4] for i in range(32)])
# 连续正文形态：一句一行，行长 40 字（公版《千字文》）
BODY_TEXT = "\n".join([
    "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜",
    "金生丽水玉出昆冈剑号巨阙珠称夜光果珍李柰菜重芥姜海咸河淡鳞潜羽翔龙师火帝鸟官人皇",
] * 6)


class FakeEngine(Engine):
    def __init__(self, name, plan):
        self.name = name
        self.plan = plan

    def recognize(self, img: Path, hint: str = "") -> Recognition:
        return Recognition(text=self.plan.get(img.name, BODY_TEXT),
                           engine=self.name, sec=0.01)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="docplat_selftest_"))
    ok = 0
    try:
        # 把配置指向临时目录，绝不能碰真实库
        C.DB, C.PAGES, C.DATA = tmp / "t.db", tmp / "pages", tmp
        shutil.copy(FIXTURES / "routing_242.json", tmp / "routing_242.json")

        src = tmp / "src"
        src.mkdir()
        for i, name in enumerate(("p0153.jpg", "p0050.jpg", "p0146.jpg")):
            _blank_png(src / name, mark=100 + i * 37)

        r = pipeline.ingest("t", str(src), "自检")
        assert r["pages"] == 3, r
        print(f"✓ 入库 {r['pages']} 页")
        ok += 1

        # 清单里 p0153/p0146 是 table（走 vl），p0050 是 text（走 glm）
        plan = {"p0153.jpg": TABLE_TEXT, "p0050.jpg": BODY_TEXT,
                "p0146.jpg": TABLE_TEXT}
        orig = pipeline.get_engine
        pipeline.get_engine = lambda ch: FakeEngine(ch, plan)

        try:
            st = pipeline.run("t", "glm", quiet=True)
        finally:
            pipeline.get_engine = orig
        # 清单里 p0153=table（留给 vl）、p0146=text、p0050=text → glm 跑 2 页。
        # p0146 喂的是分格文本，所以当场被闸门抓走 → escalate=1
        assert st["pages"] == 2 and st["escalate"] == 1, f"预期 2 页 1 升级，实际 {st}"
        print(f"✓ 预路由生效：glm 通道跑 2 页（p0153 留给 vl）；"
              f"p0146 当场被闸门抓走")
        ok += 1

        rows = {Path(p["src_path"]).name: p for p in store.queue("t", "all", 10 ** 9)}
        h = rows["p0050.jpg"]["hash"]

        # 闸门单测
        assert structure_of(TABLE_TEXT).structured
        assert not structure_of(BODY_TEXT).structured
        print("✓ 闸门判定：分格形态→升级，正文形态→放行")
        ok += 1

        # 全链路：把 p0050 伪装成「GLM 读出来是分格条目」
        plan["p0050.jpg"] = TABLE_TEXT
        store.set_page("t", h, status="pending", retries=0)
        pipeline.get_engine = lambda ch: FakeEngine(ch, plan)
        try:
            st2 = pipeline.run("t", "glm", quiet=True)
        finally:
            pipeline.get_engine = orig
        assert st2["escalate"] == 1, st2
        pg = store.get_page("t", h)
        assert pg["channel"] == "vl" and pg["status"] == "pending", pg
        assert pg["layout"] == "table", pg
        print(f"✓ 闸门生效：p0050 升级到 vl（layout={pg['layout']}），状态 pending 等第二轮")
        ok += 1

        # 第二轮：vl 通道把升级页 + 清单表格页一起跑完
        pipeline.get_engine = lambda ch: FakeEngine(ch, plan)
        try:
            st3 = pipeline.run("t", "vl", quiet=True)
        finally:
            pipeline.get_engine = orig
        pg = store.get_page("t", h)
        assert st3["pages"] == 3, f"vl 通道该跑 3 页（1 升级 + 2 清单），实际 {st3}"
        assert pg["status"] in ("done", "review") and pg["engine"] == "vl", pg
        print(f"✓ 第二轮 vl：{st3['pages']} 页跑完，p0050 引擎={pg['engine']} "
              f"状态={pg['status']}")
        ok += 1

        c = store.queue_counts("t")
        assert c["escalated"] == 0, c
        print(f"✓ 队列清空：{c}")
        ok += 1

        # 「重跑本页」不许换模型、不许进升级队列（2026-11-06 用户定：
        # 待人工的图片只有重跑这个选项，不能升级换模型跑）。
        # 这里故意让重跑的结果是分格文本：闸门若还开着，这页会被改成
        # channel=vl / pending，下一轮「换 VL 重跑 N」就把它批量换模型跑掉了。
        store.set_page("t", h, layout="text", channel="glm", status="review",
                       err="疑似重复生成")
        pipeline.get_engine = lambda ch: FakeEngine(ch, plan)
        try:
            st4 = pipeline.rerun_one("t", h)
        finally:
            pipeline.get_engine = orig
        pg = store.get_page("t", h)
        assert st4["escalate"] == 0, st4
        assert pg["channel"] == "glm", pg
        assert pg["status"] in ("done", "review"), pg
        assert h not in [p["hash"] for p in store.queue("t", "escalated", 99)]
        print(f"✓ 重跑不换模型：通道仍 {pg['channel']}、状态 {pg['status']}，"
              f"没被闸门抓去 vl")
        ok += 1

        print(f"\n全部通过（{ok}/7）")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
