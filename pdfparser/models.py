"""数据模型：五层管道的核心数据结构。

设计原则：所有元素都保留 (bbox, page, paint_order) 三维定位信息，
保证下游可以随时回溯到 PDF 原始几何与绘制顺序。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# L1 原子元素
# ---------------------------------------------------------------------------
@dataclass
class TextLine:
    """一行文本（含逐字符统计信息）。"""
    text: str
    bbox: tuple[float, float, float, float]          # (x0, y0, x1, y1)，原点左下
    font_name: str = ""
    font_size: float = 0.0
    bold: bool = False
    italic: bool = False
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    cid_font: bool = False                            # 是否为 CID 复合字体（中文常见）
    paint_order: int = -1                             # 内容流绘制序号
    page: int = 0
    block_idx: int = -1
    line_idx: int = -1

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def y0(self) -> float: return self.bbox[1]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def y1(self) -> float: return self.bbox[3]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("line_idx", None)
        return d


@dataclass
class ImageBlock:
    """页面上的图像对象。"""
    bbox: tuple[float, float, float, float]
    xref: int = -1                                    # 图像对象引用（资产导出用）
    paint_order: int = -1
    page: int = 0
    caption_bbox: Optional[tuple[float, float, float, float]] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VectorLine:
    """矢量线条（表格线/装饰线）。"""
    kind: str = "hline"                               # hline | vline | curve
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    paint_order: int = -1
    page: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# L2 版面块
# ---------------------------------------------------------------------------
@dataclass
class Block:
    """版面块：L2 的产物，L3 在其上重建结构。"""
    type: str = "text"        # text | heading | list | table | image | caption | header_footer
    level: int = 0            # heading 层级（1..N）
    text: str = ""
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    page: int = 0
    paint_order: int = -1
    order: int = -1           # 阅读顺序（L3 赋值）
    lines: list[TextLine] = field(default_factory=list)
    media: list[dict] = field(default_factory=list)   # 段落内嵌媒体锚点 [{type,bbox,order}]
    table: Optional["TableData"] = None               # type==table 时有效
    children: list["Block"] = field(default_factory=list)  # 层级栈归属的子块
    list_items: Optional[list[dict]] = None           # type==list 时的列表树
    image_info: Optional[dict] = None                 # type==image 时有效
    font_sig: Optional[tuple] = None                  # 字体签名 (name,size,bold)

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def y0(self) -> float: return self.bbox[1]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def y1(self) -> float: return self.bbox[3]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lines"] = [ln.to_dict() for ln in self.lines]
        if self.table is not None:
            d["table"] = self.table.to_dict()
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


# ---------------------------------------------------------------------------
# L3 表格
# ---------------------------------------------------------------------------
@dataclass
class TableCell:
    text: str = ""
    rowspan: int = 1
    colspan: int = 1
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TableData:
    """重建后的表格：rows 为二维单元格网格。"""
    rows: list[list[TableCell]] = field(default_factory=list)
    header_rows: int = 0            # 表头行数（跨页接续时用于去重）
    page_start: int = 0
    page_end: int = 0
    flavor: str = "lattice"         # lattice | stream
    engine: str = ""                # camelot | pdfplumber | custom
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    warnings: list[str] = field(default_factory=list)
    columns: list[float] = field(default_factory=list)  # 列边界 x 坐标

    @property
    def n_rows(self) -> int: return len(self.rows)
    @property
    def n_cols(self) -> int: return len(self.rows[0]) if self.rows else 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [[c.to_dict() for c in row] for row in self.rows]
        d["n_rows"] = self.n_rows
        d["n_cols"] = self.n_cols
        return d


# ---------------------------------------------------------------------------
# L4 文档级输出
# ---------------------------------------------------------------------------
@dataclass
class OutlineItem:
    level: int = 1
    text: str = ""
    page: int = 0
    order: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentResult:
    """管道最终产物。"""
    meta: dict[str, Any] = field(default_factory=dict)
    outline: list[OutlineItem] = field(default_factory=list)
    body: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "outline": [o.to_dict() for o in self.outline],
            "body": [b.to_dict() for b in self.body],
            "warnings": self.warnings,
        }
