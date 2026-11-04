"""版面判定 —— 分类器。**必须在 GLM 跑完之后判，不要在跑之前判。**

━━ 为什么不是「图像分类器」━━
2026-10-30 在 242 页上把图像侧的三条路都试完了，全部不可用：

1) 手调几何特征（长直线 / 线宽 / 闭合格子 / 空白分隔带）
   精确率 17-28%。根因是这本书的竖排正文本身就有「界行」竖线，而且竖排文字
   每个字之间都有行距空隙 —— 文字列在形态上和格线、格子在像素级重叠。
   用 1000px 宽下 11-18px 的线宽去卡真格线，p0001（纯正文）测出 43590 个
   竖线像素，比真表格 p0153 的 9932 还多。

2) PP-DocLayout-S（预训练检测器，Apache-2.0，CPU 0.20s/页）
   召回 59%、精确 22%。把 81 页正文判成表格。

3) PP-DocLayout-L（0.99s/页）
   召回 92%、精确 21%。159/239 页被判有表格框 —— 它把全书界行分栏页都算表格。

人工核对确认过 A 组 9 页「被判表格的正文页」：8 页确实是连续正文，
1 页是服制图。**不是模型参数问题，是这本书的版面在像素层面就分不开。**

━━ 那什么信号是干净的 ━━
GLM 输出出来的**行长结构**：
  · 分格条目页（世系行传 / 目录 / 服制图）→ 每一格只有几个字，行多
  · 连续正文页 → 一句一行，行长
实测中位数：表格页 行数 28 / 行字中位 5；正文页 行数 10 / 行字中位 15。

阈值（config.STRUCT_*）在 232 页上网格搜索得到：
  行字中位<=8 且 行数>=10 且 短行(<=6字)占比>=0.8
  → 命中 42/49 结构化页，**误报 0**

注意「42/49」而不是「32/39」：原标签是按 v1 输出里有没有 `<table>` 标的，
人工核对发现 10 个「误报」里 9 个其实是世系行传/目录/服制图，是**标签错了**，
不是分类器错了。所以 v1 的 routing_242.json 只能当参考，不能当真值。

━━ 因此的架构 ━━
  · 预路由（跑之前）：只信已验证清单；清单外一律 glm（快通道）
  · 结构闸门（跑完 glm 之后，零成本）：中招的页升级到 vl 通道
代价：结构化页多花一次 2s 的 GLM 调用。49 页 × 2s ≈ 100s，
换来的是「48/49 页结构正确」而不是「26/49 页结构正确」。

━━ 已知局限 ━━
  · 3 页（p0164/p0166/p0188）GLM 只吐出 6-11 字，行数 < 10，闸门抓不到 ——
    这类由质量闸门的「仅 N 字」兜住，进人工复核。
  · 阈值只在族谱上标定；现代书没验证过。现代书的表格通常带 markdown 竖线，
    已加 STRUCT_PIPE_MIN 规则兜底，但那同样是经验值，不是实测。
    2026-11-03 在《溧阳商业志》上实测：GLM 对现代印刷数据表（如 p117 酱园业
    资金表、p198 猪肉价格表）**逐行逐格全对**，34 页被管道规则误升级。
    所以管道规则收窄成「表格列数对不齐才升级」—— 列数齐整 = GLM 结构没崩，
    正是它擅长的活；族谱那批靠的是上面的短行规则，不受影响。
"""
import json
import re
import statistics as st
from dataclasses import dataclass
from pathlib import Path

from .. import config as C

TEXT, TABLE, IMAGE, BLANK = "text", "table", "image", "blank"

RE_TAG = re.compile(r"<[^>]+>")
RE_CHINESE_PUNCT = re.compile(r"[\s]")


# ---------- 结构信号（核心） ----------

@dataclass
class Structure:
    n_chars: int = 0
    n_lines: int = 0
    med: float = 0.0
    short_ratio: float = 0.0
    pipe_lines: int = 0
    pipe_ragged: bool = False       # 管道表的列数是否对不齐

    @property
    def structured(self) -> bool:
        if self.n_lines == 0:
            return False
        if self.pipe_lines >= C.STRUCT_PIPE_MIN:
            # 列数齐整的管道表 = GLM 已经把结构还原好了，不用再换 vl
            return self.pipe_ragged
        return (self.med <= C.STRUCT_MED_MAX
                and self.n_lines >= C.STRUCT_LINES_MIN
                and self.short_ratio >= C.STRUCT_SHORT_RATIO)

    def reason(self) -> str:
        if self.pipe_lines >= C.STRUCT_PIPE_MIN:
            return f"{self.pipe_lines} 行 markdown 表格，列数对不齐，疑似拆格失败"
        return (f"行长结构异常：{self.n_lines} 行、行字中位 {self.med:.0f}、"
                f"短行占 {self.short_ratio*100:.0f}%，疑似分格条目页")

    def brief(self) -> str:
        return f"{self.n_lines}行/中位{self.med:.0f}/短行{self.short_ratio:.2f}"


