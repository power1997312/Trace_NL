"""CLI 入口：python -m pdfparser.cli input.pdf -o output/ [--format json|md|both]

示例：
  python -m pdfparser.cli samples/sample_a.pdf -o output/ --format both
  python -m pdfparser.cli samples/sample_b.pdf --no-camelot
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pdfparser",
        description="工程级 PDF 结构化提取（不依赖 OCR/大模型）")
    ap.add_argument("input", help="输入 PDF 路径")
    ap.add_argument("-o", "--out", default="output", help="输出目录")
    ap.add_argument("--format", choices=["json", "md", "both"], default="both")
    ap.add_argument("--no-camelot", action="store_true",
                    help="禁用 camelot（无 Ghostscript 时自动禁用）")
    ap.add_argument("--asset-dir", default=None, help="图片资产输出目录")
    args = ap.parse_args(argv)

    sys.path.insert(0, "src")  # 允许直接以源码方式运行
    from pdfparser import DocumentParser
    from pdfparser.output import DocumentWriter

    t0 = time.time()
    print(f"[pdfparser] 解析: {args.input}")

    def progress(p, n):
        if n > 0 and p % max(1, n // 10) == 0:
            print(f"  提取进度: {p + 1}/{n}", end="\r")

    parser = DocumentParser(args.input, prefer_camelot=not args.no_camelot,
                            asset_dir=args.asset_dir or os.path.join(args.out, "assets"))
    result = parser.parse(progress=progress)
    print(f"\n[pdfparser] 用时 {time.time() - t0:.1f}s")

    if not result.meta.get("has_text_layer"):
        print("[pdfparser] !! 文档判定为扫描型/图像型，无文本层，需 OCR 通道")
        print("   ", "; ".join(result.meta.get("warnings", [])))
        return 1

    writer = DocumentWriter()
    paths = writer.write(result, args.out, fmt=args.format)
    for k, p in paths.items():
        print(f"  [输出] {k}: {p}")
    print(f"  [元信息] 页数={result.meta.get('pages')}  "
          f"表格引擎={result.meta.get('table_engines')}  "
          f"图片资产={result.meta.get('n_assets')}")
    print(f"  [大纲] {len(result.outline)} 条标题")
    if result.warnings:
        print(f"  [警告] {len(result.warnings)} 条")
        for w in result.warnings[:10]:
            print(f"    - {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
