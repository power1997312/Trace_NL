from __future__ import annotations
"""
格式化Excel生成模块
- 逆向追踪矩阵Sheet (6列, 按下游文档分sheet)
- CellRichText文字级着色
- 合并单元格处理一对多关系
- 格式复刻Trace_Base.xlsx

正向追踪矩阵已移至独立模块(generate_forward_matrix.py /
generate_forward_sd_matrix.py), 本模块仅输出逆向矩阵。
"""
import os
from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side,
)
from openpyxl.cell.rich_text import CellRichText, TextBlock
from core.text_matcher import TextRun
from openpyxl.cell.text import InlineFont

from config import (
    HEADER_FILL, COLOR_GREEN, COLOR_BLUE, COLOR_BLACK,
    FONT_NAME, HEADER_FONT_SIZE, CONTENT_FONT_SIZE, ID_FONT_SIZE,
    MatchCategory,
)
from core.traceability_matrix import TraceabilityMatrix


# 通用样式
_THIN_BORDER = Border(
    left=Side(style='thin'),
    right=Side(style='thin'),
    top=Side(style='thin'),
    bottom=Side(style='thin'),
)

_HEADER_FILL = PatternFill(start_color=HEADER_FILL, end_color=HEADER_FILL, fill_type='solid')
_HEADER_FONT = Font(
    name=FONT_NAME, size=HEADER_FONT_SIZE, bold=True, color='FFFFFF',
)
_HEADER_ALIGNMENT = Alignment(
    horizontal='center', vertical='center', wrap_text=True,
)
_DATA_ALIGNMENT = Alignment(vertical='top', wrap_text=True)

# InlineFont定义(用于RichText)
_IF_GREEN = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_GREEN)
_IF_BLUE = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_BLUE)
_IF_BLACK = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_BLACK)
_IF_DEFAULT = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_BLACK)


def _get_inline_font(category: MatchCategory) -> InlineFont:
    """根据匹配类别获取InlineFont"""
    color = category.color
    if color == COLOR_GREEN:
        return _IF_GREEN
    elif color == COLOR_BLUE:
        return _IF_BLUE
    else:
        return _IF_BLACK


def _make_rich_text(text_runs, default_text: str = '') -> CellRichText | str:
    """
    将TextRun列表转为CellRichText

    openpyxl CellRichText 正确处理 \\n (在 inline string <t> 元素中保留换行)。
    每个TextRun对应一个独立的 <r> 元素，各自拥有独立的颜色和字体。

    Args:
        text_runs: list[TextRun] 着色文本运行列表
        default_text: 无runs时的默认文本

    Returns:
        CellRichText 或 纯字符串
    """
    if not text_runs:
        return default_text

    blocks = []
    for run in text_runs:
        if not run.text:
            continue
        ifont = _get_inline_font(run.category)
        blocks.append(TextBlock(ifont, run.text))

    if not blocks:
        return default_text
    if len(blocks) == 1:
        return CellRichText(blocks[0])

    return CellRichText(*blocks)


def generate_excel(
    matrix: TraceabilityMatrix,
    output_path: str,
):
    """
    生成格式化的逆向追踪矩阵Excel文件

    Args:
        matrix: 追踪矩阵数据
        output_path: 输出文件路径

    逆向追踪矩阵按下游文档(downstream_doc)拆分到不同sheet页,
    不同系统设计文档的追踪关系数据不再混在同一sheet中。
    """
    wb = Workbook()

    # 按下游文档分组(问题2)
    from collections import defaultdict
    doc_groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(matrix.rows):
        key = row.downstream_doc or '逆向追踪矩阵'
        doc_groups[key].append(i)

    is_multi = len(doc_groups) > 1
    first = True
    for doc_name, indices in doc_groups.items():
        if first:
            ws = wb.active
            first = False
        else:
            ws = wb.create_sheet()
        # 多文档时分sheet, 单文档时保持原sheet名 '逆向追踪矩阵'
        ws.title = _safe_sheet_name(doc_name) if is_multi else '逆向追踪矩阵'
        _write_backward_sheet(ws, matrix, indices)

    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    wb.save(output_path)


