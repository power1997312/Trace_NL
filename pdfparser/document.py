"""DocumentParser：五层管道编排（L0 → L4）。

处理顺序（关键：表格区域先从正文流中"挖出"，再做标题/列表/阅读顺序，
保证表格不污染正文结构，也不丢失"表格打断段落"的信息）：
  L0 preprocess → L1 extract → L2 layout → L3c tables(carve)
  → L3a headings → L3b lists → L3 order → L3 tree/media → L4 output
"""
from __future__ import annotations

import os
from typing import Optional

from pdfparser.models import DocumentResult, OutlineItem
from pdfparser.preprocess import PdfPreprocessor
from pdfparser.extract import DocumentExtractor
from pdfparser.layout import LayoutAnalyzer
from pdfparser.tables import TableExtractor
from pdfparser.headings import HeadingAssigner
from pdfparser.lists import ListDetector
from pdfparser.order import ReadingOrderAssigner
from pdfparser.media import MediaAnchoring, ImageExporter


def _bbox_overlap(a, b, ratio: float = 0.3) -> bool:
    """a 与 b 的 IoU 或 a 被 b 覆盖比例超过 ratio 即认为重叠。"""
    ix0 = max(a[0], b[0])
    iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2])
    iy1 = min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / min(area_a, area_b) > ratio


class DocumentParser:
    """工程级 PDF 结构化提取主类。"""

    def __init__(self, pdf_path: str, prefer_camelot: bool = True,
                 asset_dir: Optional[str] = None):
        self.pdf_path = pdf_path
        self.prefer_camelot = prefer_camelot
        self.asset_dir = asset_dir or os.path.join(
            os.path.dirname(pdf_path), "assets")
        self.warnings: list[str] = []
        self.engine_used: list[str] = []

    # ------------------------------------------------------------------
    def parse(self, progress=None) -> DocumentResult:
        import fitz
        import pdfplumber

        doc = fitz.open(self.pdf_path)
        try:
            # ---------- L0 ----------
            pre = PdfPreprocessor()
            pres = pre.analyze(self.pdf_path)
            self.warnings.extend(pres.warnings)
            if not pres.has_text_layer:
                # 扫描件：返回报告（doc 由 finally 统一关闭）
                return DocumentResult(meta={"has_text_layer": False,
                                            "pages": pres.page_count,
                                            "scanned": True,
                                            "warnings": pres.warnings})
            # ---------- L1 ----------
            ex = DocumentExtractor(doc, progress=progress)
            lines, images, vectors = ex.extract()
            # ---------- L2 ----------
            layout = LayoutAnalyzer()
            blocks = layout.build_blocks(lines, images, vectors)
            # ---------- L3c 表格 ----------
            plumber_doc = pdfplumber.open(self.pdf_path)
            try:
                tex = TableExtractor(self.pdf_path, prefer_camelot=self.prefer_camelot)
                self.engine_used = tex.engine_used
                tables = tex.extract_all(doc, plumber_doc)
                tex.carve_blocks(blocks, tables)
                # 删除与结构图/矢量图 figure 块重叠的表格（gov 结构图常被误检为表）
                figures = [b for b in blocks if b.type == "figure"]
                table_blocks = [b for b in blocks if b.type == "table"]
                for tb in table_blocks:
                    if any(tb.page == fig.page and _bbox_overlap(tb.bbox, fig.bbox, ratio=0.3)
                           for fig in figures):
                        tb.type = "consumed"
                for t in tables:
                    self.warnings.extend(f"[表p{t.page_start + 1}] {w}" for w in t.warnings)
            finally:
                plumber_doc.close()
            # ---------- L3a 标题 ----------
            ha = HeadingAssigner()
            ha.assign_levels(blocks)
            # ---------- L3b 列表 ----------
            ld = ListDetector()
            ld.identify(blocks)
            # ---------- L3 阅读顺序 ----------
            ro = ReadingOrderAssigner()
            ro.assign(blocks)
            # ---------- L3 层级树 + 媒体 ----------
            roots = ha.build_tree(blocks)
            ma = MediaAnchoring()
            ma.anchor(blocks)
            # ---------- L4 ----------
            assets = ImageExporter().export(doc, blocks, self.asset_dir)
            # 区域渲染：figure 块（结构图/矢量图）渲染为 PNG，完整保留图
            ImageExporter().export_figures(doc, blocks, self.asset_dir)
            # 统计 figure 渲染资产
            n_figure_assets = sum(1 for b in blocks
                                  if b.type == "figure" and b.image_info
                                  and b.image_info.get("asset"))
            # 回填图片资产路径（Markdown 渲染用）
            for b in blocks:
                if b.type == "image" and b.image_info:
                    xref = b.image_info.get("xref", -1)
                    if xref in assets:
                        b.image_info["asset"] = assets[xref]
                    for m in b.media:
                        m["asset"] = assets.get(xref, "")
            result = DocumentResult(
                meta={
                    "file": os.path.basename(self.pdf_path),
                    "pages": pres.page_count,
                    "has_text_layer": True,
                    "text_chars": pres.text_char_count,
                    "engine": "pdfparser",
                    "table_engines": self.engine_used,
                    "assets_dir": self.asset_dir,
                    "n_assets": len(assets) + n_figure_assets,
                },
                outline=[OutlineItem(level=l, text=t, page=p)
                         for l, t, p in ha.outline(blocks)],
                body=roots,
                warnings=self.warnings,
            )
            return result
        finally:
            doc.close()
