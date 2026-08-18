"""L1 原子元素提取：从 PDF 提取 TextLine / ImageBlock / VectorLine。

关键设计：
- 统一坐标系：PyMuPDF 原生（原点左上，y 向下），与渲染图一致，便于可视化验证。
- paint_order（内容流绘制序号）：用 page.get_text("rawdict") 的 block 全局顺序获得。
  MuPDF 的 rawdict blocks 按内容流操作顺序产出（文本块 type=0 与图像块 type=1 统一编号），
  因此天然近似"绘制顺序"，用于阅读顺序 tiebreak 与"图文中断点"检测。
- 图像 xref：通过 get_image_info() 按 bbox 最近匹配，供资产导出。
- CID 字体标记：字体名含 CID/Type0 即标记，供下游编码兜底。

局限（文档化）：极少数文档的内容流经过 XObject 嵌套/Form 分层，rawdict 顺序可能与
严格绘制顺序有偏差；此类场景可换用 get_texttrace()（PyMuPDF>=1.26）增强，接口已预留。
"""
from __future__ import annotations

import itertools
from typing import Optional

from pdfparser.models import TextLine, ImageBlock, VectorLine

_BOLD_FLAG = 16
_ITALIC_FLAG = 2
# Computer Modern 系（LaTeX）靠字体名区分粗细，flags 无 bold 位
_BOLD_NAME_HINTS = ("bold", "black", "cmb", "hebo", "demi", "semibold")
_ITALIC_NAME_HINTS = ("italic", "oblique", "cmi", "cmsy", "cmex")


def _is_bold_by_name(font_name: str) -> bool:
    n = (font_name or "").lower()
    return any(h in n for h in _BOLD_NAME_HINTS)


def _is_italic_by_name(font_name: str) -> bool:
    n = (font_name or "").lower()
    return any(h in n for h in _ITALIC_NAME_HINTS)


def _color_int_to_tuple(color_int: int) -> tuple[float, float, float]:
    """PyMuPDF 的 color 为 sRGB int (0xRRGGBB)，转 (r,g,b) 0..1。"""
    if color_int is None:
        return (0.0, 0.0, 0.0)
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return (r / 255.0, g / 255.0, b / 255.0)


def _is_cid_font(font_name: str) -> bool:
    name = (font_name or "").lower()
    return "cid" in name or "type0" in name