def _safe_sheet_name(name: str) -> str:
    """生成合法的Excel sheet名(≤31字符, 去除非法字符)"""
    illegal = [':', '\\', '/', '?', '*', '[', ']']
    for ch in illegal:
        name = name.replace(ch, '')
    name = name.strip()
    if not name:
        name = '逆向追踪矩阵'
    if len(name) > 31:
        name = name[:31]
    return name


def _union_text_runs(all_runs_list, full_text):
    if not all_runs_list:
        return []
    priority = {MatchCategory.GREEN: 3, MatchCategory.BLUE: 2, MatchCategory.BLACK: 1}
    max_len = max(len(full_text), max(sum(len(r.text) for r in rl) for rl in all_runs_list))
    char_colors = [MatchCategory.BLACK] * max_len
    for rl in all_runs_list:
        pos = 0
        for run in rl:
            cat = run.category
            for k in range(len(run.text)):
                if pos + k < max_len and priority.get(cat, 0) > priority.get(char_colors[pos + k], 0):
                    char_colors[pos + k] = cat
            pos += len(run.text)
    runs = []
    cur_cat = None
    cur_text = []
    for i, ch in enumerate(full_text):
        cc = char_colors[i] if i < len(char_colors) else MatchCategory.BLACK
        if cur_cat is None:
            cur_cat = cc
            cur_text = [ch]
        elif cc == cur_cat:
            cur_text.append(ch)
        else:
            runs.append(TextRun(text="".join(cur_text), category=cur_cat))
            cur_cat = cc
            cur_text = [ch]
    if cur_text:
        runs.append(TextRun(text="".join(cur_text), category=cur_cat))
    return runs

