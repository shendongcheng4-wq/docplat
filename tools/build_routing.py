"""把 242 页 A/B 结果拆成分流清单（docplat 第一个真实产物）。

【为什么不按"有序相似度"判】
基准（PaddleOCR-VL）的竖排列序不可靠：run_page_by_columns() 依赖 ink_columns()
切列，切不出 >=2 列时退回整页识别，按左→右输出。p0146 是双栏页却没被切开，
基准顺序错乱而 GLM 正确（已对照原图核实 p0146/p0181/p0203）。所以有序相似度
衡量的是"跟基准的顺序是否一致"，而基准顺序本身就不可信。

【为什么不按"块级覆盖"判】
试过 block_cover，被 p0023 证伪：基准 75 字一整段、GLM 切 8 块，内容逐字相同
却只算出 40%（跨块配不上）。基准是否分块取决于 VL 自己，不能作为判据。

【采用字符集重合（bag）】
内容完整性只看"字在不在"，同时免疫分块差异与列序误差。
残余风险（字都对但排错位）由人工抽检 + 列序校验兜住。

用法
  python build_routing.py            # 只看分布
  python build_routing.py --write    # 落盘 routing_242.json
"""
import argparse
import difflib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

SRC = RESULT_JSON
GOLD_DIR = GOLD_TEXT
IMG_DIR = IMAGES
OUT = Path(__file__).resolve().parent.parent / "data" / "routing_242.json"

RE_TAG = re.compile(r"〔待人工复核：.*?〕|（本页无可识别正文：.*?）", re.S)
RE_HTML = re.compile(r"<[^>]+>")
RE_FOREIGN = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")   # 假名 / 谚文
RE_MD = set("#*`>|_-")

SEC_GLM = 1.95
SEC_VL = 35.2
SHORT_CHARS = 20
COVER_OK = 0.90      # 内容齐全
COVER_PART = 0.75    # 需抽查
ORDER_OK = 0.80      # 有序相似度，仅用于标记列序差异


def _clean(t: str) -> str:
    return RE_HTML.sub("", RE_TAG.sub("", t))


def norm(t: str) -> str:
    return "".join(ch for ch in _clean(t) if not ch.isspace() and ch not in RE_MD)


def cov(a: str, b: str) -> float:
    """字符集重合率 —— 主指标，只看内容是否齐全。"""
    if not a or not b:
        return 0.0
    inter = sum((Counter(a) & Counter(b)).values())
    return round(inter / max(len(a), len(b)), 4)


