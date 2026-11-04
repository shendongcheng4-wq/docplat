"""全局配置。所有阈值集中在这里，改一处即生效，且能写进日志便于回溯。

需要按环境改的三项走环境变量，其余都是实测调出来的常量：
  DOCPLAT_EXPORT_ROOT   导出交付目录（默认 桌面/书籍导出）
  DOCPLAT_GLM_BASE      GLM 服务地址（默认 http://127.0.0.1:8081）
  DOCPLAT_GLM_SERVE     GLM 启动脚本（默认 <项目>/scripts/glm_serve.py）
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "docplat.db"
PAGES = DATA / "pages"
EXPORTS = DATA / "exports"      # 库内暂存（--out 不指定时的旧行为），正式交付见 EXPORT_ROOT

# ---- 导出交付 ----
# 命名规则来自实际导入习惯：源目录是「丛书\卷\JPG」。
#   文件名 = JPG 的上一级（卷名）        → 某氏宗谱（卷一）.txt
#   外层目录 = JPG 的上上一级（丛书名）  → 书籍导出\某氏宗谱\
# 默认交付到桌面（作者的固定习惯），换机器时用 DOCPLAT_EXPORT_ROOT 覆盖。
EXPORT_ROOT = Path(os.environ.get("DOCPLAT_EXPORT_ROOT")
                   or Path.home() / "Desktop" / "书籍导出")
EXPORT_FORMAT = "txt"
# 这些目录名只是「图片堆放层」，不代表书本身，起名字时要跳过
IMG_DIR_NAMES = {
    "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp", "gif", "pdf",
    "img", "image", "images", "imgs", "pic", "pics", "photo", "photos",
    "图片", "图像", "影像", "照片", "扫描", "扫描件", "原图", "书页",
}

# 「收集根」：源目录直接摊在它下面（书籍\书名\图片），说明没有丛书层。
# 这时外层目录用书名自己，别落成 书籍导出\书籍\书名.txt（2026-11-03 用户口径）。
SERIES_ROOT_NAMES = {"书籍", "书", "图书", "电子书", "books", "book"}

# 分片：用内容 hash 前 2 位做目录，256 个分片，避免单目录几十万文件拖垮 NTFS
SHARD = 2

# ---- 模型服务 ----
# 默认指本机的 llama.cpp 服务（见 scripts/glm_serve.py）；换地址用环境变量。
GLM_BASE = os.environ.get("DOCPLAT_GLM_BASE") or "http://127.0.0.1:8081"
GLM_PROMPT = "Text Recognition:"
GLM_MAX_TOKENS = 6000
GLM_MODEL = ""            # 空=自动从 /v1/models 探测

# ---- 超时与重试 ----
# v1 的教训：7.9% 的页超过 60s，最慢 1305s，把均值从 6s 拖到 35s。
# 硬超时 + 重试队列比"让模型慢慢想"划算得多。
PAGE_TIMEOUT = 120
MAX_RETRY = 2

# ---- 质量闸门 ----
COVER_OK = 0.90           # 内容齐全
COVER_PART = 0.75         # 可接受但需抽查
ORDER_OK = 0.80           # 有序相似度（仅标记列序差异）
SHORT_CHARS = 20          # 少于这个字数 → 大字/草书/题签版面

# ---- 表格页的字数下限 ----
# 表格/世系表页天然是多字页：39 页 vl 实测，正常页最少 49 字。
# 低于这个值基本是「图上密布文字、模型只吐出残片」——族谱里的世系树、
# 服制图（图内小字）就是这种失手方式，输出看着像表格其实是空壳。
TABLE_MIN_CHARS = 40

# ---- 空壳网格 ----
# VL 会把一页拆成海量单元格然后几乎全空（p0162：2730 格、5% 有字）。
# 只对真带 <td> 的输出生效，现代书的 markdown 表格不适用。
GRID_CELLS_MIN = 100      # 单元格数超过这个
GRID_FILL_MIN = 0.30      # 且填充率低于这个 → 判为拆格失败

# ---- 退化重复 ----
# 模型卡死后会把同一片段反复吐（p0147 `子二`×1967、p0188 `奏請`×2020）。
# 用 zlib 压缩冗余度衡量：正常页中位 0.28、P95 约 0.20~0.5，
# 退化页 0.97~0.99，中间没有样本。渲染时按同一阈值截断。
DEGEN_REDUNDANCY = 0.85

# ---- 退化重复（整块版）----
# 单片段重复由 DEGEN_REDUNDANCY 兜；但模型还会「整块重复」：溧阳商业志 p160
# 把同 8 行数值原样吐了 7 遍（丝网/钩卡/虾笼/拖虾网/围箔/黄鳝笼/塘网 全成了
# 一模一样的数），每行本身都不重复，zlib 冗余度只有 0.4，两道闸门全放过。
# 口径：k≥3 行的块、在同一页出现 ≥4 次、覆盖行数占比 ≥RATIO。
# 2410 页（7 本书）实测：只有 p160 命中 0.54，次高是族谱捐款名录 0.22
# （真的每笔都叫「懷德堂/伍佰元」，属正常），间隔够宽。
# 注意这是「判退化 → 记 review」，不是「压缩掉」——压缩会把整块真数据洗成
# 「（重复 N 次）」，静默丢内容比报错更糟。
BLOCK_REP_KMIN = 3
BLOCK_REP_TIMES = 4
BLOCK_REP_KMAX = 8
BLOCK_REP_RATIO = 0.30

# ---- vl 失败后回退 GLM 纯文本 ----
# 族谱的世系表/世系图/服制图，字本来就是按行按列排的短条目，不需要「表格」这层结构。
# vl 一旦判定失败（漏识 / 拆格崩），保留原样只剩空格子和碎格子，所以统一换成 GLM 的
# 纯文本清单 —— 代价是丢结构，而这些版面本来就没有值得保留的结构。
# 唯一门槛：换成纯文本后不能明显变少，否则等于白换。
FALLBACK_MIN_KEEP = 0.8        # 拆格崩页：结构是废的，0.8 倍即可
FALLBACK_MIN_KEEP_LEAK = 1.0   # 漏识页：结构也留着没用，但字数不能倒退

# 生产模式（无基准）的可疑信号
MIN_CHARS_PER_1K_PX = 0.02   # 每千像素墨迹面积对应的最少字数
BLOAT_RATIO = 3.0            # 输出字数 / 预期字数 超过这个值 → 疑似重复生成

# ---- 版面真值清单 ----
# 按顺序加载，后面的覆盖前面的：routing_242 是 v1 基准自动生成的，
# layout_truth 是人工看图核对后的修正（v1 把世系行传误标成正文）。
TRUTH_FILES = ("routing_242.json", "layout_truth.json")

# ---- 结构闸门（核心）----
# 判断「这一页是分格条目（世系表/目录/服制图）而不是连续正文」。
# 必须在 GLM 跑完之后判：图像侧判不出来（三次尝试，精确率 17-28%）。
# 实测（2026-10-30，242 页，修正标签后）：命中 42/49 结构化页，误报 0。
STRUCT_MED_MAX = 8.0      # 行字中位数 <= 这个 → 短行
STRUCT_LINES_MIN = 10     # 且行数 >= 这个
STRUCT_SHORT_RATIO = 0.8  # 且「<=6 字的行」占比 >= 这个
STRUCT_PIPE_MIN = 2       # 或者有 >= 2 行带 "|" 的 markdown 表格（现代书常见）
ESCALATE_TO = "vl"        # 被结构闸门判为中招的页升级到哪个通道

# ---- 可选：预训练版面检测（PP-DocLayout，CPU）----
# 只在「现代印刷书」上有意义；古籍上实测精确率 22%，默认关闭。
PPDOC_ENABLED = False
PPDOC_MODEL = "PP-DocLayout-S"
PPDOC_TABLE_LABELS = {"table", "table_title", "table_caption", "table_footnote"}

# ---- 单人模式 ----
# 不做任务锁；核对记录带 reviewer，将来要多人时不用改表
REVIEWER = "local"

# ---- 暂停 ----
# 暂停不做进程挂起（Windows 上没有 SIGSTOP，强挂会把 HTTP 请求晾在一边超时），
# 改成「标志文件」：跑批在**每一页之前**检查它，有就原地等，清掉就接着跑。
# 粒度是一页 —— 已经跑完的页一定保住。
PAUSE_FLAG = DATA / "pause.flag"

# ---- vl 通道的输入尺寸 ----
# PaddleOCR-VL 的耗时随面积走：溧阳商业志原图 5043×7243（36MP）实测 4 分钟/页，
# 而族谱 3219×1933 只要 35 秒/页。长边压到 3200（≈族谱同量级）后速度回到可用区间，
# 表格千分位/小数点仍清晰。只影响送进模型的临时副本，不动原图。
VL_MAX_SIDE = 3200

# ---- 引擎显存需求（GB），用于判断能否同批运行 ----
GPU_TOTAL_GB = 15.9
# paddle 的值是「引擎自己要占多少」，不含桌面基线（约 0.6 GB）。
# v1 实测：整卡已用 15.4 GB、基线 0.6 GB → 引擎约 14.8 GB。
# 早先写的 15.5 是整卡峰值，会让 ensure_room 在真正够用时误报不足。
ENGINE_GPU_GB = {"glm": 4.5, "paddle": 14.8}
