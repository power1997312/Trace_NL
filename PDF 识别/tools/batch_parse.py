"""批量 PDF 解析脚本：一键解析单个 PDF 或整个目录。

用法：
  # 解析单个 PDF
  python tools/batch_parse.py 复杂文档.pdf -o output/结果

  # 解析目录下全部 PDF（递归）
  python tools/batch_parse.py complex_pdfs -o output/全部结果

  # 只输出 Markdown
  python tools/batch_parse.py complex_pdfs --format md

输出结构（目录输入时保留相对层级，避免同名冲突）：
  <out>/<相对路径>/<文件名>.md
  <out>/<相对路径>/<文件名>.json
  <out>/<相对路径>/assets/fig_*.png

依赖：.venv（pymupdf + pdfplumber），无 OCR/大模型。
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def find_pdfs(path: str) -> list[tuple[str, str]]:
    """返回 [(abs_path, 相对路径)]，相对路径用于输出目录组织。"""
    if os.path.isfile(path):
        return [(path, os.path.basename(path))]
    out = []
    for root, _, files in os.walk(path):
        for f in sorted(files):
            if f.lower().endswith(".pdf"):
                abs_path = os.path.join(root, f)
                rel = os.path.relpath(abs_path, path)
                out.append((abs_path, rel))
    return sorted(out)


def parse_one(abs_path: str, out_dir: str, fmt: str,
              prefer_camelot: bool) -> dict:
    """解析单份 PDF，把结果写入 out_dir（最终目录，含 assets/），返回统计。"""
    from pdfparser import DocumentParser
    from pdfparser.output import DocumentWriter

    base = os.path.splitext(os.path.basename(abs_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    asset_dir = os.path.join(out_dir, "assets")

    parser = DocumentParser(abs_path, prefer_camelot=prefer_camelot,
                            asset_dir=asset_dir)
    t0 = time.time()
    try:
        result = parser.parse()
    except Exception as e:  # noqa: BLE001
        return {"file": base, "ok": False, "error": str(e),
                "elapsed": round(time.time() - t0, 2)}

    meta = result.meta
    if not meta.get("has_text_layer"):
        return {"file": base, "ok": True, "scanned": True, "pages": meta.get("pages", 0),
                "elapsed": round(time.time() - t0, 2), "warnings": meta.get("warnings", [])}

    writer = DocumentWriter()
    writer.write(result, out_dir, fmt=fmt)
    return {
        "file": base,
        "ok": True,
        "scanned": False,
        "pages": meta.get("pages", 0),
        "elapsed": round(time.time() - t0, 2),
        "n_headings": len(result.outline),
        "n_tables": sum(1 for b in _iter_blocks(result.body)
                        if b.type == "table"),
        "n_assets": meta.get("n_assets", 0),
        "warnings": result.warnings,
        "out_dir": out_dir,
    }


def _iter_blocks(blocks):
    for b in blocks:
        yield b
        yield from _iter_blocks(b.children)


def main():
    ap = argparse.ArgumentParser(description="批量 PDF 结构化解析（不依赖 OCR/大模型）")
    ap.add_argument("input", help="PDF 文件或目录路径")
    ap.add_argument("-o", "--out", default="output", help="输出根目录（默认 output）")
    ap.add_argument("--format", choices=["json", "md", "both"], default="both",
                    help="输出格式（默认 both）")
    ap.add_argument("--no-camelot", action="store_true",
                    help="禁用 camelot 表格引擎（无 Ghostscript 时自动禁用）")
    args = ap.parse_args()

    files = find_pdfs(args.input)
    if not files:
        print(f"[batch] 未找到 PDF：{args.input}")
        return 1

    print(f"[batch] 共 {len(files)} 份 PDF，输出到 {args.out}\n")
    t_all = time.time()
    results = []
    for i, (abs_path, rel) in enumerate(files, 1):
        base = os.path.splitext(os.path.basename(abs_path))[0]
        rel_dir = os.path.dirname(rel)
        out_dir = os.path.join(args.out,
                               os.path.join(rel_dir, base) if rel_dir else base)
        print(f"[{i}/{len(files)}] {rel}", flush=True)
        results.append(parse_one(abs_path, out_dir, args.format,
                                 prefer_camelot=not args.no_camelot))

    # 汇总
    ok = [r for r in results if r.get("ok") and not r.get("scanned")]
    scanned = [r for r in results if r.get("scanned")]
    failed = [r for r in results if not r.get("ok")]
    print("\n========== 汇总 ==========")
    print(f"{'文件':40s} {'页':>4s} {'秒':>5s} {'标题':>4s} {'表':>3s} {'图':>3s} {'告警':>4s}  状态")
    for r in results:
        if not r["ok"]:
            print(f"{r['file']:40s}  ERROR: {r['error'][:40]}")
        elif r.get("scanned"):
            print(f"{r['file']:40s} {r['pages']:4d} {r['elapsed']:5.1f}   扫描件（需 OCR）")
        else:
            print(f"{r['file']:40s} {r['pages']:4d} {r['elapsed']:5.1f} "
                  f"{r['n_headings']:4d} {r['n_tables']:3d} {r['n_assets']:3d} "
                  f"{len(r['warnings']):4d}  完成 → {r['out_dir']}")
    print(f"\n成功 {len(ok)} | 扫描件 {len(scanned)} | 失败 {len(failed)} | "
          f"总耗时 {time.time() - t_all:.1f}s")
    for r in failed:
        print(f"  失败: {r['file']}: {r['error'][:80]}")
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