def sim_ord(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return round(sum(x.size for x in sm.get_matching_blocks()) / max(len(a), len(b)), 4)


def load():
    d = json.loads(SRC.read_text(encoding="utf-8"))
    rows = []
    for r in d["pages"]:
        pid = r["id"]
        gf = GOLD_DIR / f"{pid}.md"
        if not gf.exists():
            continue
        raw = gf.read_text(encoding="utf-8")
        low = raw.lower()
        gold, pred = norm(raw), norm(r["pred"])
        gl = len([x for x in _clean(raw).split("\n") if x.strip()])
        pl = len([x for x in _clean(r["pred"]).split("\n") if x.strip()])
        is_table = "<table" in low
        img_n = len(re.findall(r"<img", low))
        gold_len, pred_len = len(gold), len(pred)

        if is_table:
            kind = "table"
        elif img_n and gold_len < SHORT_CHARS:
            kind = "image"
        else:
            kind = "text"

        # 基准本身混入假名/谚文（草书被读成日文）→ 基准不可信，GLM 反而可能对
        gold_foreign = bool(RE_FOREIGN.search(gold))
        pred_foreign = bool(RE_FOREIGN.search(pred))

        rows.append({
            "id": pid,
            "kind": kind,
            "gold_chars": gold_len,
            "pred_chars": pred_len,
            "gold_blocks": gl,
            "pred_blocks": pl,
            "cover": cov(gold, pred) if kind == "text" else None,
            "sim_ord": sim_ord(gold, pred),
            "gold_foreign": gold_foreign,
            "pred_foreign": pred_foreign,
            "sec_glm": r["sec"],
            "err": r.get("err", ""),
            "short": gold_len < SHORT_CHARS,
            "bloat": pred_len > max(gold_len * 3, gold_len + 200),
            "img_tags": img_n,
        })
    return rows


def route(r):
    if r["err"] or r["pred_chars"] == 0:
        return "vl", "识别报错或空输出"
    if r["kind"] == "table":
        return "vl", "表格/世系表，GLM 结构还原失败"
    if r["kind"] == "image":
        return "human", f"整幅影像页（{r['img_tags']} 处裁图），无正文"
    if r["short"]:
        return "human", f"仅 {r['gold_chars']} 字，大字/草书/题签版面"
    if r["bloat"]:
        return "vl", f"输出暴涨 {r['pred_chars']}字（基准 {r['gold_chars']}字），疑似重复生成"
    c = r["cover"]
    tag = ""
    if r["sim_ord"] < ORDER_OK:
        tag = "；与基准顺序不同（待列序校验，非模型错）"
    if c >= COVER_OK:
        return "glm", "内容齐全" + tag
    if c >= COVER_PART:
        return "glm", f"覆盖 {c*100:.0f}%，需抽查" + tag
    return "vl", f"覆盖 {c*100:.0f}%，疑似漏读" + tag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = load()
    done = {r["id"] for r in rows}
    miss = sorted({p.stem for p in IMG_DIR.glob("p*.jpg")} - done)
    print(f"载入 {len(rows)} 页" + (f"（未覆盖 {len(miss)} 页：{', '.join(miss)}）" if miss else ""))
    print()

    for r in rows:
        r["channel"], r["reason"] = route(r)

    print("版面分布")
    for k, v in Counter(r["kind"] for r in rows).most_common():
        print(f"  {k:<8}{v:>4} 页")

    c = Counter(r["channel"] for r in rows)
    print("\n通道路由")
    note = {"glm": "GLM-OCR 直接采用（1.95s）", "vl": "PaddleOCR-VL 兜底（35.2s）",
            "human": "转人工"}
    for ch in ("glm", "vl", "human"):
        if c.get(ch):
            print(f"  {ch:<8}{c[ch]:>5}{c[ch]/len(rows)*100:>7.1f}%   {note[ch]}")

    mins = (c.get("glm", 0) * SEC_GLM + c.get("vl", 0) * SEC_VL) / 60
    base = len(rows) * SEC_VL / 60
    print(f"\n  全量 PaddleOCR-VL : {base:5.1f} 分钟")
    print(f"  分流后            : {mins:5.1f} 分钟  （{base/mins:.1f}x）")

    text = [r for r in rows if r["kind"] == "text" and not r["short"]]
    if text:
        cv = sorted(r["cover"] for r in text)
        n = len(cv)
        print(f"\n  正文页 {n} 个（cover=字符集重合，免疫列序与分块差异）")
        print(f"    平均 {sum(cv)/n*100:.1f}%  中位 {cv[n//2]*100:.1f}%  P10 {cv[int(n*0.1)]*100:.1f}%")
        print(f"    >=90%: {sum(1 for x in cv if x>=COVER_OK):>3} 页   "
              f"75-90%: {sum(1 for x in cv if COVER_PART<=x<COVER_OK):>3} 页   "
              f"<75%: {sum(1 for x in cv if x<COVER_PART):>3} 页")
        sus = [r for r in text if r["cover"] >= COVER_OK and r["sim_ord"] < ORDER_OK]
        print(f"    与基准顺序不同但内容齐全: {len(sus)} 页 ← 需列序校验，不是模型错")
        f = [r for r in text if r["gold_foreign"] and not r["pred_foreign"]]
        if f:
            print(f"    基准混入假名/谚文（基准误识，GLM 更可信）: {len(f)} 页  "
                  f"{', '.join(x['id'] for x in f)}")

    hum = sorted([r for r in rows if r["channel"] == "human"], key=lambda x: x["gold_chars"])
    if hum:
        print(f"\n  转人工 {len(hum)} 页：")
        for r in hum:
            print(f"    {r['id']}  {r['kind']:<6}{r['gold_chars']:>3}字  GLM:{r['pred_chars']:>3}字  {r['reason']}")

    vl = [r for r in rows if r["channel"] == "vl"]
    if vl:
        print(f"\n  需 VL 兜底 {len(vl)} 页：")
        for r in sorted(vl, key=lambda x: (x["cover"] if x["cover"] is not None else -1))[:12]:
            cv = f"{r['cover']*100:5.1f}%" if r["cover"] is not None else "  -  "
            print(f"    {r['id']}  {r['kind']:<6}{cv}  {r['reason']}")
        if len(vl) > 12:
            print(f"    … 另 {len(vl)-12} 页")

    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({
            "meta": {
                "source": str(SRC),
                "gold_dir": str(GOLD_DIR),
                "pages": len(rows),
                "missing": miss,
                "sec_glm": SEC_GLM,
                "sec_vl": SEC_VL,
                "cover_ok": COVER_OK,
                "cover_part": COVER_PART,
                "order_ok": ORDER_OK,
                "generated": datetime.now().isoformat(timespec="seconds"),
                "metric_note": "cover=字符集重合率(免疫列序/分块差异)；sim_ord 仅标记列序差异，不作路由依据",
                "gold_warning": "基准的竖排列序不可靠，不可作为唯一真值；需人工抽检验证",
            },
            "pages": rows,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n✓ 已写入 {OUT}")


if __name__ == "__main__":
    main()
