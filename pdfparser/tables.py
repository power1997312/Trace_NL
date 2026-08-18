"""L3 表格结构重建（研究报告场景 D / T6）。

策略（准确性优先）：
1. CamelotEngine：camelot lattice（有线表）优先，无表/低准确率时 stream（无线表）。
   仅在 Ghostscript 可用时启用（准确性标杆）。
2. PdfplumberEngine：兜底引擎，per-axis 策略（lines/text），自研行列网格重建。
3. 后处理（两引擎通用）：
   a. 合并单元格恢复（spanning）：extract 网格中 None 单元格向上/向左延伸，
      依据原始 cell bbox 跨越多行/列的证据；
   b. 表头识别：首行（或前 N 行）与数据行存在样式/粗线分隔 → header_rows；
   c. 跨页接续表拼接：相邻页表头文本重复 → 合并两个 TableData，去除重复表头；
   d. 结构校验：行列一致性、空单元格占比、列边界对齐，输出 warnings。
"""
from __future__ import annotations

import shutil
from typing import Optional

from pdfparser.models import TableData, TableCell, Block

# 跨页表头：文本相似度阈值（去空白后完全一致即视为重复表头）
HEADER_REPEAT_TOL = 0.0


# ---------------------------------------------------------------------------
# 引擎抽象
# ---------------------------------------------------------------------------
class _BaseEngine:
    name = "base"

    def available(self) -> bool:
        return True

    def detect(self, pdf_path: str, page_no: int, page, plumber_page=None) -> list[TableData]:
        """检测第 page_no 页（0-based）的表格，返回 TableData 列表。"""
        raise NotImplementedError


class CamelotEngine(_BaseEngine):
    """camelot lattice/stream 双模式。"""

    name = "camelot"

    def __init__(self):
        self._camelot = None
        self._ok = False
        if shutil.which("gs") or shutil.which("gswin64c"):
            try:
                import camelot  # type: ignore
                self._camelot = camelot
                self._ok = True
            except Exception:  # noqa: BLE001
                self._ok = False

    def available(self) -> bool:
        return self._ok

    def detect(self, pdf_path, page_no, page, plumber_page=None) -> list[TableData]:
        out: list[TableData] = []
        if not self._ok:
            return out
        for flavor in ("lattice", "stream"):
            try:
                tables = self._camelot.read_pdf(
                    pdf_path, pages=str(page_no + 1), flavor=flavor,
                    suppress_stdout=True, silent=True,
                )
            except Exception:  # noqa: BLE001
                continue
            if len(tables) == 0:
                continue
            for t in tables:
                acc = getattr(t, "accuracy", 0.0) or 0.0
                df = t.df
                rows = [[TableCell(text=(c or ""), bbox=(0, 0, 0, 0))
                         for c in row] for row in df.values.tolist()]
                td = TableData(rows=rows, flavor=flavor, engine="camelot",
                               page_start=page_no, page_end=page_no,
                               bbox=tuple(getattr(t, "bbox", (0, 0, 0, 0)) or (0, 0, 0, 0)))
                if acc < 60.0:
                    td.warnings.append(f"camelot accuracy 偏低: {acc:.0f}%")
                out.append(td)
            if out:
                break  # lattice 有结果则不再试 stream
        return out


