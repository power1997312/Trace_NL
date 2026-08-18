"""L0 预处理：文档类型判别、加密/损坏检测、规范化。

核心职责：
1. 判别文档是"文本型"（有真实文本层，可解析）还是"扫描型"（纯图像，需 OCR）。
2. 检测加密/权限受限，给出明确报告。
3. 为下游提供统一入口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PreprocessResult:
    has_text_layer: bool = False       # 是否含可提取文本层
    page_count: int = 0
    encrypted: bool = False            # 是否加密
    needs_password: bool = False
    text_char_count: int = 0           # 抽样字符总数
    scanned_pages: list[int] = field(default_factory=list)  # 判为扫描的页
    warnings: list[str] = field(default_factory=list)
    engine: str = "pymupdf"


class PdfPreprocessor:
    """L0 预处理。"""

    # 文本型判别的字符密度阈值（字符数/页）。低于该值判定为扫描页。
    TEXT_CHARS_PER_PAGE_MIN = 60
    # 抽样页数
    SAMPLE_PAGES = 10

    def __init__(self, min_chars_per_page: int | None = None,
                 sample_pages: int | None = None):
        if min_chars_per_page is not None:
            self.TEXT_CHARS_PER_PAGE_MIN = min_chars_per_page
        if sample_pages is not None:
            self.SAMPLE_PAGES = sample_pages

    def analyze(self, path: str) -> PreprocessResult:
        import fitz  # PyMuPDF

        res = PreprocessResult()
        try:
            doc = fitz.open(path)
        except Exception as e:  # noqa: BLE001
            res.warnings.append(f"无法打开 PDF: {e}")
            return res

        res.page_count = doc.page_count
        res.encrypted = doc.needs_pass if hasattr(doc, "needs_pass") else False
        if res.encrypted:
            res.needs_password = True
            res.warnings.append("文档已加密，需密码解密后才能解析")
            doc.close()
            return res

        # 文本层判别：抽样若干页统计可提取字符数
        total_pages = doc.page_count
        step = max(1, total_pages // self.SAMPLE_PAGES) if total_pages > self.SAMPLE_PAGES else 1
        sample_idx = list(range(0, total_pages, step))[: self.SAMPLE_PAGES]

        chars_total = 0
        for pno in sample_idx:
            page = doc[pno]
            txt = page.get_text("text")
            n = len(txt.strip())
            chars_total += n
            if n < self.TEXT_CHARS_PER_PAGE_MIN:
                res.scanned_pages.append(pno + 1)  # 1-based 页码

        res.text_char_count = chars_total
        # 判定：抽样页中文本页占比
        text_pages = len(sample_idx) - len(res.scanned_pages)
        ratio = text_pages / len(sample_idx) if sample_idx else 0.0
        res.has_text_layer = ratio >= 0.7 and chars_total > 0

        if not res.has_text_layer:
            res.warnings.append(
                "判定为扫描型/图像型 PDF（无可提取文本层），需 OCR 通道处理"
            )

        doc.close()
        return res