def _estimate_visible_lines(text, col_width: float, font_size: float = CONTENT_FONT_SIZE) -> int:
    """估算文本在指定列宽(Excel字符单位)下 wrap_text 后的可见行数。

    Excel 列宽 1 单位 ≈ 1 个半角字符宽度; 全角/CJK 字符按 2 单位计。
    wrap_text 时文本按列宽自动折行, 显式换行(\\n)强制分段。
    """
    if not text:
        return 1
    # 可用字符空间: 列宽扣除左右边距余量
    chars_per_line = max(4.0, col_width - 2.0)
    total = 0
    for seg in str(text).split('\n'):
        if not seg:
            total += 1
            continue
        w = 0.0
        for ch in seg:
            # 全角字符(含CJK)占 2 个半角宽度
            w += 2.0 if ord(ch) > 0x7F else 1.0
        lines = max(1, -(-int(w) // int(chars_per_line)))
        total += lines
    return max(1, total)


def _set_row_height(ws, row: int, texts_with_widths: list[tuple], min_height: float = 15.0,
                    line_height: float = 17.0, pad: float = 4.0) -> None:
    """根据单元格内容估算并设置行高, 使 wrap_text 产生的多行在视觉上展开。

    Args:
        texts_with_widths: [(文本, 列宽), ...] —— 取其中最大估算行数作为行高依据。
    """
    max_lines = 1
    for text, col_width in texts_with_widths:
        if text:
            est = _estimate_visible_lines(text, col_width)
            # 安全系数1.15: 字宽边界处Excel实际折行常比估算多一行;
            # 长文本折行数多, 该误差被放大, 乘系数兜底避免"看不全"
            est = max(est, int(est * 1.15) + (1 if est > 3 else 0))
            max_lines = max(max_lines, est)
    ws.row_dimensions[row].height = max(min_height, max_lines * line_height + pad)


def _set_merged_row_heights(ws, first_row: int, last_row: int,
                            texts_with_widths: list[tuple], min_height: float = 15.0,
                            line_height: float = 17.0, pad: float = 4.0) -> None:
    """合并单元格区域的行高分摊。

    合并单元格的 wrap_text 多行文本显示在合并区域总高度内, 因此把估算所需
    总高度按行数平均分摊给合并范围内的每一行。
    """
    max_lines = 1
    for text, col_width in texts_with_widths:
        if text:
            est = _estimate_visible_lines(text, col_width)
            est = max(est, int(est * 1.15) + (1 if est > 3 else 0))
            max_lines = max(max_lines, est)
    total_height = max(min_height, max_lines * line_height + pad)
    n = max(1, last_row - first_row + 1)
    per_row = max(min_height, total_height / n)
    for r in range(first_row, last_row + 1):
        ws.row_dimensions[r].height = per_row


def _write_backward_sheet(ws, matrix: TraceabilityMatrix, row_indices: list[int] = None):
    """
    写入逆向追踪矩阵Sheet

    Args:
        row_indices: 本sheet要写入的全局行索引列表(按文档分组后)。
                     为None时写入全部行(保持兼容)。
    """
    if row_indices is None:
        row_indices = list(range(len(matrix.rows)))

    # 表头（第7列 下游文档 = 实际来源文档名, 供正向矩阵识别来源）
    # 第8-11列为候选发现层元数据(三模式接入): 关系来源/置信度/证据/待人工确认
    # 第12列为LLM裁决器的AI审核意见(未启用裁决器时为空列)
    headers = ['序号', '下游条目号', '下游内容', '上游文档', '上游条目号/章节', '上游内容', '下游文档',
               '关系来源', '置信度', '证据', '待人工确认', 'AI 审核意见']
    widths = [6, 22, 55, 20, 22, 70, 20, 10, 8, 48, 10, 48]
    _write_header_row(ws, headers, widths)

    # 全局索引 -> 本sheet局部Excel行号(2-based) 的映射
    local_row = {}
    for local, global_i in enumerate(row_indices):
        local_row[global_i] = local + 2

    # 预处理: 对合并组的下游内容计算Union着色(GREEN>BLUE>BLACK)
    # 仅处理属于本sheet的合并组
    _backward_union_ds = {}
    for seq, indices in matrix.backward_merge_groups.items():
        if len(indices) < 2:
            continue
        # 合并组必须完整属于本sheet
        if not all(idx in local_row for idx in indices):
            continue
        all_ds_runs = []
        for idx in indices:
            mr = matrix.rows[idx].match_result
            if mr and mr.downstream_runs:
                all_ds_runs.append(mr.downstream_runs)
        if all_ds_runs:
            union = _union_text_runs(all_ds_runs, matrix.rows[indices[0]].downstream_content)
            _backward_union_ds[indices[0]] = union

    # 数据行
    for local_i, global_i in enumerate(row_indices):
        row = matrix.rows[global_i]
        r = local_row[global_i]  # Excel行号

        # 序号(按sheet本地重新编号, 问题2)
        _set_cell(ws, r, 1, local_i + 1,
                  Font(name=FONT_NAME, size=ID_FONT_SIZE))

        # 下游条目号
        _set_cell(ws, r, 2, row.downstream_id,
                  Font(name=FONT_NAME, size=ID_FONT_SIZE))

        # 下游内容(RichText着色)
        # 合并组首行使用Union着色(合并所有子行的下游匹配结果)
        if global_i in _backward_union_ds:
            rich = _make_rich_text(_backward_union_ds[global_i], row.downstream_content)
            ws.cell(row=r, column=3, value=rich)
            ws.cell(row=r, column=3).alignment = _DATA_ALIGNMENT
            ws.cell(row=r, column=3).border = _THIN_BORDER
        elif row.match_result and row.match_result.downstream_runs:
            rich = _make_rich_text(row.match_result.downstream_runs, row.downstream_content)
            ws.cell(row=r, column=3, value=rich)
            ws.cell(row=r, column=3).alignment = _DATA_ALIGNMENT
            ws.cell(row=r, column=3).border = _THIN_BORDER
        else:
            _set_cell(ws, r, 3, row.downstream_content,
                      Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))

        # 上游文档
        _set_cell(ws, r, 4, row.upstream_doc,
                  Font(name=FONT_NAME, size=ID_FONT_SIZE))

        # 上游条目号/章节
        _set_cell(ws, r, 5, row.upstream_ref,
                  Font(name=FONT_NAME, size=ID_FONT_SIZE))

        # 上游内容(RichText着色)
        if row.match_result and row.match_result.upstream_runs:
            rich = _make_rich_text(row.match_result.upstream_runs, row.upstream_content)
            ws.cell(row=r, column=6, value=rich)
            ws.cell(row=r, column=6).alignment = _DATA_ALIGNMENT
            ws.cell(row=r, column=6).border = _THIN_BORDER
        else:
            _set_cell(ws, r, 6, row.upstream_content,
                      Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))

        # 下游文档（实际来源文档名, 供正向矩阵识别来源, 避免按条目号前缀猜测）
        _set_cell(ws, r, 7, row.downstream_doc,
                  Font(name=FONT_NAME, size=ID_FONT_SIZE))

        # 关系来源: table(追踪表) / discover(内容发现) / hybrid(表+发现)
        _set_cell(ws, r, 8, row.relation_source,
                  Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))

        # 置信度: 发现层融合分(表格来源行留空)
        if row.relation_source != 'table' and row.candidate_score > 0:
            _set_cell(ws, r, 9, round(row.candidate_score, 2),
                      Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))
        else:
            _set_cell(ws, r, 9, '',
                      Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))

        # 证据: 通道证据摘要 / [added] / [suspicious] / UNTRACED参考候选
        _set_cell(ws, r, 10, row.evidence,
                  Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))

        # 待人工确认: ambiguous标记(AI高置信确认后由裁决器解除)
        _set_cell(ws, r, 11, '待人工确认' if row.ambiguous else '',
                  Font(name=FONT_NAME, size=CONTENT_FONT_SIZE,
                       color='FF0000' if row.ambiguous else COLOR_BLACK))

        # AI审核意见: LLM裁决器的输出(未启用时为空)
        ai_color = COLOR_BLACK
        if row.ai_opinion:
            if row.ai_opinion.startswith('AI确认') or row.ai_opinion.startswith('AI认同'):
                ai_color = COLOR_GREEN       # AI高置信确认
            elif row.ai_opinion.startswith('AI建议'):
                ai_color = COLOR_BLUE         # AI建议改溯/补漏, 需人工确认
        _set_cell(ws, r, 12, row.ai_opinion,
                  Font(name=FONT_NAME, size=CONTENT_FONT_SIZE, color=ai_color))

        # 数据行行高: 纳入全部长文本列自适应(内容+证据+AI意见)。
        # 此前只估算下游/上游内容两列, 证据与AI意见(均宽45)的长文本
        # 不参与行高计算, 导致这两列被压扁看不全、需手动调整。
        _set_row_height(ws, r, [
            (row.downstream_content, 55),
            (row.upstream_content, 70),
            (row.evidence, 48),
            (row.ai_opinion, 48),
        ])

    # 合并单元格(一对多关系) —— 仅合并本sheet内完整属于该组的行
    for seq, indices in matrix.backward_merge_groups.items():
        if len(indices) < 2:
            continue
        if not all(idx in local_row for idx in indices):
            continue
        first_row = local_row[indices[0]]
        last_row = local_row[indices[-1]]
        for col in [1, 2, 3]:  # 序号、下游条目号、下游内容
            ws.merge_cells(
                start_row=first_row, start_column=col,
                end_row=last_row, end_column=col,
            )
        # 合并组行高: 组内仅 序号/条目号/下游内容 三列合并竖排, 其余列
        # (上游内容/证据/AI意见等)每行独立显示 — 分摊值只保证合并列需求,
        # 各行不合并列的文本需求必须逐行守护, 取 max 防止分摊压低。
        _set_merged_row_heights(ws, first_row, last_row, [
            (matrix.rows[indices[0]].downstream_content, 55),
        ])
        for idx in indices:
            r = local_row[idx]
            row = matrix.rows[idx]
            prev = ws.row_dimensions[r].height or 15.0
            _set_row_height(ws, r, [
                (row.upstream_content, 70),
                (row.evidence, 48),
                (row.ai_opinion, 48),
            ])
            ws.row_dimensions[r].height = max(prev, ws.row_dimensions[r].height)


def _write_header_row(ws, headers: list[str], widths: list[int]):
    """写入表头行"""
    from openpyxl.utils import get_column_letter
    for col_idx, (header, width) in enumerate(zip(headers, widths), 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGNMENT
        cell.border = _THIN_BORDER
        # 设置列宽(支持>26列: AA/AB/...)
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _set_cell(ws, row: int, col: int, value, font: Font):
    """设置单元格值和样式"""
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = font
    cell.alignment = _DATA_ALIGNMENT
    cell.border = _THIN_BORDER


# ============================================================
# 导出入口
# ============================================================

if __name__ == "__main__":
    pass

