"""量化验证工具：批量解析 complex_pdfs 全部文档并输出指标。

用法：
  python tools/evaluate.py [--out output/baseline.json] [--dir complex_pdfs]

指标（每文档）：
  file/pages/elapsed/ok           基本
  has_text_layer/scanned          扫描判别
  n_headings/n_outline            标题数
  n_tables/n_table_cells          表格数与总单元格
  n_warnings                     警告数
  n_body_blocks                  正文块数
  text_recall                    文本召回率 = 解析输出字符 / fitz 原始文本字符
  engine                         表格引擎
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def count_chars_in_result(result: dict) -> int:
    """统计解析结果中全部文本字符（标题+正文+列表+表格+图题）。"""
    total = 0

    def walk(b):
        nonlocal total
        t = b["type"]
        if t == "heading":
            total += len(b.get("text", ""))
        elif t in ("text", "paragraph", "caption"):
            total += len(b.get("text", ""))
        elif t == "list":
            for reg in b.get("list_items", []):
                total += len(reg.get("text", ""))
                for it in reg.get("items", []):
                    total += len(it.get("text", ""))
                    for ch in it.get("children", []):
                        total += len(ch.get("text", ""))
        elif t == "table" and b.get("table"):
            for row in b["table"]["rows"]:
                for c in row:
                    total += len(c.get("text", ""))
        elif t == "image":
            total += len((b.get("image_info") or {}).get("caption", ""))
        for c in b.get("children", []):
            walk(c)

    for b in result.get("body", []):
        walk(b)
    return total


def evaluate_one(path: str) -> dict:
    from pdfparser import DocumentParser

    doc = DocumentParser(path)
    t0 = time.time()
    try:
        r = doc.parse()
    except Exception as e:  # noqa: BLE001
        return {"file": os.path.basename(path), "ok": False, "error": str(e),
                "elapsed": round(time.time() - t0, 2)}
    d = r.to_dict()

    # 文本召回基准：fitz 原始提取
    import fitz
    fz = fitz.open(path)
    raw_chars = sum(len(fz[i].get_text("text")) for i in range(fz.page_count))
    fz.close()

    out_chars = count_chars_in_result(d)
    n_tables = 0
    n_cells = 0

    def walk_tables(b):
        nonlocal n_tables, n_cells
        if b["type"] == "table" and b.get("table"):
            n_tables += 1
            for row in b["table"]["rows"]:
                n_cells += len(row)
        for c in b.get("children", []):
            walk_tables(c)

    for b in d["body"]:
        walk_tables(b)

    return {
        "file": os.path.basename(path),
        "pages": d.get("meta", {}).get("pages", 0),
        "ok": True,
        "elapsed": round(time.time() - t0, 2),
        "has_text_layer": d.get("meta", {}).get("has_text_layer", False),
        "scanned": d.get("meta", {}).get("scanned", False),
        "n_headings": len(d.get("outline", [])),
        "n_tables": n_tables,
        "n_table_cells": n_cells,
        "n_warnings": len(d.get("warnings", [])),
        "n_body_blocks": len(d.get("body", [])),
        "raw_chars": raw_chars,
        "out_chars": out_chars,
        "text_recall": round(out_chars / raw_chars, 4) if raw_chars else 0.0,
        "engine": d.get("meta", {}).get("table_engines", []),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="complex_pdfs")
    ap.add_argument("--out", default="output/baseline.json")
    args = ap.parse_args()

    files = sorted(os.path.join(r, f) for r, _, fs in os.walk(args.dir)
                   for f in fs if f.endswith(".pdf"))
    results = []
    for path in files:
        print(f"→ {os.path.basename(path)}", flush=True)
        results.append(evaluate_one(path))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    print("\n========== 汇总 ==========")
    print(f"{'文件':38s} {'页':>4s} {'秒':>5s} {'标题':>4s} {'表':>2s} {'告警':>4s} {'召回':>7s} {'文本层'}")
    for r in results:
        if not r["ok"]:
            print(f"{r['file']:38s} ERROR: {r['error'][:40]}")
            continue
        tl = "✓" if r["has_text_layer"] else ("扫描" if r["scanned"] else "✗")
        print(f"{r['file']:38s} {r['pages']:4d} {r['elapsed']:5.1f} "
              f"{r['n_headings']:4d} {r['n_tables']:2d} {r['n_warnings']:4d} "
              f"{r['text_recall']:6.1%} {tl}")
    print(f"\n结果已存 {args.out}")


if __name__ == "__main__":
    main()
