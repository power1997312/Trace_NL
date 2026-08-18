"""pdfparser —— 工程级 PDF 结构化提取工具（不依赖 OCR/大模型）。

五层管道：L0 预处理 → L1 原子提取 → L2 版面分析 → L3 结构重建 → L4 输出。
"""
from pdfparser.models import (
    TextLine, ImageBlock, VectorLine, Block,
    TableCell, TableData, OutlineItem, DocumentResult,
)
from pdfparser.document import DocumentParser

__version__ = "0.1.0"
__all__ = [
    "TextLine", "ImageBlock", "VectorLine", "Block",
    "TableCell", "TableData", "OutlineItem", "DocumentResult",
    "DocumentParser",
]
