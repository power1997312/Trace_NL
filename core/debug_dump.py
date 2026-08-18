# -*- coding: utf-8 -*-
"""中间产物 dump 模块

用途: 将 PDF 解析与提取各阶段的中间产物输出到磁盘, 便于排查
      问题出在"解析"阶段(文本/表格没提取对)还是"提取"阶段(条目/章节没切分对).

输出目录结构:
  output/debug/<时间戳>/<文档名>/
    parse/                      # 解析阶段产物(从 PDF 原始内容提取)
      document_result.json     # 新模块完整解析结果(Block树/outline/tables/图片)
      document_result.txt      # 块树缩进预览(快速查看章节/列表/表格结构)
      page_text.txt            # 每页正文文本(清洗后, 可见页眉页脚过滤效果)
      tables.json              # 全文档表格(页码/表头/行数/首行数据)
    extract/                    # 提取阶段产物(从解析文本进一步提取业务数据)
      requirement_items.txt    # 提取的需求条目(ID+内容)
      requirement_items.json
      sections.txt             # 提取的章节(章节号+标题+内容)
      content_map.json         # 内容映射(by_id/by_section/by_heading)
      trace_table.json         # 追踪矩阵表查找结果(表头+全部行)
      trace_relations.json     # 解析出的追踪关系(downstream/upstream)
      summary.json             # 提取阶段统计

开关:
  TRACE_NL_DUMP=0    关闭(默认开启)
  TRACE_NL_DUMP_DIR  自定义输出目录(默认 output/debug)
"""
from __future__ import annotations

import os
import json
import time
import threading

try:
    from config import OUTPUT_DIR
except Exception:
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")

_ENABLED = os.environ.get("TRACE_NL_DUMP", "1") == "1"
_BASE_DIR = os.environ.get("TRACE_NL_DUMP_DIR", "") or os.path.join(OUTPUT_DIR, "debug")

_lock = threading.Lock()
_session_dir = None          # output/debug/<时间戳>/
_doc_dir_cache = {}          # doc_name -> {sub: 绝对路径}


def _get_session_dir() -> str:
    """获取本次运行的会话目录(带时间戳, 便于多次运行对比)"""
    global _session_dir
    if _session_dir is None:
        with _lock:
            if _session_dir is None:
                ts = time.strftime("%Y%m%d_%H%M%S")
                _session_dir = os.path.join(_BASE_DIR, ts)
                os.makedirs(_session_dir, exist_ok=True)
    return _session_dir


def _doc_subdir(pdf_path: str, sub: str) -> str:
    """返回 <会话>/<文档名>/<sub>/ 目录(parse|extract)"""
    doc_name = os.path.splitext(os.path.basename(pdf_path))[0]
    if doc_name not in _doc_dir_cache:
        _doc_dir_cache[doc_name] = {}
    if sub not in _doc_dir_cache[doc_name]:
        d = os.path.join(_get_session_dir(), doc_name, sub)
        os.makedirs(d, exist_ok=True)
        _doc_dir_cache[doc_name][sub] = d
    return _doc_dir_cache[doc_name][sub]


def _write_text(path: str, text: str) -> None:
    """写文本文件(utf-8-sig 带 BOM, 兼容 Windows 记事本中文显示)"""
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(text)


def _write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# 解析阶段产物
# ============================================================

def dump_document_result(pdf_path: str, result) -> None:
    """dump 新模块 DocumentResult(完整 JSON + 块树预览 + 统计)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "parse")
        # 完整 JSON
        _write_json(os.path.join(d, "document_result.json"), result.to_dict())
        # 块树预览(快速查看结构)
        _write_text(os.path.join(d, "document_result.txt"), _render_block_tree(result))
        # 统计
        meta = getattr(result, "meta", {}) or {}
        n_blocks = _count_blocks(result)
        _write_json(os.path.join(d, "summary.json"), {
            "pages": meta.get("pages", 0),
            "text_chars": meta.get("text_chars", 0),
            "n_outline": len(getattr(result, "outline", []) or []),
            "n_blocks": n_blocks,
            "n_warnings": len(getattr(result, "warnings", []) or []),
            "engine": meta.get("engine", ""),
        })
    except Exception:
        pass


def dump_page_texts(pdf_path: str, pages) -> None:
    """dump 每页正文文本(带页码分隔与字符统计)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "parse")
        parts = []
        total = 0
        for p in pages:
            total += len(p.text)
            parts.append(f"===== 第{p.page_number}页 ({len(p.text)}字符) =====\n{p.text}")
        header = (
            f"# 正文文本(页眉页脚过滤/清洗后)\n"
            f"# 共 {len(pages)} 页, {total} 字符\n"
            f"# 用途: 检查文本提取与清洗是否正确; 若此处已缺文字则是解析问题\n\n"
        )
        _write_text(os.path.join(d, "page_text.txt"), header + "\n\n".join(parts))
    except Exception:
        pass


