"""L2 版面分析：行→段→块合并、块分类、列检测、页眉页脚识别。

流程：
1. 行聚类成段（垂直间距 ≤ 1.6×行高 且 水平起点接近）。
2. 段落 → Block（text）。
3. 初步分类：caption（图题/表题）、header_footer（跨页重复）、其余为 text。
   标题判定（heading）在 L3 完成——需要全文档字体统计，属 headings 模块职责。
4. 列检测：X 轴空白带聚类（多栏文档）。
5. 页眉页脚：跨页重复文本识别并从正文剔除。
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from pdfparser.models import TextLine, ImageBlock, VectorLine, Block

# 段合并阈值：垂直间距 / 行高
PARA_GAP_RATIO = 1.6
# 页眉页脚：出现页数占比达到该值判为重复装饰
HF_MIN_RATIO = 0.5
# 图题/表题正则
CAPTION_PREFIXES = ("图", "表", "Figure", "Fig.", "Table", "TABLE", "FIGURE", "Fig")
CAPTION_NUM_HINT = ("图", "表", "Fig", "Table", "Figure")


def _is_caption(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 120:
        return False
    for pre in CAPTION_PREFIXES:
        if t.startswith(pre):
            # 要求带编号特征：图1 / 图 1-1 / 表2.1
            rest = t[len(pre):].lstrip()
            return rest[:1].isdigit() or rest[:1] in "0123456789"
    return False


class LayoutAnalyzer:
    """L2 版面分析。"""

    def __init__(self, para_gap_ratio: float = PARA_GAP_RATIO,
                 hf_min_ratio: float = HF_MIN_RATIO):
        self.para_gap_ratio = para_gap_ratio
        self.hf_min_ratio = hf_min_ratio

    # ------------------------------------------------------------------
    def build_blocks(self, lines: list[TextLine],
                     images: list[ImageBlock],
                     vectors: list[VectorLine]) -> list[Block]:
        """行→段→块。返回 Block 列表（按页、按绘制顺序）。

        页眉/页脚识别在【行级】完成（跨 ≥3 页重复的相同短行）：
        页眉行常与下方标题行在段合并中粘连，块级识别会失效。
        """
        blocks: list[Block] = []
        by_page: dict[int, list[TextLine]] = {}
        for ln in lines:
            by_page.setdefault(ln.page, []).append(ln)

        # 行级页眉/页脚：跨页重复的短行
        # 注意: 纯符号行("-"/"•"/"·"等)是列表项标记, 不是页眉页脚, 需排除
        # (否则每页的列表项"-"被误判为 header_footer, 导致列表项前缀丢失)
        import re
        _pure_symbol = re.compile(r"^[\s\-–—•·●○■□►◆◇★☆✦✧*＊·・，。、；;：:！!？?（）()\[\]【】《》<>/|\\]+$")
        # 记录每个候选短行的出现位置(y0), 用于区分"真页眉页脚"(每页位置固定)
        # 与"跨页重复的正文列表项"(如 3.2.2.1/3.2.2.2 共有的"在役检查和定期试验；",
        # 各页 y0 差异大)。仅按出现次数判定会把后者误判为页眉页脚而剔除。
        from collections import defaultdict
        pos_by_text: dict[str, list[float]] = defaultdict(list)
        for ln in lines:
            t = ln.text.strip()
            if t and len(t) <= 60 and not _pure_symbol.match(t):
                pos_by_text[t].append(ln.y0)
        self._hf_lines = set()
        for t, ys in pos_by_text.items():
            if len(ys) < max(3, int(len(by_page) * 0.35)):
                continue
            # 页眉/页脚在每页的 y0 应接近(差异 ≤ 20pt);
            # 列表项跨页重复则 y0 差异大, 不判为页眉页脚。
            if max(ys) - min(ys) <= 20.0:
                self._hf_lines.add(t)

        for pno in sorted(by_page):
            page_lines = sorted(by_page[pno], key=lambda l: (l.paint_order, l.y0, l.x0))
            blocks.extend(self._cluster_paragraphs(page_lines, pno))

        # 图片块
        for img in images:
            blocks.append(Block(
                type="image",
                bbox=img.bbox,
                page=img.page,
                paint_order=img.paint_order,
                image_info={"xref": img.xref, "bbox": img.bbox},
            ))

        # 分类 + 页码识别
        self._classify(blocks)
        # 图片内文字图层吸附（CAD/Visio 位图+独立文字图层结构）
        self._absorb_image_labels(blocks)
        # 结构图/矩阵图区域检测（图内短文本不当作正文段落）
        self._detect_figure_regions(blocks, vectors)
        # 矢量图检测（tikz/pgfplots 绘制的图无位图 xref，靠图题上方矢量密集区域识别）
        self._detect_vector_figures(blocks, vectors)
        # 跨页段落合并（中文公文常见：一行被页中断开）
        self._merge_cross_page_paragraphs(blocks)

        # 排序：按 (page, paint_order)
        blocks.sort(key=lambda b: (b.page, b.paint_order))
        return blocks

    # ------------------------------------------------------------------
    def _absorb_image_labels(self, blocks: list[Block]) -> None:
        """图片内文字图层吸附（CAD/Visio 位图+独立文字图层结构）。

        CAD 软件（Visio/AutoCAD Electrical）导出 PDF 时采用"位图渲染 + 独立文字
        图层"模式：图形主体是位图(image)，文字标签是叠加的独立 TextLine。这些
        文字标签通常与 image bbox 相交或紧邻（在图内/图框边缘），但被 PyMuPDF
        提取为独立 text 块。

        本方法将 bbox 与 image 块相交或紧邻(≤30pt)的 text 块吸附进 image 块：
        - 文字并入 image.text（不再作为正文段落输出）
        - 标记 absorbed，正文提取时跳过
        - 图片资产仍由 media 模块正常导出

        过滤条件（防止误吸正文）：
        - 短文本 ≤40 字符（图内标签通常很短；长正文不吸附）
        - 与 image bbox 相交 或 中心距 ≤30pt（紧邻图框）
        - 不吸附以编号开头/以句子标点结尾的正文行（如列表项、条目ID）
        - 若某 text 块同时靠近多个 image，只吸附一次
        """
        images = [b for b in blocks if b.type == "image"]
        if not images:
            return
        absorbed: set[int] = set()
        for img in images:
            ix0, iy0, ix1, iy1 = img.bbox
            img_area = (ix1 - ix0) * (iy1 - iy0)
            if img_area <= 0:
                continue
            icx, icy = (ix0 + ix1) / 2, (iy0 + iy1) / 2
            labels: list[str] = []
            for b in blocks:
                if b.type != "text" or id(b) in absorbed:
                    continue
                if b.page != img.page:
                    continue
                flat = b.text.replace("\n", "").strip()
                # 仅短标签（图内文字一般较短）；编号/条目行/长正文不吸附
                if not (1 <= len(flat) <= 40):
                    continue
                if flat and flat[-1] in "。；！？":
                    continue  # 完整句子结尾 → 正文，非图标签
                # 位置判定：相交 或 中心距 ≤35pt（紧邻图框/图内间隙标签）
                overlap = (b.x0 <= ix1 and b.x1 >= ix0
                           and b.y0 <= iy1 and b.y1 >= iy0)
                if not overlap:
                    tcx, tcy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
                    dist = ((tcx - icx) ** 2 + (tcy - icy) ** 2) ** 0.5
                    if dist > 35.0:
                        # 大字号图内主标题（比普通标签大，位于图内中心）：
                        # 要求 y 轴与 image 重叠/极近（真图内标签与图在同一垂直带），
                        # 且距图≤70pt；排除图片上方/下方的章节标题（y 不重叠）
                        fsize = b.font_sig[1] if b.font_sig else 0
                        y_overlap = not (b.y1 < iy0 - 5.0 or b.y0 > iy1 + 5.0)
                        if not (fsize >= 10.0 and dist <= 70.0 and y_overlap):
                            continue
                labels.append(flat)
                absorbed.add(id(b))
            if labels:
                # 合并到 image 块文字
                prev_text = img.text or ""
                merged = " / ".join([prev_text] + labels) if prev_text else " / ".join(labels)
                img.text = merged
        # 被吸附的 text 块标记为 consumed（正文提取时跳过）
        for b in blocks:
            if id(b) in absorbed:
                b.type = "consumed"

    # ------------------------------------------------------------------
    def _detect_vector_figures(self, blocks: list[Block],
                               vectors: list[VectorLine]) -> None:
        """矢量图（LaTeX tikz/pgfplots 曲线图）识别：图题(Figure N/图N)上方
        矢量线密集区域且无位图 image → 生成 figure 块。

        若 caption 附近已由结构图检测生成 figure 块，则不再把 caption 单独
        转成 figure，避免同一张图输出两次。
        """
        import re
        fig_cap = re.compile(r"^(Figure|Fig\.?|图)\s*\d")
        existing_figs = [bl for bl in blocks if bl.type == "figure"]
        for b in blocks:
            if b.type != "caption" or not fig_cap.match(b.text.strip()):
                continue
            x0, y0, x1, y1 = b.bbox
            h = max(y1 - y0, 10.0)
            up_y0 = y0 - h * 9
            # 已有结构图 figure 覆盖本 caption 上方区域 → 跳过
            if any(f.page == b.page
                   and f.x0 <= x1 and f.x1 >= x0
                   and f.y0 <= y0 and f.y1 >= up_y0
                   for f in existing_figs):
                continue
            region = [v for v in vectors
                      if v.page == b.page
                      and v.bbox[1] >= up_y0 and v.bbox[3] <= y0 + 2
                      and v.bbox[0] >= x0 - 40 and v.bbox[2] <= x1 + 40]
            if len(region) < 15:
                continue
            has_img = any(bl.type == "image" and bl.page == b.page
                          and bl.y0 >= up_y0 and bl.y1 <= y0 + 2
                          for bl in blocks)
            if not has_img:
                b.type = "figure"
                b.text = f"[矢量图] {b.text.strip()}"

    # ------------------------------------------------------------------
    def _detect_figure_regions(self, blocks: list[Block],
                               vectors: list[VectorLine]) -> None:
        """结构图/矩阵图检测：同页 ≥12 个短文本块(≤10字符)且构成 ≥4 个规整行带
        → 判定为"图内文字"区域，合并为一个 figure 块（不入正文段落流）。

        gov 数据标准体系结构图即此类：AA术语/GA数据基础设施安全 等网格排布短块。
        关键修复：
        - 结构图内的单字标签常被行级页眉页脚误识别为 header_footer，需一并吸附；
        - 结构图区域可能覆盖位图 image，需吞并避免 caption 重复渲染；
        - 放宽 bbox 容差，避免边缘标签遗漏；
        - 有线表格（含大量横竖线）不是结构图，需排除。
        """
        by_page: dict[int, list[Block]] = {}
        import re
        _num_head = re.compile(r"^(\d+[.、)）]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[)）])")
        # 纯符号/纯标点短块（如 "-"、•、·）：列表项符号/装饰，不是结构图标签
        _pure_symbol = re.compile(r"^[\s\-–—•·●○■□►◆◇★☆✦✧*＊·・，。、；;：:！!？?（）()\[\]【】《》<>/|\\]+$")
        # 候选块：text 或 header_footer（单字标签被误标为页眉页脚），长度 1-10
        def _is_short_candidate(b: Block) -> bool:
            if b.type not in ("text", "header_footer"):
                return False
            flat = b.text.replace("\n", "").strip()
            if not (1 <= len(flat) <= 10):
                return False
            if _pure_symbol.match(flat):
                return False  # 纯符号（列表标记/装饰线）不是图标签
            if _num_head.match(flat):
                return False
            if _is_caption(b.text):
                return False
            return True

        # 同行长文本块索引（判断短块是否为正文行碎片）
        long_by_page: dict[int, list[Block]] = {}
        for b in blocks:
            if b.type == "text" and len(b.text) > 30:
                long_by_page.setdefault(b.page, []).append(b)

        for b in blocks:
            if not _is_short_candidate(b):
                continue
            flat = b.text.replace("\n", "").strip()
            # 排除正文行碎片：同行(y 带)存在长文本块 → 短块是正文断行而非图标注
            is_frag = any(abs(lb.y0 - b.y0) < 12 and abs(lb.x0 - b.x0) < 60
                          for lb in long_by_page.get(b.page, []))
            if not is_frag:
                by_page.setdefault(b.page, []).append(b)

        for page, sblocks in by_page.items():
            if len(sblocks) < 12:
                continue
            # 中文占比判别：结构图标签以中文为主；英文表格单元格（18-layer 等）无 CJK → 跳过
            cjk_blocks = sum(1 for b in sblocks
                             if any('\u4e00' <= ch <= '\u9fff' for ch in b.text))
            if cjk_blocks / len(sblocks) < 0.5:
                continue
            rows: dict[int, list[Block]] = {}
            for b in sblocks:
                rows.setdefault(round(b.y0 / 15), []).append(b)
            if len(rows) < 4:
                continue
            # 表格过滤：连续多个行带具有相同块数（≥3）是真表格网格；
            # 结构图的行带块数通常不规则或多为单字竖排。
            # 但若候选区域上下存在 "图X" 图题，说明是结构图而非表格，保留。
            import re
            fig_cap_re = re.compile(r"^图\s*\d")
            y0 = min(b.y0 for b in sblocks)
            y1 = max(b.y1 for b in sblocks)
            x0 = min(b.x0 for b in sblocks)
            x1 = max(b.x1 for b in sblocks)
            # 页面覆盖守卫：候选区域高度占页面主体高度过大 → 是正文页而非结构图
            # （真实结构图只占页面局部；正文页的短行碎片会散布全页，如列表项"-"）
            page_blocks = [b for b in blocks if b.page == page]
            if page_blocks:
                page_y0 = min(b.y0 for b in page_blocks)
                page_y1 = max(b.y1 for b in page_blocks)
                page_h = max(page_y1 - page_y0, 1.0)
                region_h = y1 - y0
                # 候选区域覆盖页面 70% 以上高度 → 判定为正文页，跳过
                if region_h > 0.7 * page_h:
                    continue
            has_fig_caption = any(
                b.type == "caption" and b.page == page
                and fig_cap_re.match(b.text.strip())
                and b.y0 >= y0 - 90 and b.y1 <= y1 + 90
                and b.x0 >= x0 - 60 and b.x1 <= x1 + 60
                for b in blocks)
            if not has_fig_caption:
                sorted_counts = [len(rows[k]) for k in sorted(rows)]
                consecutive_same = 1
                max_consecutive = 1
                for i in range(1, len(sorted_counts)):
                    if sorted_counts[i] == sorted_counts[i - 1] and sorted_counts[i] >= 3:
                        consecutive_same += 1
                        max_consecutive = max(max_consecutive, consecutive_same)
                    else:
                        consecutive_same = 1
                if max_consecutive >= 3:
                    continue
            # 初始 bbox（来自候选短块）
            y0 = min(b.y0 for b in sblocks)
            y1 = max(b.y1 for b in sblocks)
            x0 = min(b.x0 for b in sblocks)
            x1 = max(b.x1 for b in sblocks)
            # 有线表格排除：区域内横竖线密集（无论中英文）→ 是真表格而非结构图
            # （表格线可能略超出文字 bbox，因此用相交判断）
            # 原实现仅在 cjk_ratio < 0.45 时检查线格；中文表格因空单元格使行带
            # 块数不齐、逃过上方"连续同块数"过滤，整表被误判为结构图而吞并
            # （sample_b 回归）。改为"无图题的区域一律检查线格"，有图题(图X)
            # 的结构图区域保持豁免。
            if not has_fig_caption:
                hlines = [v for v in vectors
                          if v.kind == "hline" and v.page == page
                          and v.bbox[0] <= x1 + 10 and v.bbox[2] >= x0 - 10
                          and v.bbox[1] >= y0 - 15 and v.bbox[3] <= y1 + 15]
                vlines = [v for v in vectors
                          if v.kind == "vline" and v.page == page
                          and v.bbox[0] >= x0 - 15 and v.bbox[2] <= x1 + 15
                          and v.bbox[1] <= y1 + 10 and v.bbox[3] >= y0 - 10]
                if len(hlines) >= 3 and len(vlines) >= 3:
                    continue
            # 放宽容差吸附边缘标签
            x_tol, y_tol = 25.0, 25.0
            # 核心短标签（长度 1-10）用于确认结构图区域
            inside_core = [b for b in blocks
                           if b.type in ("text", "header_footer") and b.page == page
                           and 1 <= len(b.text.replace("\n", "").strip()) <= 10
                           and not _num_head.match(b.text.replace("\n", "").strip())
                           and not _is_caption(b.text)
                           and b.y0 >= y0 - y_tol and b.y1 <= y1 + y_tol
                           and b.x0 >= x0 - x_tol and b.x1 <= x1 + x_tol]
            if len(inside_core) < 12:
                continue
            # 扩展 bbox 后，吸附 figure 区域内所有非 caption 文本块（包括较长
            # 的结构图说明文字，如 "B 数据基础设施 C 数据资源 E 数据流通"）
            inside = [b for b in blocks
                      if b.type in ("text", "header_footer") and b.page == page
                      and not _is_caption(b.text)
                      and not _num_head.match(b.text.replace("\n", "").strip())
                      and b.y0 >= y0 - y_tol and b.y1 <= y1 + y_tol
                      and b.x0 >= x0 - x_tol and b.x1 <= x1 + x_tol]
            # 吞并 figure bbox 内的位图 image，避免 caption 再次渲染同一张图
            swallowed_images = [b for b in blocks
                                if b.type == "image" and b.page == page
                                and b.x0 >= x0 - x_tol and b.x1 <= x1 + x_tol
                                and b.y0 >= y0 - y_tol and b.y1 <= y1 + y_tol]
            labels = " / ".join(b.text.replace("\n", "") for b in inside)
            # 扩展 bbox 包含 image（完整保留图）
            all_x0 = [b.x0 for b in inside] + [b.x0 for b in swallowed_images]
            all_y0 = [b.y0 for b in inside] + [b.y0 for b in swallowed_images]
            all_x1 = [b.x1 for b in inside] + [b.x1 for b in swallowed_images]
            all_y1 = [b.y1 for b in inside] + [b.y1 for b in swallowed_images]
            fig = Block(type="figure", text=labels,
                        bbox=(min(all_x0), min(all_y0), max(all_x1), max(all_y1)),
                        page=page,
                        paint_order=min(b.paint_order for b in inside))
            for b in inside:
                blocks.remove(b)
            for b in swallowed_images:
                blocks.remove(b)
            blocks.append(fig)

    # ------------------------------------------------------------------
    def _cluster_paragraphs(self, lines: list[TextLine], page: int) -> list[Block]:
        """行聚类成段。

        断段条件（任一命中即断）：
        - 垂直间距 > ratio×行高（空行/大间隔）；
        - 水平起点偏移 > 12pt（缩进/对齐变化）；
        - 字号突变 > 1.5pt（标题与正文的字号差异信号）。
        font_sig 用众数字号（避免混合块平均值失真）。
        """
        from collections import Counter
        import re
        blocks: list[Block] = []
        cur_lines: list[TextLine] = []
        prev: Optional[TextLine] = None

        def flush():
            nonlocal cur_lines
            if not cur_lines:
                return
            text = "\n".join(l.text for l in cur_lines)
            x0 = min(l.x0 for l in cur_lines)
            y0 = min(l.y0 for l in cur_lines)
            x1 = max(l.x1 for l in cur_lines)
            y1 = max(l.y1 for l in cur_lines)
            sizes = Counter(round(l.font_size, 1) for l in cur_lines)
            dom_size = sizes.most_common(1)[0][0]
            blocks.append(Block(
                type="text",
                text=text,
                bbox=(x0, y0, x1, y1),
                page=page,
                paint_order=cur_lines[0].paint_order,
                lines=cur_lines,
                font_sig=(cur_lines[0].font_name, dom_size,
                          cur_lines[0].bold),
            ))
            cur_lines = []

        for ln in lines:
            # 页眉/页脚行（跨页重复）→ 独立块
            if getattr(self, "_hf_lines", None) and ln.text.strip() in self._hf_lines:
                flush()
                blocks.append(Block(
                    type="header_footer", text=ln.text.strip(),
                    bbox=ln.bbox, page=page, paint_order=ln.paint_order,
                    lines=[ln], font_sig=(ln.font_name, round(ln.font_size, 1), ln.bold)))
                prev = None
                continue
            # Bullet 符号行（Wingdings / Symbol / dingbat 单字符）→ 强制断段。
            # 原实现会把 \uf06c 这类 symbol 与下一行 SimSun 文本合并成同一段，
            # 导致 bullet 标记被 ListDetector 提取后丢失，整段 7 行被拼成单段。
            # 修复（2026-08-18，DCS设备技术规格书 3.2.2.1/3.2.2.2）：symbol 行
            # 与后续内容文本行分属两个 block —— symbol 行作为独立 list bullet marker，
            # 内容文本作为独立 list item body，下游适配器分别渲染。
            if (ln.font_name and ln.font_name not in ("SimSun", "TimesNewRomanPSMT", "")
                    and len(ln.text.strip()) <= 3):
                flush()
                cur_lines.append(ln)
                prev = ln
                flush()  # symbol 单独成段,后续内容文本开启新段
                prev = None
                continue
            # 图题/表题行（短行）独占一段：先关段，再单独成段
            if _is_caption(ln.text) and len(ln.text) < 150:
                flush()
                cur_lines.append(ln)
                prev = ln
                flush()
                prev = None
                continue
            # 标题行断段：编号标题（3.1 / 3.3.2 / 第X章 / 附录A）→ 强制独立成段
            # （防止标题与上一段正文或下一段正文粘连）
            t_now = ln.text.strip()
            if t_now and (
                    re.match(r"^\d+(\.\d+){1,3}\s+[\u4e00-\u9fffA-Za-z]", t_now)
                    or re.match(r"^第[一二三四五六七八九十百\d零]+[章节部分篇讲]\s*[\u4e00-\u9fff]", t_now)
                    or re.match(r"^附录\s*[A-Za-z\d]", t_now)):
                flush()
                cur_lines.append(ln)
                prev = ln
                flush()
                prev = None
                continue
            # 短标题行断段：prev 短行(≤25字符)且非句子标点结尾 + 当前行更长
            # → prev 是无编号标题（如 "系统架构设计要求"），断段独立
            if (prev is not None and prev.text
                    and len(prev.text.strip()) <= 25
                    and prev.text.strip()[-1] not in "。；！？，、：:"
                    and len(t_now) > 25):
                flush()
                cur_lines.append(ln)
                prev = ln
                continue
            if prev is not None:
                gap = ln.y0 - prev.y1
                line_h = max(prev.font_size, 1.0) * 1.2
                # 水平对齐容差 32pt：覆盖中文公文"首行缩进 2 字符"(~30pt) 段首行场景
                same_col = abs(ln.x0 - prev.x0) < 32.0
                size_jump = abs(ln.font_size - prev.font_size) > 1.2
                # 智能字体断段：短行 + 字体族突变 + 行距足够（中文文档标题用楷体/黑体，
                # 与正文同字号；但 LaTeX 长正文行字体频繁切换，须限定短行+行距）
                # 例外：中文列举引导句 "一是/二是/三是..." 后接正文，虽字体族不同
                #（引导句常为楷体，正文为仿宋），仍应视为同一段落。
                _CN_ENUM_START = re.compile(r"^(一是|二是|三是|四是|五是|六是|七是|八是|九是|十是|第一|第二|第三|第四|第五|第六|第七|第八|第九|第十|首先|其次|再次|最后)")
                prev_is_enum = bool(prev.text and _CN_ENUM_START.match(prev.text.strip()))
                font_change = (ln.font_name != prev.font_name
                               and abs(ln.font_size - prev.font_size) <= 1.2
                               and len(ln.text) <= 80
                               and gap > 0.4 * line_h
                               and not prev_is_enum)
                if gap <= self.para_gap_ratio * line_h and same_col and not size_jump \
                        and not font_change:
                    cur_lines.append(ln)
                    prev = ln
                    continue
                flush()
            cur_lines.append(ln)
            prev = ln
        flush()
        return blocks

    # ------------------------------------------------------------------
    def _classify(self, blocks: list[Block]) -> None:
        import re
        page_no_re = re.compile(r"^(第\s*\d+\s*页|-?\d+\s*/\s*\d+|\d+|[-–—]\s*\d+\s*[-–—])$")
        # 正文引用句（"Fig. 6 (middle) shows the behaviors..."）排除在图题之外
        fig_ref_re = re.compile(
            r"^(Fig\.?|Figure|图)\s*\d+\b[^.]*?\b(shows|illustrates|presents|"
            r"displays|depicts|reports|plots|compares|我们|示|给出)\b", re.I)
        for b in blocks:
            if b.type != "text":
                continue
            # 页码行（短单行 + 页码模式）
            t = b.text.strip()
            if len(b.lines) == 1 and len(t) <= 12 and page_no_re.match(t):
                b.type = "header_footer"
                continue
            # caption：首行图题模式 + 短小（首行≤80、≤2 行、非正文引用句）
            first = b.text.split("\n")[0]
            if (len(b.lines) <= 2 and len(first) <= 80
                    and not fig_ref_re.match(first)
                    and _is_caption(first)):
                b.type = "caption"

    # ------------------------------------------------------------------
    def _merge_cross_page_paragraphs(self, blocks: list[Block]) -> None:
        """合并被分页切断的同一中文段落。

        PDF 内容流按页切割，一个中文段落可能在页末以未完整词结尾（如 '评'），
        下一页首行以续字开头（如 '估'）。按页聚类会错误拆成两段，此处按
        字体/坐标/文本连续性做后处理合并。

        修复（2026-08-18，DCS设备技术规格书 第1页污染问题）：
        1. 循环入口检查 j/id(ordered[j]) 是否已被合并 —— 原实现只标记 b 但未在
           循环入口跳过，导致下一页首块被前一页【多个】 text 块重复合并
           （第1页"质保等级:QA1"被注入 5 次的根因）。
        2. 增加页面几何边界约束 —— 仅当 a 位于本页底部（y1 贴近本页最大 y1）、
           b 位于下一页顶部（y0 贴近下一页最小 y0）时才合并，避免把页面中部
           的标题/段落与下一页首块错误合并（原实现只要字体/文本连续就合并）。
        """
        _END_SENTENCE = set("。！？")
        ordered = sorted(blocks, key=lambda b: (b.page, b.paint_order))
        # 建立每页第一个/最后一个正文块索引
        first_text_by_page: dict[int, int] = {}
        last_text_by_page: dict[int, int] = {}
        for idx, b in enumerate(ordered):
            if b.type in ("text", "paragraph"):
                first_text_by_page.setdefault(b.page, idx)
                last_text_by_page[b.page] = idx

        # 每页正文块几何范围（用于跨页边界判定；header_footer 已剔除，不会干扰）
        page_y0_min: dict[int, float] = {}
        page_y1_max: dict[int, float] = {}
        for b in blocks:
            if b.type not in ("text", "paragraph"):
                continue
            page_y0_min[b.page] = min(page_y0_min.get(b.page, float("inf")), b.y0)
            page_y1_max[b.page] = max(page_y1_max.get(b.page, float("-inf")), b.y1)

        merged: set[int] = set()
        for i in range(len(ordered)):
            if i in merged or id(ordered[i]) in merged:
                continue
            a = ordered[i]
            if a.type not in ("text", "paragraph"):
                continue
            # 关键修复: 只有每页【最后一个】正文块才可能是被分页切断的段落。
            # 原实现遍历所有 text 块, 会先遇到非页末块(如 po=29)并抢走下一页首块,
            # 而真正的页末块(如 po=30)反而被跳过 —— 导致"软件开发..."被挤到
            # "质保等级:QA1。" 之前, 文本顺序错乱。
            if last_text_by_page.get(a.page) != i:
                continue
            j = first_text_by_page.get(a.page + 1)
            if j is None or j <= i:
                continue
            # 下一页首块已被合并过 → 跳过（防止重复注入）
            if j in merged or id(ordered[j]) in merged:
                continue
            b = ordered[j]
            if b.type not in ("text", "paragraph"):
                continue
            if a.font_sig != b.font_sig:
                continue
            at = a.text.strip()
            bt = b.text.strip()
            if not at or not bt:
                continue
            # 前一段必须以未完结方式结尾（非句末标点）
            if at[-1] in _END_SENTENCE:
                continue
            # 下一段开头不能是编号或项目符号（避免把下一节首段合并进来）
            import re
            if re.match(r"^(\d+[.、)）]|[一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[)）]|[-•·*])", bt):
                continue
            # 页面几何边界约束：
            #   a 必须贴近本页底部（y1 距本页正文最大 y1 ≤ 60pt）
            #   b 必须贴近下一页顶部（y0 距下一页正文最小 y0 ≤ 60pt）
            # 这保证是"页末被切段的续行"，而非把页面中部块与下一页首块误合。
            EDGE_TOL = 60.0
            if page_y1_max.get(a.page, a.y1) - a.y1 > EDGE_TOL:
                continue
            if b.y0 - page_y0_min.get(b.page, b.y0) > EDGE_TOL:
                continue
            # 文本连续性：末尾为中文或中文非句末标点，开头为中文
            # （跨页断行常以逗号/顿号结尾，如 "...能力要求、" + "评估等..."）
            _NON_END_PUNCT = set("，、；：")
            a_last = at[-1]
            b_first = bt[0]
            a_ok = ('\u4e00' <= a_last <= '\u9fff') or a_last in _NON_END_PUNCT
            b_ok = '\u4e00' <= b_first <= '\u9fff'
            if not (a_ok and b_ok):
                continue
            # 合并文本、行、坐标
            a.text = a.text.rstrip() + "\n" + b.text
            a.lines.extend(b.lines)
            a.bbox = (min(a.x0, b.x0), min(a.y0, b.y0), max(a.x1, b.x1), max(a.y1, b.y1))
            # paint_order 取两段中的最大值：跨页段落的主体会落在下一页标题之后，
            # 同时保证原本属于上一页末尾的续行不会跑到下一页标题前面。
            a.paint_order = max(a.paint_order, b.paint_order)
            merged.add(id(b))
        # 删除被合并的块
        blocks[:] = [b for b in blocks if id(b) not in merged]

    def detect_columns(self, blocks: list[Block], page_width: float) -> list[tuple[float, float]]:
        """列检测：X 轴空白带聚类。返回列边界区间列表 [(x0,x1), ...]。"""
        # 简单实现：统计文本块 x0 起点聚类
        starts = sorted({round(b.bbox[0], 1) for b in blocks if b.type == "text"})
        if len(starts) < 2:
            return [(40.0, page_width - 40.0)]
        clusters: list[list[float]] = []
        for s in starts:
            if clusters and abs(s - clusters[-1][-1]) < 30.0:
                clusters[-1].append(s)
            else:
                clusters.append([s])
        cols = []
        for c in clusters:
            cols.append((min(c) - 20.0, max(c) + 200.0))
        return cols