class PdfplumberEngine(_BaseEngine):
    """pdfplumber 兜底：lines/text 策略 + 自研网格。"""

    name = "pdfplumber"

    def detect(self, pdf_path, page_no, page, plumber_page=None) -> list[TableData]:
        out: list[TableData] = []
        if plumber_page is None:
            return out
        # 1) 有线表：pdfplumber lines 策略
        try:
            tables = plumber_page.find_tables(
                table_settings={"vertical_strategy": "lines",
                                "horizontal_strategy": "lines"})
        except Exception:  # noqa: BLE001
            tables = []
        if tables:
            for t in tables:
                if self._is_chart_area(t, plumber_page):
                    continue  # 图表区域（旋转轴标签等）非真表格
                td = self._rebuild(t, plumber_page, page_no, "lattice")
                if td.n_rows >= 1 and td.n_cols >= 1:
                    out.append(td)
            if out:
                return out
        # 2) 无线表：自研全页检测（字段间隙聚类 + 空白带分段）
        return self._stream_detect_page(plumber_page, page_no)

    @staticmethod
    def _is_chart_area(table, plumber_page) -> bool:
        """图表区域判定：表 bbox 内旋转/竖排文本占比 > 30% → 图表坐标轴而非表格。
        （LaTeX pgfplots 用 cm 变换旋转轴标签，'upright' 标志可能失效，
        故补充 bbox 高>宽的竖排词检测，如旋转 90° 的 'rorre'=error）"""
        words = plumber_page.extract_words()
        x0, y0, x1, y1 = table.bbox
        inside = [w for w in words
                  if w["x0"] >= x0 and w["x1"] <= x1
                  and w["top"] >= y0 and w["bottom"] <= y1]
        if len(inside) < 3:
            return False
        rot = sum(1 for w in inside
                  if not w.get("upright", True)
                  or (len(w["text"]) >= 3
                      and (w["bottom"] - w["top"]) > (w["x1"] - w["x0"]) + 2))
        # 竖排/旋转词是图表的强信号（正常表格文字均为横排）：≥2 个即判图表
        if rot >= 2:
            return True
        return rot / len(inside) > 0.3

    # ------------------------------------------------------------------
    def _stream_detect_page(self, plumber_page, page_no: int) -> list[TableData]:
        """全页无线表检测：多字段行聚类，按空白带分段，逐段重建。"""
        from collections import defaultdict

        words = plumber_page.extract_words()
        if len(words) < 6:
            return []
        # 行分组
        rows_dict: dict[int, list] = defaultdict(list)
        for w in words:
            rows_dict[round(w["top"] / 3)].append(w)
        segments: list[list[tuple]] = []   # 每段: [(key, fields, ws), ...]
        cur: list[tuple] = []
        prev_top = None
        for key in sorted(rows_dict):
            ws = sorted(rows_dict[key], key=lambda w: w["x0"])
            fields = self._fields_of(ws)
            if len(fields) < 2:
                continue  # 单字段行（标题/正文/说明）→ 剔除
            top = min(w["top"] for w in ws)
            if cur and prev_top is not None and top - prev_top > 24.0:
                segments.append(cur)
                cur = []
            cur.append((key, fields, ws))
            prev_top = max(w["bottom"] for w in ws)
        if cur:
            segments.append(cur)
        out: list[TableData] = []
        for seg in segments:
            if len(seg) < 3:
                continue
            td = self._build_stream_table(seg)
            if td.n_rows >= 1 and td.n_cols >= 2:
                out.append(td)
        if not out:
            return []
        # 双栏页面防线：若检测表列数与页面内容栏数一致且列起点吻合，
        # 说明是把"双栏正文"误判为表格（LLM_Survey 类文档）
        col_clusters = self._x_clusters([w["x0"] for w in words], tol=15.0)
        if len(col_clusters) >= 2:
            filtered = []
            for td in out:
                if len(td.columns) <= len(col_clusters) and all(
                        any(abs(cx - c[0]) < 15.0 for c in col_clusters)
                        for cx in td.columns):
                    continue  # 栏匹配 → 双栏正文
                filtered.append(td)
            out = filtered
        return out

    @staticmethod
    def _x_clusters(vals: list[float], tol: float) -> list[list[float]]:
        vs = sorted(vals)
        clusters: list[list[float]] = []
        for v in vs:
            if clusters and v - clusters[-1][-1] <= tol:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return clusters

    @staticmethod
    def _fields_of(ws) -> list[tuple[str, float, float]]:
        """行内词间隙 > 6pt → 字段分隔。"""
        fields: list[tuple[str, float, float]] = []
        cur = [ws[0]]
        for w in ws[1:]:
            if w["x0"] - cur[-1]["x1"] > 6.0:
                fields.append((" ".join(x["text"] for x in cur),
                               cur[0]["x0"], cur[-1]["x1"]))
                cur = [w]
            else:
                cur.append(w)
        fields.append((" ".join(x["text"] for x in cur),
                       cur[0]["x0"], cur[-1]["x1"]))
        return fields

    def _build_stream_table(self, seg) -> TableData:
        """由多字段行段重建表格网格。

        误检防线（真实文档强化，2026-08 验证）：
        1) 列数范围 [2, 12]——双栏文本/公式会被 18+ 列聚类排除；
        2) 列对齐一致性——每行的每个字段起点必须命中列边界聚类（±6pt）；
        3) 行字段一致性——每行占满全部列（众数列数 == 列数）；
        4) 有效行 ≥3。
        """
        from collections import Counter

        # 1) 候选列边界（字段起点聚类）
        starts = sorted(f[1] for _, fs, _ in seg for f in fs)
        clusters: list[list[float]] = []
        for s in starts:
            if clusters and s - clusters[-1][-1] <= 8.0:
                clusters[-1].append(s)
            else:
                clusters.append([s])
        col_x = [sum(c) / len(c) for c in clusters]
        if not (2 <= len(col_x) <= 12):
            return TableData()

        # 2) 行过滤：字段起点全部命中列边界
        valid_rows = []
        for key, fs, ws in seg:
            hits: list[int] = []
            ok = True
            for text, x0, x1 in fs:
                cand = [i for i, cx in enumerate(col_x) if abs(x0 - cx) <= 6.0]
                if not cand:
                    ok = False
                    break
                hits.append(min(cand, key=lambda i: abs(x0 - col_x[i])))
            if ok:
                valid_rows.append((key, fs, ws, hits))
        if len(valid_rows) < 3:
            return TableData()

        # 3) 行字段一致性：众数"唯一列数"必须等于列数（每行占满全部列）
        col_counts = Counter(len(set(h)) for _, _, _, h in valid_rows)
        if col_counts.most_common(1)[0][0] != len(col_x):
            return TableData()

        # 4) 行距一致性（防段落块误检）：行间距波动 CV ≤ 0.8
        tops = sorted(min(w["top"] for w in ws) for _, _, ws, _ in valid_rows)
        if len(tops) >= 4:
            gaps = [b - a for a, b in zip(tops, tops[1:]) if b - a > 0]
            if gaps:
                import statistics
                mean_gap = sum(gaps) / len(gaps)
                if mean_gap > 0 and statistics.pstdev(gaps) / mean_gap > 0.8:
                    return TableData()

        # 重建网格
        x1_all = max(w["x1"] for _, _, ws, _ in valid_rows for w in ws)
        rows: list[list[TableCell]] = []
        for key, fs, ws, hits in valid_rows:
            top = min(w["top"] for w in ws)
            bot = max(w["bottom"] for w in ws)
            n = len(col_x)
            row_cells = [TableCell(text="", bbox=(col_x[i], top,
                           col_x[i + 1] if i + 1 < n else x1_all, bot))
                         for i in range(n)]
            for (text, x0, x1), ci in zip(fs, hits):
                span = 1
                while ci + span < n and x1 > col_x[ci + span] - 4:
                    span += 1
                cell = row_cells[ci]
                cell.text = text
                cell.colspan = max(cell.colspan, span)
            rows.append(row_cells)
        if not rows:
            return TableData()
        # 列表防线：>50% 行首字段为符号标记（•·-* 等）→ 是符号列表而非表格
        _SYMBOLS = ("•", "·", "-", "*", "–", "—", "○", "●", "▪", "■")
        sym_rows = sum(1 for row in rows
                       if row and (row[0].text or "").strip() in _SYMBOLS)
        if len(rows) and sym_rows / len(rows) > 0.5:
            return TableData()
        y0 = min(c.bbox[1] for row in rows for c in row)
        y1 = max(c.bbox[3] for row in rows for c in row)
        return TableData(rows=rows, flavor="stream", engine="pdfplumber",
                         page_start=0, page_end=0,
                         bbox=(col_x[0], y0, x1_all, y1),
                         columns=col_x)

    # ------------------------------------------------------------------
    def _stream_rebuild(self, region, plumber_page, page_no: int) -> TableData:
        """自研无线表重建（空格对齐表格）。

        1. 区域内的 word 按 top 聚类成行；
        2. 行内词间隙 > 6pt → 字段分隔；字段数 < 2 的行视为非表格行（标题/说明）剔除；
        3. 字段起点 X 聚类（容差 8pt）→ 列边界；
        4. 字段按最近列归属，跨列字段标记 colspan。
        """
        from collections import defaultdict

        rx0, ry0, rx1, ry1 = region
        words = [w for w in plumber_page.words
                 if w["x0"] >= rx0 - 2 and w["x1"] <= rx1 + 2
                 and w["top"] >= ry0 - 2 and w["bottom"] <= ry1 + 2]
        if len(words) < 6:
            return TableData()
        # 行分组
        rows_dict: dict[int, list] = defaultdict(list)
        for w in words:
            rows_dict[round(w["top"] / 3)].append(w)
        field_rows = []
        for key in sorted(rows_dict):
            ws = sorted(rows_dict[key], key=lambda w: w["x0"])
            fields: list[tuple[str, float, float]] = []
            cur = [ws[0]]
            for w in ws[1:]:
                if w["x0"] - cur[-1]["x1"] > 6.0:
                    fields.append((cur[0]["text"], cur[0]["x0"], cur[-1]["x1"]))
                    cur = [w]
                else:
                    cur.append(w)
            fields.append((" ".join(x["text"] for x in cur), cur[0]["x0"], cur[-1]["x1"]))
            if len(fields) >= 2:
                field_rows.append((key, fields, ws))
        if len(field_rows) < 3:
            return TableData()
        # 列边界：字段起点聚类
        starts = sorted(f[1] for _, fs, _ in field_rows for f in fs)
        clusters: list[list[float]] = []
        for s in starts:
            if clusters and s - clusters[-1][-1] <= 8.0:
                clusters[-1].append(s)
            else:
                clusters.append([s])
        col_x = [sum(c) / len(c) for c in clusters]
        if len(col_x) < 2:
            return TableData()
        # 行边界与网格
        rows: list[list[TableCell]] = []
        for key, fs, ws in field_rows:
            top = min(w["top"] for w in ws)
            bot = max(w["bottom"] for w in ws)
            n = len(col_x)
            row_cells = []
            for i in range(n):
                xa = col_x[i]
                xb = col_x[i + 1] if i + 1 < n else rx1
                row_cells.append(TableCell(text="", bbox=(xa, top, xb, bot)))
            for text, x0, x1 in fs:
                ci = min(range(n), key=lambda i: abs(col_x[i] - x0))
                # 跨列检测
                span = 1
                while ci + span < n and x1 > col_x[ci + span] - 4:
                    span += 1
                cell = row_cells[ci]
                cell.text = text
                cell.colspan = max(cell.colspan, span)
            rows.append(row_cells)
        return TableData(rows=rows, flavor="stream", engine="pdfplumber",
                         page_start=page_no, page_end=page_no,
                         bbox=region, columns=col_x)

    def _rebuild(self, table, plumber_page, page_no: int, flavor: str) -> TableData:
        grid = table.extract()
        cells_rects = [tuple(c) for c in table.cells]  # (x0, top, x1, bottom)
        n_rows = len(grid)
        n_cols = max((len(r) for r in grid), default=0)
        rows: list[list[TableCell]] = []
        # 行/列边界（pdfplumber Row/Column 仅暴露 bbox: x0,top,x1,bottom）
        row_bounds = [(r.bbox[1], r.bbox[3]) for r in table.rows]
        col_bounds = [(c.bbox[0], c.bbox[2]) for c in table.columns]
        for ri, (top, bot) in enumerate(row_bounds):
            row_cells: list[TableCell] = []
            for ci, (x0, x1) in enumerate(col_bounds):
                # 找到覆盖该格的原始 cell
                cell_text = ""
                span_cell = None
                for rect in cells_rects:
                    rx0, ry0, rx1, ry1 = rect
                    # 原始 cell 垂直/水平覆盖本格中心
                    cx = (x0 + x1) / 2
                    cy = (top + bot) / 2
                    if rx0 - 1 <= cx <= rx1 + 1 and ry0 - 1 <= cy <= ry1 + 1:
                        if rx1 - rx0 > (x1 - x0) + 1 or ry1 - ry0 > (bot - top) + 1:
                            span_cell = rect  # 跨越多个基础格
                        cell_text = grid[ri][ci] if ri < n_rows and ci < len(grid[ri]) else ""
                        if not cell_text:
                            # 尝试从 rect 直接取文本
                            rect_text = self._text_in_rect(plumber_page, rect)
                            if rect_text:
                                cell_text = rect_text
                        break
                # 保留单元格内换行(多值单元格如 "<A>\n<B>" 靠换行切分多引用)
                tc = TableCell(text=(cell_text or ""),
                               bbox=(x0, top, x1, bot))
                if span_cell:
                    # 计算跨行列数
                    tc.colspan = max(1, round((span_cell[2] - span_cell[0]) / max((x1 - x0), 1)))
                    tc.rowspan = max(1, round((span_cell[3] - span_cell[1]) / max((bot - top), 1)))
                row_cells.append(tc)
            rows.append(row_cells)
        x0 = min((c.bbox[0] for c in table.columns), default=0)
        x1 = max((c.bbox[2] for c in table.columns), default=0)
        y0 = min((r.bbox[1] for r in table.rows), default=0)
        y1 = max((r.bbox[3] for r in table.rows), default=0)
        td = TableData(rows=rows, flavor=flavor,
                       engine="pdfplumber", page_start=page_no, page_end=page_no,
                       bbox=(x0, y0, x1, y1),
                       columns=[c.bbox[0] for c in table.columns])
        td.header_rows = self._detect_header(table, plumber_page)
        return td

    @staticmethod
    def _detect_header(table, plumber_page) -> int:
        """表头识别：首行与次行的字符样式（字体名/字号）不一致 → 表头。"""
        from collections import Counter
        if len(table.rows) < 2:
            return 0
        r0 = table.rows[0].bbox
        r1 = table.rows[1].bbox

        def style_of(b):
            chars = [c for c in plumber_page.chars
                     if c["x0"] >= b[0] - 1 and c["x1"] <= b[2] + 1
                     and c["top"] >= b[1] - 1 and c["bottom"] <= b[3] + 1]
            if not chars:
                return None
            return Counter((c["fontname"], round(c["size"], 1)) for c in chars).most_common(1)[0][0]

        s0, s1 = style_of(r0), style_of(r1)
        if s0 is None or s1 is None:
            return 0
        return 1 if s0 != s1 else 0

    @staticmethod
    def _text_in_rect(plumber_page, rect) -> str:
        try:
            crop = plumber_page.crop(rect)
            return (crop.extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------
class TableExtractor:
    """表格检测编排：引擎选择 → 检测 → 后处理（合并格/表头/跨页）→ 校验。"""

    def __init__(self, pdf_path: str, prefer_camelot: bool = True):
        self.pdf_path = pdf_path
        self._engines: list[_BaseEngine] = []
        camelot_eng = CamelotEngine()
        plumber_eng = PdfplumberEngine()
        if prefer_camelot and camelot_eng.available():
            self._engines = [camelot_eng, plumber_eng]
        else:
            self._engines = [plumber_eng]
        self.engine_used = [e.name for e in self._engines]

    def extract_all(self, doc, plumber_doc) -> list[TableData]:
        """检测全文档表格。返回 TableData 列表（按页排序）。"""
        all_tables: list[TableData] = []
        for pno in range(doc.page_count):
            page = doc[pno]
            plumber_page = plumber_doc.pages[pno] if plumber_doc else None
            found = False
            for eng in self._engines:
                if not eng.available():
                    continue
                try:
                    ts = eng.detect(self.pdf_path, pno, page, plumber_page)
                except Exception:  # noqa: BLE001
                    continue
                if ts:
                    # 过滤：1 行表/空表（页面框线误检）+ 单元格非空率过低（图框/图表）
                    ts = [t for t in ts
                          if t.n_rows >= 2
                          and (t.n_rows >= 3 or t.n_cols >= 3)
                          and any(c.text.strip() for row in t.rows for c in row)]
                    if ts:
                        total_cells = sum(len(r) for t in ts for r in t.rows)
                        filled = sum(1 for t in ts for r in t.rows
                                     for c in r if c.text.strip())
                        if total_cells and filled / total_cells < 0.25:
                            ts = []  # 整体非空率过低（图框/空表）
                    all_tables.extend(ts)
                    found = True
                    break
            if not found:
                # 记录该页无表（供 Block 分区时判断）
                pass
        all_tables.sort(key=lambda t: (t.page_start, t.bbox[1]))
        merged = self._join_cross_page(all_tables)
        for t in merged:
            self._validate(t)
        return merged

    # ------------------------------------------------------------------
    def _join_cross_page(self, tables: list[TableData]) -> list[TableData]:
        """跨页接续表拼接：相邻页表头重复 → 合并。"""
        if len(tables) < 2:
            return tables
        merged: list[TableData] = []
        prev: Optional[TableData] = None
        for t in tables:
            if prev is not None and t.page_start == prev.page_end + 1:
                if self._same_header(prev, t):
                    self._append_rows(prev, t)
                    prev.page_end = t.page_end
                    prev.engine += f"+{t.engine}"
                    continue
            merged.append(t)
            prev = t
        return merged

    @staticmethod
    def _same_header(a: TableData, b: TableData) -> bool:
        """跨页接续判据（放宽版）：列数一致 + 列边界一致 + 首行文本匹配。"""
        if a.n_cols != b.n_cols or a.n_cols == 0:
            return False
        for c in range(a.n_cols):
            ta = (a.rows[0][c].text or "").strip()
            tb = (b.rows[0][c].text or "").strip()
            if ta != tb:
                return False
        # 列边界一致性（±3pt）——降低"两张独立同表头表"的误拼
        if a.columns and b.columns and len(a.columns) == len(b.columns):
            for x, y in zip(a.columns, b.columns):
                if abs(x - y) > 3.0:
                    return False
        return True

    def _append_rows(self, target: TableData, src: TableData) -> None:
        """去除 src 重复表头后追加。"""
        start = src.header_rows
        # 未标注表头但首行与 target 表头一致（重复表头）→ 跳过首行
        if start == 0 and target.n_rows > 0 and self._same_header(target, src):
            start = 1
        target.rows.extend(src.rows[start:])
        target.header_rows = max(target.header_rows, src.header_rows)

    def _validate(self, t: TableData) -> None:
        """结构校验。"""
        if t.n_cols == 0:
            t.warnings.append("表格列数为 0，疑似误检")
        widths = [len(r) for r in t.rows]
        if len(set(widths)) > 1:
            t.warnings.append(f"行宽不一致: {set(widths)}，存在合并格或误检")
        empty = sum(1 for r in t.rows for c in r if not (c.text or "").strip())
        total = t.n_rows * t.n_cols
        if total and empty / total > 0.6:
            t.warnings.append(f"空单元格占比过高: {empty / total:.0%}")

    # ------------------------------------------------------------------
    def carve_blocks(self, blocks: list[Block], tables: list[TableData]) -> None:
        """把表格区域内的正文块移除，替换为 table 块。

        原则：表格区域（bbox 扩展小容差）内的文本行不再作为正文输出；
        表格块按绘制顺序插回块流，保证"表格打断段落"信息不丢失。
        """
        for td in tables:
            # 删除被表格覆盖的 text/caption 块
            for b in blocks:
                if b.type == "table":
                    continue
                if b.page == td.page_start and _bbox_inside(b.bbox, td.bbox, tol=4.0):
                    b.type = "consumed"  # 被表格吸收
            # 插入 table 块（保持原绘制位置）
            tb = Block(type="table", text="", bbox=td.bbox,
                       page=td.page_start, paint_order=-1, table=td)
            # 放置在同页最后一个被吸收块之后（近似）
            blocks.append(tb)


def _bbox_inside(inner, outer, tol: float = 4.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)
