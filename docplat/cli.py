"""命令行入口。无界面 —— 几十万页的场景不需要工作台，需要的是跑完给报表。

用法
  python -m docplat ingest  --doc zu  --src D:\\path\\to\\images
  python -m docplat run     --doc zu  --channel glm --limit 10
  python -m docplat run     --doc zu                  # 自动路由
  python -m docplat report  --doc zu
  python -m docplat queue   --doc zu  --kind review
  python -m docplat export  --doc zu                  # 桌面/书籍导出/丛书/卷.txt
  python -m docplat bleed   --doc zu                  # 找出「空白页的背面透印」，只报告
  python -m docplat bleed   --doc zu --apply          # 清成空白（原文进 _quarantine）
  python -m docplat setsrc  --doc zu  --src "D:\\...\\卷\\JPG"
  python -m docplat layout-check --doc zu
"""
import argparse
import sys

from . import config as C
from . import auto, pipeline, store


def cmd_ingest(a):
    # 走 import_dir 而不是 ingest：和工作台的「导入」按钮共用一条路，
    # 才有「同目录复用文档号」和「整本重扫拦截」这两道保护。
    try:
        r = pipeline.import_dir(a.src, a.doc, a.name)
    except (FileNotFoundError, ValueError) as e:
        print(f"[导入失败] {e}")
        return 1
    print(f"入库完成：{r['files']} 个文件 → {r['pages']} 页"
          f"（重复 {r['dup']} 个已跳过），预路由 {r['routed']} 页")
    print(f"  文档号 {r['doc_id']}   导出会落在 {r['target']}")


def cmd_run(a):
    r = pipeline.run(a.doc, a.channel, a.limit, quiet=a.quiet)
    print(f"\n完成 {r['pages']} 页  正常 {r['done']}  待复核 {r['review']}  "
          f"升级 {r.get('escalate', 0)}  失败 {r['failed']}  用时 {r['sec']}s")
    if r.get("slow"):
        # 单独一行：不然「用时 2470s」会被误读成模型慢
        print(f"  其中 {r['slow']} 页单页严重超时（服务侧干等，不是模型慢）")


def cmd_retext(a):
    r = pipeline.fallback_text(a.doc, quiet=a.quiet)
    print(f"\n回退重读 {r['pages']} 页，替换 {r['replaced']} 页，用时 {r['sec']}s")


def cmd_auto(a):
    stages = [s.strip() for s in (a.stages or "glm").split(",") if s.strip()]
    if "all" in stages:
        stages = list(auto.STAGES)
    bad = [s for s in stages if s not in auto.STAGES]
    if bad:
        print(f"[参数错误] 不认识的阶段：{', '.join(bad)}（可选 {', '.join(auto.STAGES)}）")
        return 2
    try:
        auto.run_batch(a.root, stages, do_export=not a.no_export,
                       dry=a.dry, only_missing=not a.force, wait=a.wait)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"[批量失败] {e}")
        return 1
    return 0


def cmd_report(a):
    pipeline.report(a.doc)

def cmd_queue(a):
    rows = store.queue(a.doc, a.kind, a.limit)
    if not rows:
        print(f"队列 {a.kind} 为空")
        return
    print(f"队列 {a.kind}（{len(rows)} 条）")
    print(f"  {'页号':>6}  {'版面':<6}{'通道':<6}{'字数':>6}  说明")
    for r in rows:
        print(f"  {r['page_no']:>6}  {r['layout'] or '-':<6}{r['channel'] or '-':<6}"
              f"{r['chars'] or 0:>6}  {(r['err'] or '')[:50]}")


def cmd_export(a):
    p = pipeline.export(a.doc, a.out, src=a.src, root=a.root,
                        ext="md" if a.md else "", report=True)
    print(f"已导出 {p}  ({p.stat().st_size} 字节)")


def cmd_bleed(a):
    r = pipeline.bleed_scan(a.doc, apply=a.apply, include_review=a.all,
                            quiet=False)
    if not a.apply and r["hits"]:
        print("（只报告，未改动。加 --apply 才清空并备份）")
    return 0


