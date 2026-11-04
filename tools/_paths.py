"""评测脚本共用的数据路径。

⚠ 这套评测脚本依赖一份「v1 数据集」，它不在本仓库里：
  242 页扫描件 + 逐页人工真值。那是出版社/收藏者的书页，不能公开。

要复跑评测，把数据集放到任意位置，用环境变量指过来：

    set DOCPLAT_V1_ROOT=D:\\somewhere\\v1        # 该目录下有 work\\pages 和 output\\pages
    set DOCPLAT_GLM_JSON=D:\\somewhere\\result.json

不设就走原作者当年的目录（D:/Ocr/族谱OCR），别人的机器上会直接报「目录不存在」，
这是有意的：与其悄悄跑在空目录上得出假数字，不如当场失败。

只有 tools/selftest_router.py 是完全自包含的 —— 它用合成图 + 仓库自带的
tests/fixtures 真值清单，不需要 v1 数据集，也不需要 GPU。
"""
import os
from pathlib import Path

V1 = Path(os.environ.get("DOCPLAT_V1_ROOT") or "D:/Ocr/族谱OCR")

IMAGES = V1 / "work" / "pages"          # 242 页扫描件（p0001.jpg …）
GOLD_TEXT = V1 / "output" / "pages"     # 逐页人工真值文本
RESULT_JSON = Path(os.environ.get("DOCPLAT_GLM_JSON")
                   or "D:/Ocr/_glm_test/result.json")   # GLM 逐页原始输出