def structure_of(text: str) -> Structure:
    """只看正文，不看任何标记。成本为零（已经在内存里）。"""
    t = RE_TAG.sub("", text or "")
    lines = [re.sub(r"\s", "", ln) for ln in t.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return Structure()
    lens = [len(ln) for ln in lines]
    pipes = [ln for ln in lines if ln.count("|") >= 2]
    return Structure(
        n_chars=sum(lens),
        n_lines=len(lines),
        med=float(st.median(lens)),
        short_ratio=sum(1 for x in lens if x <= 6) / len(lines),
        pipe_lines=len(pipes),
        pipe_ragged=_ragged(pipes),
    )


def _ragged(pipes) -> bool:
    """管道表列数是否对不齐。

    正常表格：表头、分隔行、数据行的 `|` 个数一致（列数 = 竖线数 - 1）。
    GLM 拆格失败时，行与行之间会多一个少一个 —— 那才是要换 vl 的信号。
    少数行（<=1/4）不一致不算，模型偶尔会把长内容折行。
    """
    if len(pipes) < 3:
        return False
    cols = [ln.count("|") for ln in pipes]
    modal = max(set(cols), key=cols.count)
    off = sum(1 for c in cols if c != modal)
    return off / len(cols) > 0.25


# ---------- 已验证清单 ----------

class KnownLayout:
    """已验证的版面清单。

    可以传多个文件，后传的覆盖先传的 —— 所以「人工核对的修正」放第二个，
    就能盖掉 v1 基准里按 <table> 标签误判的条目。
    """

    def __init__(self, paths=None):
        self.map = {}
        self.notes = {}
        if not paths:
            return
        for path in ([paths] if isinstance(paths, (str, Path)) else paths):
            self._load(Path(path))

    def _load(self, p: Path):
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        for r in data.get("pages", []):
            if r.get("kind"):
                self.map[r["id"]] = r["kind"]
                if r.get("note"):
                    self.notes[r["id"]] = r["note"]


# ---------- 可选：预训练版面检测（只对现代书有意义） ----------

class PPDocLayout:
    """PP-DocLayout 预训练检测器。CPU 0.2s/页，不占显存。

    古籍实测精确率 22%（见模块头），所以默认关闭（C.PPDOC_ENABLED）。
    现代印刷书没验证过 —— 打开前请先跑 tools/eval_ppdoc.py 在你自己的书上量一遍。

    坑：paddle 3.3.1 的 oneDNN 内核在 PIR 执行器上有 bug，必须 enable_mkldnn=False，
    否则 NotImplementedError: ConvertPirAttribute2RuntimeAttribute。
    """

    def __init__(self, model_name: str = None, enabled: bool = None):
        self.model_name = model_name or C.PPDOC_MODEL
        self.enabled = C.PPDOC_ENABLED if enabled is None else enabled
        self._m = None
        self._err = ""

    def _load(self):
        if self._m is not None or self._err:
            return
        try:
            from paddleocr import LayoutDetection
            self._m = LayoutDetection(model_name=self.model_name, device="cpu",
                                      enable_mkldnn=False)
        except Exception as e:
            self._err = f"{type(e).__name__}: {e}"

    def classify(self, img: Path) -> str:
        if not self.enabled:
            return TEXT
        self._load()
        if self._m is None:
            return TEXT
        try:
            boxes = []
            for one in self._m.predict(str(img)):
                boxes = ((one.json or {}).get("res") or {}).get("boxes", [])
            n = sum(1 for b in boxes if b.get("label") in C.PPDOC_TABLE_LABELS)
            return TABLE if n else TEXT
        except Exception:
            return TEXT


# ---------- 分类器 ----------

class Classifier:
    """预路由只信清单 + （可选）预训练检测器；真正的判定在结构闸门。

    classify() 在**跑之前**调用，只能返回粗版面；
    escalate() 在**跑完之后**调用，才是决定要不要升级通道的地方。
    """

    def __init__(self, known: KnownLayout = None, ppdoc: PPDocLayout = None):
        self.known = known or KnownLayout()
        self.ppdoc = ppdoc or PPDocLayout()

    def classify(self, img: Path, page_key: str = "") -> str:
        if page_key and page_key in self.known.map:
            return self.known.map[page_key]
        return self.ppdoc.classify(Path(img))

    def escalate(self, text: str, layout: str, channel: str) -> tuple:
        """结构闸门。返回 (新通道, 理由)；不升级返回 ("", "")。

        只对 glm 通道生效：vl 已经是兜底，human 不走模型。
        清单里已标明 kind 的页不参与（那些页的结论已经人工确认过）。
        """
        if channel != "glm" or layout == TABLE:
            return "", ""
        s = structure_of(text)
        if not s.structured:
            return "", ""
        return C.ESCALATE_TO, s.reason()