def dump_tables(pdf_path: str, tables) -> None:
    """dump 全文档表格(页码/表头/行数/前3行数据)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "parse")
        data = [{
            "page": getattr(t, "page_number", 0),
            "headers": list(getattr(t, "headers", []) or []),
            "n_rows": len(getattr(t, "rows", []) or []),
            "rows_preview": (getattr(t, "rows", []) or [])[:3],
        } for t in tables]
        _write_json(os.path.join(d, "tables.json"), data)
    except Exception:
        pass


# ============================================================
# 提取阶段产物
# ============================================================

def dump_requirement_items(pdf_path: str, items, doc_type: str = "") -> None:
    """dump 提取的需求条目(ID+内容)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "extract")
        json_data = [{
            "item_id": getattr(it, "item_id", ""),
            "raw_id": getattr(it, "raw_id", ""),
            "page": getattr(it, "page_number", 0),
            "content_len": len(getattr(it, "content", "")),
            "content": getattr(it, "content", ""),
        } for it in items]
        _write_json(os.path.join(d, "requirement_items.json"), json_data)
        parts = [f"# 需求条目提取 文档类型={doc_type} 条目数={len(items)}",
                 f"# 用途: 检查条目切分是否正确; 若 ID 缺失/内容截断则是提取问题", ""]
        for it in items:
            content = getattr(it, "content", "")
            parts.append(f"[{getattr(it, 'item_id', '')}] ({len(content)}字符) 第{getattr(it, 'page_number', 0)}页")
            parts.append(content[:300])
            parts.append("---")
        _write_text(os.path.join(d, "requirement_items.txt"), "\n".join(parts))
    except Exception:
        pass


def dump_sections(pdf_path: str, sections, doc_type: str = "") -> None:
    """dump 提取的章节(章节号+标题+内容)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "extract")
        parts = [f"# 章节提取 文档类型={doc_type} 章节数={len(sections)}",
                 f"# 用途: 检查章节切分是否正确; 若章节号/标题缺失或内容粘连则是提取问题", ""]
        for s in sections:
            content = getattr(s, "content", "")
            title = getattr(s, "section_title", "")
            num = getattr(s, "section_number", "")
            parts.append(f"[{num}] {title} ({len(content)}字符)")
            parts.append(content[:200])
            parts.append("---")
        _write_text(os.path.join(d, "sections.txt"), "\n".join(parts))
    except Exception:
        pass


def dump_content_map(pdf_path: str, content_map) -> None:
    """dump 内容映射(by_id/by_section/by_heading)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "extract")
        data = {
            "by_id": {k: v for k, v in getattr(content_map, "_by_id", {}).items()},
            "by_section": {k: v for k, v in getattr(content_map, "_by_section", {}).items()},
            "by_heading": {k: v for k, v in getattr(content_map, "_by_heading", {}).items()},
        }
        _write_json(os.path.join(d, "content_map.json"), data)
    except Exception:
        pass


def dump_trace_table(pdf_path: str, table, role: str = "") -> None:
    """dump 追踪矩阵表查找结果(表头+全部行)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "extract")
        rows = getattr(table, "rows", []) or []
        data = {
            "role": role,
            "page": getattr(table, "page_number", 0),
            "headers": list(getattr(table, "headers", []) or []),
            "n_rows": len(rows),
            "rows": rows,
        }
        _write_json(os.path.join(d, "trace_table.json"), data)
    except Exception:
        pass


def dump_relations(pdf_path: str, relations) -> None:
    """dump 解析出的追踪关系(downstream_id/upstream_ref/upstream_doc)"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "extract")
        data = [{
            "downstream_id": getattr(r, "downstream_id", ""),
            "upstream_ref": getattr(r, "upstream_ref", ""),
            "upstream_doc": getattr(r, "upstream_doc", ""),
        } for r in relations]
        _write_json(os.path.join(d, "trace_relations.json"), data)
    except Exception:
        pass


def dump_extract_summary(pdf_path: str, meta: dict) -> None:
    """dump 提取阶段统计汇总"""
    if not _ENABLED:
        return
    try:
        d = _doc_subdir(pdf_path, "extract")
        _write_json(os.path.join(d, "summary.json"), meta)
    except Exception:
        pass


# ============================================================
# 内部辅助
# ============================================================

def _walk_all(blocks) -> list:
    out = []
    for b in blocks:
        out.append(b)
        for c in getattr(b, "children", []) or []:
            out.extend(_walk_all([c]))
    return out


def _count_blocks(result) -> int:
    return len(_walk_all(getattr(result, "body", []) or []))


def _render_block_tree(result) -> str:
    """将 DocumentResult 块树渲染为缩进预览文本"""
    lines = ["# 文档大纲 (outline)"]
    outline = getattr(result, "outline", []) or []
    for o in outline:
        lines.append(f"  [L{o.level}] 第{o.page + 1}页  {o.text}")
    lines.append("")
    lines.append("# 块结构 (type/页码/层级/文本预览)")
    body = getattr(result, "body", []) or []
    for b in body:
        _render_node(b, 0, lines)
    lines.append("")
    warnings = getattr(result, "warnings", []) or []
    lines.append(f"# 警告 ({len(warnings)} 条)")
    for w in warnings:
        lines.append(f"  {w}")
    return "\n".join(lines)


def _render_node(b, depth: int, out: list[str]) -> None:
    text = (b.text or "").strip().replace("\n", " ")
    if len(text) > 80:
        text = text[:80] + "..."
    out.append(f"{'  ' * depth}[{b.type}] 第{b.page + 1}页 L{b.level}  {text}")
    for c in getattr(b, "children", []) or []:
        _render_node(c, depth + 1, out)
