"""把页面拼成带编号的联系表，用来人工快速标注版面真值。

  python tools/make_sheet.py            # 生成 4 张分层抽样拼版
输出 _tmp/sheets/<name>.png（3 列 x 4 行，每格左上角烧上页号）
"""
import json
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import IMAGES, GOLD_TEXT, RESULT_JSON   # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
IMG = IMAGES
OUT = ROOT / "_tmp" / "sheets"
TILE_W, TILE_H = 460, 760
COLS, ROWS = 3, 4


def load(pid):
    raw = cv2.imdecode(np.fromfile(str(IMG / f"{pid}.jpg"), np.uint8), cv2.IMREAD_GRAYSCALE)
    if raw is None:
        return None
    h, w = raw.shape
    s = min(TILE_W / w, TILE_H / h)
    g = cv2.resize(raw, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    tile = np.full((TILE_H, TILE_W), 255, np.uint8)
    oy, ox = (TILE_H - g.shape[0]) // 2, (TILE_W - g.shape[1]) // 2
    tile[oy:oy + g.shape[0], ox:ox + g.shape[1]] = g
    tile = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
    cv2.putText(tile, pid, (10, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 220), 2, cv2.LINE_AA)
    return tile


def sheet(name, ids):
    t = np.full((ROWS * TILE_H, COLS * TILE_W, 3), 60, np.uint8)
    for i, pid in enumerate(ids[:ROWS * COLS]):
        tile = load(pid)
        if tile is None:
            continue
        r, c = divmod(i, COLS)
        t[r * TILE_H:(r + 1) * TILE_H, c * TILE_W:(c + 1) * TILE_W] = tile
    OUT.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT / f"{name}.png"), t)
    print(f"{name}: {', '.join(ids)}")


def main():
    rt = {r["id"]: r for r in json.loads(
        (ROOT / "tests" / "fixtures" / "routing_242.json").read_text(encoding="utf-8"))["pages"]}
    s = json.loads((ROOT / "data" / "layout_ppdoc_S.json").read_text(encoding="utf-8"))["pages"]
    fp = [k for k in s if s[k]["n_table"] and rt.get(k, {}).get("kind") == "text"]
    tp = [k for k in s if s[k]["n_table"] and rt.get(k, {}).get("kind") == "table"]
    tn = [k for k in s if not s[k]["n_table"] and rt.get(k, {}).get("kind") == "text"]
    fn = [k for k in s if not s[k]["n_table"] and rt.get(k, {}).get("kind") == "table"]
    # 等距抽样，避免全落在同一段落里
    def pick(lst, n):
        if not lst:
            return []
        step = max(1, len(lst) // n)
        return sorted(lst)[::step][:n]
    sheet("A_pred_table_lbl_text", pick(fp, 9))
    sheet("B_pred_table_lbl_table", pick(tp, 9))
    sheet("C_pred_text_lbl_text", pick(tn, 9))
    sheet("D_pred_text_lbl_table", pick(fn, 9))


if __name__ == "__main__":
    main()