class PageExtractor:
    """单页原子元素提取。"""

    def __init__(self, page, page_no: int):
        self.page = page
        self.page_no = page_no
        self._cached_image_infos = None

    # ------------------------------------------------------------------
    def extract(self) -> tuple[list[TextLine], list[ImageBlock], list[VectorLine]]:
        """返回 (text_lines, image_blocks, vector_lines)。"""
        lines: list[TextLine] = []
        images: list[ImageBlock] = []
        vectors: list[VectorLine] = []

        # 1) rawdict：文本行 + 图像块（统一 paint_order）
        raw = self.page.get_text("rawdict")
        for bi, block in enumerate(raw["blocks"]):
            bbox = tuple(block["bbox"])
            po = bi  # 全局绘制序号（含图像块）
            if block["type"] == 1:
                img = ImageBlock(bbox=bbox, xref=-1, paint_order=po, page=self.page_no)
                images.append(img)
                continue
            # 文本块：遍历行与 span（span 间隙拆分已回退：正文中英混排 span 边界
            # 与 MuPDF origin/bbox 不一致会导致正文行误拆，2026-08-15 实测）
            for li, line in enumerate(block.get("lines", [])):
                text_parts: list[str] = []
                font_name, font_size = "", 0.0
                bold, italic = False, False
                color = (0.0, 0.0, 0.0)
                best_len = 0
                for span in line.get("spans", []):
                    # rawdict 的 span 无 text 键，字符在 chars[].c
                    chars = span.get("chars", [])
                    span_text = "".join(ch.get("c", "") for ch in chars)
                    text_parts.append(span_text)
                    if len(span_text) > best_len:
                        best_len = len(span_text)
                        font_name = span.get("font", "")
                        font_size = span.get("size", 0.0)
                        flags = span.get("flags", 0)
                        bold = bool(flags & _BOLD_FLAG) or _is_bold_by_name(font_name)
                        italic = bool(flags & _ITALIC_FLAG) or _is_italic_by_name(font_name)
                        color = _color_int_to_tuple(span.get("color"))
                text = "".join(text_parts)
                if not text.strip():
                    continue
                lb = tuple(line["bbox"])
                lines.append(TextLine(
                    text=text,
                    bbox=lb,
                    font_name=font_name,
                    font_size=font_size,
                    bold=bold,
                    italic=italic,
                    color=color,
                    cid_font=_is_cid_font(font_name),
                    paint_order=po,
                    page=self.page_no,
                    block_idx=bi,
                    line_idx=li,
                ))

        # 2) 图像 xref 回填（按 bbox 最近匹配）
        self._match_image_xrefs(images)

        # 3) 矢量线（表格线等）
        vectors = self._extract_vectors()

        return lines, images, vectors

    # ------------------------------------------------------------------
    def _image_infos(self) -> list[dict]:
        if self._cached_image_infos is None:
            self._cached_image_infos = self.page.get_image_info(xrefs=True)
        return self._cached_image_infos

    def _match_image_xrefs(self, images: list[ImageBlock]) -> None:
        infos = self._image_infos()
        if not infos:
            return
        for img in images:
            best, best_dist = None, float("inf")
            for info in infos:
                ib = tuple(info["bbox"])
                # 中心距离
                cx = (ib[0] + ib[2]) / 2 - (img.bbox[0] + img.bbox[2]) / 2
                cy = (ib[1] + ib[3]) / 2 - (img.bbox[1] + img.bbox[3]) / 2
                d = cx * cx + cy * cy
                if d < best_dist:
                    best, best_dist = info, d
            if best is not None and best_dist < 25.0:
                img.xref = best.get("xref", -1)

    def _extract_vectors(self) -> list[VectorLine]:
        vectors: list[VectorLine] = []
        try:
            drawings = self.page.get_drawings()
        except Exception:  # noqa: BLE001
            return vectors
        for di, d in enumerate(drawings):
            items = d.get("items", [])
            for item in items:
                op = item[0]
                if op == "l":  # 直线
                    p1, p2 = item[1], item[2]
                    kind = "hline" if abs(p1.y - p2.y) < 0.8 else ("vline" if abs(p1.x - p2.x) < 0.8 else "curve")
                    bbox = (min(p1.x, p2.x), min(p1.y, p2.y), max(p1.x, p2.x), max(p1.y, p2.y))
                    vectors.append(VectorLine(kind=kind, bbox=bbox,
                                              paint_order=di, page=self.page_no))
                elif op == "re":  # 矩形（表格外框常用）
                    r = item[1]
                    bbox = (r.x0, r.y0, r.x1, r.y1)
                    vectors.append(VectorLine(kind="rect", bbox=bbox,
                                              paint_order=di, page=self.page_no))
        return vectors


class DocumentExtractor:
    """文档级 L1 提取。"""

    def __init__(self, doc, progress=None):
        self.doc = doc
        self.progress = progress

    def extract(self) -> tuple[list[TextLine], list[ImageBlock], list[VectorLine]]:
        all_lines: list[TextLine] = []
        all_images: list[ImageBlock] = []
        all_vectors: list[VectorLine] = []
        for pno in range(self.doc.page_count):
            if self.progress:
                self.progress(pno, self.doc.page_count)
            page = self.doc[pno]
            ex = PageExtractor(page, pno)
            lines, images, vectors = ex.extract()
            all_lines.extend(lines)
            all_images.extend(images)
            all_vectors.extend(vectors)
        return all_lines, all_images, all_vectors
