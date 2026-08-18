"""L4 输出层：JSON 序列化 + Markdown 渲染。

Markdown 渲染规则：
- heading  →  #/##/###（level 对应）
- paragraph → 文本段（media 中的图片以 ![图题](资产路径) 内联）
- list     → 有序/无序嵌套列表（* 与 1.）
- table    → 管道表格（| a | b |），表头行与数据行分隔
- image    → ![图题](资产路径)
- header_footer → 忽略（不入正文）
"""
from __future__ import annotations

import json
import os
from typing import Optional

from pdfparser.models import DocumentResult, Block

_CJK_EXTRA = set("，。；：""''《》（）…—！？、·【】")


def _is_cjk(ch: str) -> bool:
    return '\u4e00' <= ch <= '\u9fff' or ch in _CJK_EXTRA


def _join_lines(text: str) -> str:
    """段内行合并为连续文本：中文/标点紧连，英文数字间补空格。"""
    parts = [ln.strip() for ln in text.split('\n') if ln.strip()]
    if not parts:
        return ""
    out = parts[0]
    for ln in parts[1:]:
        a, b = out[-1], ln[0]
        out += ln if (_is_cjk(a) or _is_cjk(b)) else " " + ln
    return out


class JsonRenderer:
    @staticmethod
    def render(result: DocumentResult, indent: int = 2) -> str:
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=indent)


class MarkdownRenderer:
    @staticmethod
    def render(result: DocumentResult, asset_dir: Optional[str] = None) -> str:
        lines: list[str] = []
        # 标题（可选：文档大标题）
        title = result.meta.get("file", "")
        if title:
            lines.append(f"# {title}\n")
        for blk in result.body:
            MarkdownRenderer._render_block(blk, lines, asset_dir)
        return "\n".join(lines)

    @staticmethod
    def _render_block(b: Block, lines: list[str], asset_dir: Optional[str]) -> None:
        t = b.type
        if t == "heading":
            level = min(max(b.level, 1), 6)
            lines.append(f"{'#' * level} {b.text.strip()}\n")
        elif t in ("paragraph", "text"):
            text = _join_lines(b.text)
            for m in b.media:
                cap = m.get("caption", "")
                lines.append(f"![{cap}](assets/{m.get('asset', '')})")
            if text:
                lines.append(text + "\n")
        elif t == "caption":
            if b.text.strip():
                lines.append(b.text.strip() + "\n")
        elif t == "figure":
            # 结构图/矢量图：优先内联完整图片；alt 用图题而非全部标注
            # （避免渲染端按 '/' 分隔渲染成长列文字）
            asset = (b.image_info or {}).get("asset", "")
            labels = b.text
            if labels.startswith("[矢量图] "):
                labels = labels[len("[矢量图] "):]
            alt = labels[:60]
            if " / " in alt:
                alt = f"[结构图] 共 {len(labels.split(' / '))} 项标注"
            if asset:
                lines.append(f"![{alt}](assets/{asset})\n")
            else:
                lines.append(f"> [图] 共 {len(labels.split(' / '))} 项标注：{labels}\n")
        elif t == "list":
            MarkdownRenderer._render_regions(b.list_items or [], lines)
        elif t == "table" and b.table is not None:
            MarkdownRenderer._render_table(b.table, lines)
        elif t == "image":
            cap = (b.image_info or {}).get("caption", "")
            asset = b.image_info.get("asset", "") if b.image_info else ""
            if asset:
                lines.append(f"![{cap}](assets/{asset})\n")
        # 子块递归（标题 children）
        for c in b.children:
            MarkdownRenderer._render_block(c, lines, asset_dir)

    @staticmethod
    def _render_regions(regions: list[dict], lines: list[str]) -> None:
        """渲染列表块（intro/list 区域混合序列）。"""
        for r in regions:
            if r.get("type") == "intro":
                if r.get("text"):
                    lines.append(r["text"] + "\n")
            else:
                MarkdownRenderer._render_list(r.get("items", []), lines, 0)

    @staticmethod
    def _render_list(items: list[dict], lines: list[str], depth: int) -> None:
        for it in items:
            indent = "  " * depth
            bullet = "1." if it.get("ordered") else "-"
            lines.append(f"{indent}{bullet} {it.get('text', '')}")
            if it.get("children"):
                MarkdownRenderer._render_list(it["children"], lines, depth + 1)
        lines.append("")

    @staticmethod
    def _render_table(t, lines: list[str]) -> None:
        if not t.rows:
            return
        n_cols = t.n_cols
        header = t.rows[0]
        lines.append("| " + " | ".join(c.text.strip().replace("\n", " ") for c in header) + " |")
        lines.append("|" + "---|" * n_cols)
        # 表头行始终作为 header 渲染过，数据区从第 2 行起（header_rows 记录重复表头
        # 用于跨页拼接，渲染无需再跳过——rows[0] 即首表头）
        for row in t.rows[1:]:
            cells = [c.text.strip().replace("\n", " ") for c in row]
            # 补齐列数
            cells += [""] * (n_cols - len(cells))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")


class DocumentWriter:
    """结果落盘：JSON + Markdown + 图片资产路径回填。"""

    def write(self, result: DocumentResult, out_dir: str,
              fmt: str = "both") -> dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.splitext(os.path.basename(result.meta.get("file", "doc")))[0]
        paths: dict[str, str] = {}
        if fmt in ("json", "both"):
            p = os.path.join(out_dir, f"{base}.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write(JsonRenderer.render(result))
            paths["json"] = p
        if fmt in ("md", "both"):
            p = os.path.join(out_dir, f"{base}.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(MarkdownRenderer.render(result))
            paths["md"] = p
        return paths
