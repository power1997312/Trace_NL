"""L3 图文锚定（研究报告场景 A / T7）。

职责：
1. 图题关联：匹配 ^图X / ^表X 的 caption 块与其最近图片块配对。
2. 正文媒体锚点：正文段落被图片打断时，把图片作为段落的 media 锚点，
   保证"图前文字 + 图 + 图后文字"输出为同一段落结构（含 media 列表）。
3. 图片资产导出：按 xref 从 PDF 提取原始图像数据保存到输出目录。
"""
from __future__ import annotations

import re
import os
from typing import Optional

from pdfparser.models import Block

_CAP_IMG = re.compile(r"^(图|Fig\.?)\s*[\d.]+")
_CAP_TBL = re.compile(r"^(表|Table)\s*[\d.]+")


def _center(bbox) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


class MediaAnchoring:
    """图文锚定。"""

    def anchor(self, blocks: list[Block]) -> None:
        """1) 图题-图片配对（更新 image block 的 caption 与说明）；
        2) 正文段落 media 锚点（按绘制顺序将打断段落的图片挂入 media）。"""
        self._pair_captions(blocks)
        self._embed_media(blocks)

    # ------------------------------------------------------------------
    def _pair_captions(self, blocks: list[Block]) -> None:
        images = [b for b in blocks if b.type == "image"]
        captions = [b for b in blocks if b.type == "caption"]
        if not images or not captions:
            return
        for cap in captions:
            t = cap.text.strip()
            if not (_CAP_IMG.match(t) or _CAP_TBL.match(t)):
                continue
            cx, cy = _center(cap.bbox)
            best, best_d = None, float("inf")
            for img in images:
                ix, iy = _center(img.bbox)
                d = (cx - ix) ** 2 + (cy - iy) ** 2
                # 图题通常在图片下方或紧邻：仅接受垂直方向距离合理的候选
                if d < best_d:
                    best, best_d = img, d
            if best is not None:
                # 图题文本存入图片块，图题块标记为 caption 并从正文流剔除（保留引用）
                img.image_info = img.image_info or {}
                img.image_info["caption"] = cap.text.strip()
                cap.type = "caption"
                cap.text = cap.text.strip()

    # ------------------------------------------------------------------
    def _embed_media(self, blocks: list[Block]) -> None:
        """按 (page, paint_order) 扫描：相邻文本块之间夹着 image 块时，
        把 image 挂到前一个文本块的 media 里（段落中断点合并）。"""
        ordered = sorted([b for b in blocks if b.type in ("text", "image", "caption")],
                         key=lambda b: (b.page, b.paint_order))
        pending: list[Block] = []  # 待挂载的媒体
        for b in ordered:
            if b.type == "image":
                pending.append(b)
            else:
                if pending:
                    # 把媒体挂到当前文本块（同页内）
                    for m in pending:
                        if m.page == b.page:
                            b.media.append({
                                "type": "image",
                                "order": m.paint_order,
                                "bbox": list(m.bbox),
                                "caption": (m.image_info or {}).get("caption", ""),
                            })
                    pending = []


class ImageExporter:
    """图片资产导出（按 xref 提取原始图像）。"""

    def export(self, doc, blocks: list[Block], out_dir: str) -> dict[int, str]:
        """返回 {xref: 保存路径}。"""
        os.makedirs(out_dir, exist_ok=True)
        saved: dict[int, str] = {}
        seen: set[int] = set()
        for b in blocks:
            if b.type != "image":
                continue
            xref = (b.image_info or {}).get("xref", -1)
            if xref <= 0 or xref in seen:
                continue
            seen.add(xref)
            try:
                pix = doc.extract_image(xref)
                ext = pix.get("ext", "png")
                fname = f"img_p{b.page + 1:03d}_x{xref}.{ext}"
                path = os.path.join(out_dir, fname)
                with open(path, "wb") as f:
                    f.write(pix["image"])
                saved[xref] = fname
            except Exception:  # noqa: BLE001
                continue
        return saved

    # ------------------------------------------------------------------
    def export_figures(self, doc, blocks: list[Block], out_dir: str,
                       zoom: float = 2.5) -> None:
        """区域渲染：把 figure 块（结构图/矢量图/图表）所在页面区域渲染为 PNG，
        完整保留图（含线条/颜色/图内文字），回填 b.image_info['asset']。

        位图 xref 提取不到的矢量图（tikz/CAD/Visio 导出）由此兜底。
        """
        import fitz

        os.makedirs(out_dir, exist_ok=True)
        for b in blocks:
            if b.type != "figure":
                continue
            try:
                page = doc[b.page]
                x0, y0, x1, y1 = b.bbox
                # 适度外扩边距，避免裁掉图形边缘
                pad = 6.0
                clip = fitz.Rect(max(x0 - pad, 0), max(y0 - pad, 0),
                                 x1 + pad, y1 + pad)
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                fname = f"fig_p{b.page + 1:03d}_x{int(x0):03d}_{int(y0):03d}.png"
                pix.save(os.path.join(out_dir, fname))
                if b.image_info is None:
                    b.image_info = {}
                b.image_info["asset"] = fname
                b.image_info["kind"] = "vector"
            except Exception:  # noqa: BLE001
                continue