def cmd_setsrc(a):
    store.init()
    if not a.src:
        d = store.get_doc(a.doc)
        print(f"{a.doc} 当前来源目录：{d.get('src_dir') or '（空）'}")
        print(f"  → 导出会落在 {pipeline.export_target(d.get('src_dir'))}")
        return
    store.set_doc_src(a.doc, a.src, a.note)
    print(f"已记录来源目录：{a.src}")
    print(f"  → 导出会落在 {pipeline.export_target(a.src)}")


def cmd_layout(a):
    pipeline.layout_check(a.doc)


def cmd_docs(a):
    rows = store.list_docs()
    if not rows:
        print("还没有文档，先 ingest")
        return
    for r in rows:
        print(f"  {r['id']:<16}{r['pages']:>7} 页   {r['src_dir']}")


def cmd_serve(a):
    from .web import serve
    serve(a.host, a.port, a.debug)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="docplat", description="文档识别平台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="扫描目录入库（不填 --doc 就按卷名自动定文档号）")
    p.add_argument("--doc", default="",
                   help="文档号；留空按卷名自动取，同目录重复导入会复用原文档")
    p.add_argument("--src", required=True)
    p.add_argument("--name", default="")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("run", help="跑识别")
    p.add_argument("--doc", required=True)
    p.add_argument("--channel", default="", choices=["", "glm", "vl", "human"])
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("retext",
                       help="vl 读成空壳的图页，改用 GLM 列成纯文本清单")
    p.add_argument("--doc", required=True)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_retext)

    p = sub.add_parser("auto", help="整套书批量跑：扫目录 → 逐卷导入 → 识别 → 导出")
    p.add_argument("--root", required=True,
                   help="丛书目录（下面每卷一个文件夹），也可以直接指某一卷")
    p.add_argument("--stages", default="glm",
                   help="跑哪些阶段，逗号分隔：glm / vl / retext / all。"
                        "默认 glm（只有它不用切显存）。all = glm,vl,retext")
    p.add_argument("--no-export", action="store_true", help="跑完不导出 txt")
    p.add_argument("--dry", action="store_true", help="只列计划，不动手")
    p.add_argument("--force", action="store_true", help="连已跑完的卷也重跑")
    p.add_argument("--wait", action="store_true",
                   help="如果已经有一批在跑，等它结束后自动接着跑（排队）")
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser("report", help="统计报表")
    p.add_argument("--doc", required=True)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("queue", help="查看人工队列")
    p.add_argument("--doc", required=True)
    p.add_argument("--kind", default="review",
                   choices=["review", "escalated", "failed", "lowscore", "all"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("export", help="合并全书（默认按导入目录自动命名，txt）")
    p.add_argument("--doc", required=True)
    p.add_argument("--src", default="",
                   help="导入时选的那个目录；默认用库里记的来源目录")
    p.add_argument("--root", default="",
                   help=f"导出根目录，默认 {C.EXPORT_ROOT}")
    p.add_argument("--out", default="", help="直接指定文件名（相对则落在推导出的目录里）")
    p.add_argument("--md", action="store_true", help="导出 markdown（保留表格管道）而不是 txt")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("bleed",
                       help="找出「本来是空白、只是纸太薄透了背面」的页，清成空白")
    p.add_argument("--doc", required=True)
    p.add_argument("--apply", action="store_true",
                   help="真的清空（原文备份到 data/_quarantine）；不加只报告")
    p.add_argument("--all", action="store_true",
                   help="连 review/escalated 的页一起清（默认只动 done）")
    p.set_defaults(func=cmd_bleed)

    p = sub.add_parser("setsrc", help="查看/修正文档的来源目录（决定导出路径与文件名）")
    p.add_argument("--doc", required=True)
    p.add_argument("--src", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_setsrc)

    p = sub.add_parser("layout-check", help="只跑版面分类器（验证准确率）")
    p.add_argument("--doc", required=True)
    p.set_defaults(func=cmd_layout)

    p = sub.add_parser("docs", help="列出文档")
    p.set_defaults(func=cmd_docs)

    p = sub.add_parser("serve", help="启动校对工作台（浏览器里看队列、核对待人工页）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7788)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_serve)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
