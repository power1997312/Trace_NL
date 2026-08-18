from __future__ import annotations
"""
正向追踪矩阵生成模块

从已完成的逆向追踪矩阵Excel中提取匹配数据，结合用户需求PDF的完整章节结构，
生成正向追踪矩阵（用户需求 → 系统需求）。

核心逻辑:
1. 从用户需求PDF提取所有章节/条目 (完整覆盖)
2. 从逆向矩阵获取追踪关系和匹配着色
3. 按用户需求分组, 上游内容合并着色 (union coloring: GREEN > BLUE > BLACK)
4. 未追踪的用户需求, 下游填"NA"
"""
import os
import sys
import re
import zipfile
import xml.etree.ElementTree as ET
import difflib
from collections import OrderedDict, defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont

from config import (
    HEADER_FILL, COLOR_GREEN, COLOR_BLUE, COLOR_BLACK,
    FONT_NAME, HEADER_FONT_SIZE, CONTENT_FONT_SIZE, ID_FONT_SIZE,
    MatchCategory,
)

from core.pdf_parser_adapter import prefetch_pdfs

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'

# InlineFont定义 (用于RichText着色)
_IF_GREEN = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_GREEN)
_IF_BLUE = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_BLUE)
_IF_BLACK = InlineFont(rFont=FONT_NAME, sz=CONTENT_FONT_SIZE, color=COLOR_BLACK)

# 通用样式
_THIN_BORDER = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin'),
)
_HEADER_FILL = PatternFill(start_color=HEADER_FILL, end_color=HEADER_FILL, fill_type='solid')
_HEADER_FONT = Font(name=FONT_NAME, size=HEADER_FONT_SIZE, bold=True, color='FFFFFF')
_HEADER_ALIGNMENT = Alignment(horizontal='center', vertical='center', wrap_text=True)
_DATA_ALIGNMENT = Alignment(vertical='top', wrap_text=True)


# ============================================================
# XML级RichText读取
# ============================================================

def _hex_to_category(rgb):
    """将RGB颜色字符串转为MatchCategory"""
    if not rgb:
        return MatchCategory.BLACK
    rgb = rgb.upper()
    if len(rgb) == 8:
        rgb = rgb[2:]
    if rgb == '00B050':
        return MatchCategory.GREEN
    elif rgb == '0070C0':
        return MatchCategory.BLUE
    return MatchCategory.BLACK


def _category_to_rgb(cat):
    """MatchCategory转RGB字符串"""
    if cat == MatchCategory.GREEN:
        return '00B050'
    elif cat == MatchCategory.BLUE:
        return '0070C0'
    return '000000'


def _dominant_category_from_runs(runs):
    """从runs列表判断主色调类别"""
    green_len = 0
    blue_len = 0
    for color, text in runs:
        if not text:
            continue
        cat = _hex_to_category(color)
        if cat == MatchCategory.GREEN:
            green_len += len(text)
        elif cat == MatchCategory.BLUE:
            blue_len += len(text)
    if green_len > 0 and green_len >= blue_len:
        return MatchCategory.GREEN
    elif blue_len > 0:
        return MatchCategory.BLUE
    return MatchCategory.BLACK


def _read_reverse_matrix_data(xlsx_path):
    """
    从逆向追踪矩阵Excel中读取所有数据(含颜色)

    通过直接解析XML获取富文本颜色信息

    Returns:
        list[dict]: 每行包含 seq, ds_id, ds_text, ds_runs, ds_category,
                    up_doc, up_ref, up_text, up_runs, up_category
    """
    with zipfile.ZipFile(xlsx_path) as z:
        # 读取sharedStrings
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.parse(z.open('xl/sharedStrings.xml')).getroot()
            for si in root.findall(f'{{{NS}}}si'):
                runs = si.findall(f'{{{NS}}}r')
                if runs:
                    shared.append([
                        (r.find(f'{{{NS}}}rPr').find(f'{{{NS}}}color').get('rgb', '')
                         if r.find(f'{{{NS}}}rPr') is not None
                            and r.find(f'{{{NS}}}rPr').find(f'{{{NS}}}color') is not None
                         else '',
                         r.find(f'{{{NS}}}t').text
                         if r.find(f'{{{NS}}}t') is not None
                            and r.find(f'{{{NS}}}t').text else '')
                        for r in runs
                    ])
                else:
                    t = si.find(f'{{{NS}}}t')
                    shared.append([('', t.text if t is not None and t.text else '')])

        # 读取第一个sheet (逆向追踪矩阵)
        sheet_path = 'xl/worksheets/sheet1.xml'
        if sheet_path not in z.namelist():
            raise ValueError(f"未找到逆向追踪矩阵sheet: {sheet_path}")

        root = ET.parse(z.open(sheet_path)).getroot()
        rows = []
        current_ds = None  # 当前下游条目追踪器 (处理合并单元格)

        for row_el in root.iter(f'{{{NS}}}row'):
            row_num = int(row_el.get('r', '0'))
            if row_num <= 1:
                continue

            cells = {}
            for cell_el in row_el.findall(f'{{{NS}}}c'):
                ref = cell_el.get('r', '')
                if not ref:
                    continue
                col_letter = ''.join(c for c in ref if c.isalpha())

                # 尝试读取inline string
                is_el = cell_el.find(f'{{{NS}}}is')
                if is_el is not None:
                    runs_xml = is_el.findall(f'{{{NS}}}r')
                    if runs_xml:
                        runs = []
                        for r in runs_xml:
                            rPr = r.find(f'{{{NS}}}rPr')
                            t = r.find(f'{{{NS}}}t')
                            text = t.text if t is not None and t.text else ''
                            color = ''
                            if rPr is not None:
                                c = rPr.find(f'{{{NS}}}color')
                                if c is not None:
                                    color = c.get('rgb', '')
                            runs.append((color, text))
                        full_text = ''.join(text for _, text in runs)
                        category = _dominant_category_from_runs(runs)
                        cells[col_letter] = {'text': full_text, 'runs': runs, 'category': category}
                        continue
                    else:
                        t_el = is_el.find(f'{{{NS}}}t')
                        if t_el is not None and t_el.text:
                            cells[col_letter] = {
                'text': t_el.text, 'runs': None, 'category': MatchCategory.BLACK,
                            }
                            continue

                # 尝试读取shared string
                ct = cell_el.get('t', '')
                v = cell_el.find(f'{{{NS}}}v')
                if ct == 's' and v is not None and v.text:
                    sidx = int(v.text)
                    if sidx < len(shared):
                        runs = shared[sidx]
                        full_text = ''.join(text for _, text in runs)
                        category = _dominant_category_from_runs(runs)
                        cells[col_letter] = {'text': full_text, 'runs': runs, 'category': category}
                        continue

                # 普通值
                if v is not None and v.text:
                    cells[col_letter] = {'text': v.text, 'runs': None, 'category': MatchCategory.BLACK}

            if not cells:
                continue

            # A=序号, B=下游ID(系统需求), C=下游内容, D=上游文档, E=上游引用(用户需求), F=上游内容
            seq = cells.get('A', {}).get('text', '')
            ds_id = cells.get('B', {}).get('text', '')
            ds_data = cells.get('C', {})
            up_doc = cells.get('D', {}).get('text', '')
            up_ref = cells.get('E', {}).get('text', '')
            up_data = cells.get('F', {})

            # 更新当前下游条目追踪器 (处理下游侧合并单元格)
            # 仅当ds_id和ds_text同时存在时才更新缓存
            # 防止B列有值但C列为空(合并单元格)时覆盖缓存的空文本
            ds_text_val = ds_data.get('text', '')
            if ds_id and ds_text_val:
                current_ds = {
                    'ds_id': ds_id,
                    'ds_text': ds_text_val,
                    'ds_runs': ds_data.get('runs'),
                    'ds_category': ds_data.get('category', MatchCategory.BLACK),
                }
            elif ds_id and not ds_text_val and current_ds:
                # B列有值但C列为空(合并单元格) -> 使用缓存的下游内容
                ds_data = {
                    'text': current_ds['ds_text'],
                    'runs': current_ds['ds_runs'],
                    'category': current_ds['ds_category'],
                }
            if not ds_id and current_ds:
                # B列为空(合并单元格) -> 使用缓存的下游条目号和内容
                ds_id = current_ds['ds_id']
                ds_data = {
                    'text': current_ds['ds_text'],
                    'runs': current_ds['ds_runs'],
                    'category': current_ds['ds_category'],
                }

            if not up_ref:
                continue

            rows.append({
                'seq': seq,
                'ds_id': ds_id,
                'ds_text': ds_data.get('text', ''),
                'ds_runs': ds_data.get('runs'),
                'ds_category': ds_data.get('category', MatchCategory.BLACK),
                'up_doc': up_doc,
                'up_ref': up_ref,
                'up_text': up_data.get('text', ''),
                'up_runs': up_data.get('runs'),
                'up_category': up_data.get('category', MatchCategory.BLACK),
            })

    return rows


# ============================================================
# PDF条目提取与引用匹配
# ============================================================

def _normalize_ref(ref):
    """归一化引用字符串: 去空格、统一全半角"""
    ref = ref.strip()
    ref = ref.replace('（', '(').replace('）', ')').replace('：', ':')
    ref = ref.replace('，', ',').replace('；', ';')
    ref = re.sub(r'\s+', '', ref)
    return ref


def _extract_pdf_items(pdf_path, doc_name=''):
    """
    从PDF中提取所有需求条目和章节

    - ID型文档: 仅提取需求条目, 不重复提取章节
    - 章节型文档: 提取章节, 标题截断至20字防止正文泄漏

    Returns:
        items: 每项含 key, content, section_number, section_title, item_id
    """
    from core.pdf_parser_adapter import extract_full_text
    from core.requirement_extractor import (
        extract_requirement_items, extract_sections,
        detect_document_type,
    )

    text = extract_full_text(pdf_path)
    doc_type = detect_document_type(text)
    items = []

    if doc_type == "id_based":
        # ID型文档: 仅按需求条目获取, 不再按章节号重复提取
        for item in extract_requirement_items(text):
            items.append({
                'key': item.item_id,
                'content': item.content,
                'section_number': '',
                'section_title': '',
                'item_id': item.item_id,
            })
    else:
        # 章节型文档: 提取章节, 标题截断防止正文泄漏
        _MAX_TITLE_LEN = 12
        for section in extract_sections(text):
            title = section.section_title
            if len(title) > _MAX_TITLE_LEN:
                # 策略1: 在句号/冒号处截断
                period_pos = title.find('。')
                colon_pos = title.find('：')
                cut = -1
                for pos in (period_pos, colon_pos):
                    if 4 <= pos <= _MAX_TITLE_LEN and (cut < 0 or pos < cut):
                        cut = pos
                if cut > 0:
                    title = title[:cut]
                else:
                    # 策略2: 在逗号处截断
                    comma_pos = title.find('，')
                    if 4 <= comma_pos <= _MAX_TITLE_LEN:
                        title = title[:comma_pos]
                    else:
                        title = title[:_MAX_TITLE_LEN]
            items.append({
                'key': f"{section.section_number} {title}",
                'content': section.content,
                'section_number': section.section_number,
                'section_title': title,
                'item_id': '',
            })

    return items


def _build_item_key_map(items):
    """
    构建归一化键→条目索引的映射

    支持多种键形式: item_id, section_number, section_number+title(带空格/不带空格), title
    """
    key_map = {}
    for idx, item in enumerate(items):
        keys = []
        if item.get('item_id'):
            keys.append(item['item_id'])
        if item.get('section_number'):
            keys.append(item['section_number'])
            if item.get('section_title'):
                # 同时注册带空格和不带空格两种格式
                keys.append(f"{item['section_number']} {item['section_title']}")
                keys.append(f"{item['section_number']}{item['section_title']}")
        if item.get('section_title'):
            keys.append(item['section_title'])
        keys.append(item['key'])

        for k in keys:
            nk = _normalize_ref(k)
            if nk and nk not in key_map:
                key_map[nk] = idx
    return key_map


def _match_ref_to_item(ref, key_map, items):
    """
    将引用字符串匹配到PDF条目

    策略: 精确匹配 → 子串匹配 → 章节号前缀匹配 → 模糊匹配
    """
    ref_norm = _normalize_ref(ref)

    # 1. 精确匹配
    if ref_norm in key_map:
        return key_map[ref_norm]

    # 2. 子串匹配 (要求双方长度≥5, 且非纯章节号, 避免父节抢匹配)
    if len(ref_norm) >= 5:
        for nk, idx in key_map.items():
            if len(nk) >= 5 and not re.match(r'^\d+(?:\.\d+)*$', nk) \
                    and (ref_norm in nk or nk in ref_norm):
                return idx

    # 3. 章节号前缀匹配
    ref_num_m = re.match(r'^(\d+(?:\.\d+)*)', ref_norm)
    if ref_num_m:
        ref_num = ref_num_m.group(1)
        best_idx = None
        best_len = 0
        for nk, idx in key_map.items():
            key_num_m = re.match(r'^(\d+(?:\.\d+)*)', nk)
            if key_num_m:
                kn = key_num_m.group(1)
                if kn == ref_num:
                    return idx
                if kn.startswith(ref_num) or ref_num.startswith(kn):
                    if len(kn) > best_len:
                        best_len = len(kn)
                        best_idx = idx
        if best_idx is not None:
            return best_idx

    # 4. 模糊匹配
    best_idx = None
    best_ratio = 0.0
    for nk, idx in key_map.items():
        ratio = difflib.SequenceMatcher(None, ref_norm, nk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_idx = idx
    if best_ratio >= 0.7:
        return best_idx

    return None


# ============================================================
# Union Coloring (组合合并着色)
# ============================================================

def _compute_union_coloring(runs_list, base_text):
    """
    计算多组runs的union着色

    对每个字符位置, 取所有runs中优先级最高的颜色:
    GREEN > BLUE > BLACK

    Args:
        runs_list: list of (list[(color_rgb, text)] or None)
        base_text: 用于对齐的完整文本

    Returns:
        list[(color_rgb, text)] 或 None
    """
    if not runs_list:
        return None

    # 过滤掉None和空runs
    valid_runs = [r for r in runs_list if r]
    if not valid_runs:
        return None

    if len(valid_runs) == 1:
        return valid_runs[0]

    # 将每组runs转为字符级颜色数组
    char_colors_list = []
    for runs in valid_runs:
        colors = []
        for color, text in runs:
            cat = _hex_to_category(color)
            for _ in text:
                colors.append(cat)
        char_colors_list.append(colors)

    max_len = max(len(c) for c in char_colors_list) if char_colors_list else 0
    if max_len == 0:
        return None

    # 计算union: 每个字符取最高优先级颜色
    priority = {MatchCategory.GREEN: 3, MatchCategory.BLUE: 2, MatchCategory.BLACK: 1}
    union_colors = []
    for i in range(max_len):
        best = MatchCategory.BLACK
        for colors in char_colors_list:
            if i < len(colors):
                c = colors[i]
                if priority.get(c, 0) > priority.get(best, 0):
                    best = c
        union_colors.append(best)

    # 转回runs, 用base_text对齐
    runs = []
    cur_rgb = None
    cur_text = []

    for i, ch in enumerate(base_text):
        cat = union_colors[i] if i < len(union_colors) else MatchCategory.BLACK
        rgb = _category_to_rgb(cat)

        if cur_rgb is None:
            cur_rgb = rgb
            cur_text = [ch]
        elif rgb == cur_rgb:
            cur_text.append(ch)
        else:
            runs.append((cur_rgb, ''.join(cur_text)))
            cur_rgb = rgb
            cur_text = [ch]

    if cur_text:
        runs.append((cur_rgb, ''.join(cur_text)))

    return runs


# ============================================================
# 正向矩阵数据构建
# ============================================================

def _build_forward_data(reverse_rows, pdf_items_by_doc):
    """
    构建正向矩阵数据

    1. 以PDF条目为基准 (完整覆盖)
    2. 匹配逆向矩阵行到PDF条目
    3. 有追踪: 收集所有up_runs, 计算union coloring
    4. 无追踪: 标记has_trace=False (下游填NA)

    Returns:
        OrderedDict: {(doc_name, item_key): {up_text, up_runs, up_category, sys_reqs, has_trace}}
    """
    forward_data = OrderedDict()

    # 为所有文档构建键映射
    doc_key_maps = {}
    for doc_name, pdf_items in pdf_items_by_doc.items():
        doc_key_maps[doc_name] = _build_item_key_map(pdf_items)

    # 将每条逆向矩阵行匹配到对应PDF条目 (跨文档搜索)
    # up_doc可能是目录名(如"系统需求")而非PDF文件名(如"DCS需求说明书")
    # 因此不能仅靠up_doc过滤, 需要尝试在所有文档中匹配up_ref
    doc_item_to_rows = {doc_name: defaultdict(list) for doc_name in pdf_items_by_doc}
    for row in reverse_rows:
        matched = False
        # 优先在up_doc对应的文档中查找
        for doc_name, pdf_items in pdf_items_by_doc.items():
            if row.get('up_doc', '') == doc_name or row.get('up_doc', '') in doc_name or doc_name in row.get('up_doc', ''):
                idx = _match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    matched = True
                    break
        # 如果up_doc不匹配任何文档名, 在所有文档中搜索
        if not matched:
            for doc_name, pdf_items in pdf_items_by_doc.items():
                idx = _match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    break

    for doc_name, pdf_items in pdf_items_by_doc.items():
        item_to_rows = doc_item_to_rows[doc_name]

        # 为每个PDF条目构建正向数据
        for item_idx, item in enumerate(pdf_items):
            key = (doc_name, item['key'])
            matching_rows = item_to_rows.get(item_idx, [])

            if matching_rows:
                # 有追踪关系
                all_up_runs = [r['up_runs'] for r in matching_rows]
                up_text = matching_rows[0]['up_text']
                union_runs = _compute_union_coloring(all_up_runs, up_text)

                if union_runs:
                    category = _dominant_category_from_runs(union_runs)
                else:
                    category = matching_rows[0]['up_category']

                sys_reqs = []
                for row in matching_rows:
                    sys_reqs.append({
                        'ds_id': row['ds_id'],
                        'ds_text': row['ds_text'],
                        'ds_runs': row['ds_runs'],
                        'ds_category': row['ds_category'],
                    })

                forward_data[key] = {
                    'up_text': up_text,
                    'up_runs': union_runs,
                    'up_category': category,
                    'sys_reqs': sys_reqs,
                    'has_trace': True,
                }
            else:
                # 无追踪关系
                forward_data[key] = {
                    'up_text': item['content'],
                    'up_runs': None,
                    'up_category': MatchCategory.BLACK,
                    'sys_reqs': [],
                    'has_trace': False,
                }

    return forward_data


def _split_by_user_doc(forward_data):
    """按用户需求文档名拆分"""
    by_doc = {}
    for (up_doc, up_ref), data in forward_data.items():
        if up_doc not in by_doc:
            by_doc[up_doc] = OrderedDict()
        by_doc[up_doc][(up_doc, up_ref)] = data
    return by_doc


# ============================================================
# Excel生成
# ============================================================

def _make_rich_text_cell(runs, default_text=''):
    """将runs列表转为CellRichText"""
    if not runs:
        return default_text

    blocks = []
    for color, text in runs:
        if not text:
            continue
        cat = _hex_to_category(color)
        if cat == MatchCategory.GREEN:
            ifont = _IF_GREEN
        elif cat == MatchCategory.BLUE:
            ifont = _IF_BLUE
        else:
            ifont = _IF_BLACK
        blocks.append(TextBlock(ifont, text))

    if not blocks:
        return default_text
    if len(blocks) == 1:
        return CellRichText(blocks[0])
    return CellRichText(*blocks)


def _set_cell(ws, row, col, value, font=None):
    """设置单元格值和样式"""
    cell = ws.cell(row=row, column=col, value=value)
    if font:
        cell.font = font
    cell.alignment = _DATA_ALIGNMENT
    cell.border = _THIN_BORDER
    return cell


def _category_to_description(category):
    """匹配类别转文字描述"""
    if category == MatchCategory.GREEN:
        return '一致'
    elif category == MatchCategory.BLUE:
        return '部分匹配'
    return '不一致'


def _write_header_row(ws, headers, widths):
    """写入表头行"""
    for col_idx, (header, width) in enumerate(zip(headers, widths), 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGNMENT
        cell.border = _THIN_BORDER
        col_letter = chr(64 + col_idx) if col_idx <= 26 else 'A'
        ws.column_dimensions[col_letter].width = width


def generate_forward_excel(forward_data_by_doc, output_path, sys_req_doc_name=''):
    """
    生成正向追踪矩阵Excel

    - 每份用户需求文档一个独立sheet
    - 上游内容(C列)合并, 使用union coloring
    - 下游内容(F列)按追踪关系独立着色
    - 无追踪关系的条目, 下游填"NA"
    """
    wb = Workbook()
    first_sheet = True

    for doc_name, entries in forward_data_by_doc.items():
        if first_sheet:
            ws = wb.active
            ws.title = doc_name[:31]
            first_sheet = False
        else:
            ws = wb.create_sheet(title=doc_name[:31])

        headers = ['序号', '上游条目号', '上游内容', '下游文档', '下游条目号', '下游内容']
        widths = [6, 22, 55, 20, 22, 55]
        _write_header_row(ws, headers, widths)

        row_num = 2
        seq = 1

        for (up_doc, up_ref), data in entries.items():
            sys_reqs = data['sys_reqs']
            has_trace = data['has_trace']

            if has_trace and sys_reqs:
                # 有追踪关系: 一行用户需求对应多行系统需求
                start_row = row_num

                for sr in sys_reqs:
                    # A: 序号
                    _set_cell(ws, row_num, 1, seq,
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # B: 上游条目号
                    _set_cell(ws, row_num, 2, up_ref,
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # C: 上游内容 (union coloring, 合并)
                    up_rich = _make_rich_text_cell(data['up_runs'], data['up_text'])
                    ws.cell(row=row_num, column=3, value=up_rich)
                    ws.cell(row=row_num, column=3).alignment = _DATA_ALIGNMENT
                    ws.cell(row=row_num, column=3).border = _THIN_BORDER

                    # D: 下游文档 (系统需求文档名)
                    doc_val = sys_req_doc_name if sys_req_doc_name else sr.get('up_doc', '')
                    _set_cell(ws, row_num, 4, doc_val,
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # E: 下游条目号 (系统需求条目号)
                    _set_cell(ws, row_num, 5, sr['ds_id'],
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # F: 下游内容 (系统需求内容, 按该条追踪关系着色)
                    ds_rich = _make_rich_text_cell(sr['ds_runs'], sr['ds_text'])
                    ws.cell(row=row_num, column=6, value=ds_rich)
                    ws.cell(row=row_num, column=6).alignment = _DATA_ALIGNMENT
                    ws.cell(row=row_num, column=6).border = _THIN_BORDER

                    row_num += 1

                end_row = row_num - 1

                # 合并 A, B, C 列 (序号、上游条目号、上游内容)
                if end_row > start_row:
                    for col in [1, 2, 3]:
                        ws.merge_cells(
                            start_row=start_row, start_column=col,
                            end_row=end_row, end_column=col,
                        )

                seq += 1
            else:
                # 无追踪关系: 下游填NA
                _set_cell(ws, row_num, 1, seq,
                          Font(name=FONT_NAME, size=ID_FONT_SIZE))
                _set_cell(ws, row_num, 2, up_ref,
                          Font(name=FONT_NAME, size=ID_FONT_SIZE))

                # C: 上游内容 (来自PDF, 无着色)
                _set_cell(ws, row_num, 3, data['up_text'],
                          Font(name=FONT_NAME, size=CONTENT_FONT_SIZE))

                # D, E, F: NA
                na_font = Font(name=FONT_NAME, size=ID_FONT_SIZE, color='999999')
                _set_cell(ws, row_num, 4, 'NA', na_font)
                _set_cell(ws, row_num, 5, 'NA', na_font)
                _set_cell(ws, row_num, 6, 'NA', na_font)

                row_num += 1
                seq += 1

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    wb.save(output_path)


# ============================================================
# 主入口
# ============================================================

def generate_forward_matrix(
    reverse_matrix_path,
    user_pdf_dir,
    output_path,
    sys_req_dir=None,
):
    """
    从已完成的逆向追踪矩阵生成正向追踪矩阵

    流程:
    1. 读取逆向矩阵Excel, 提取追踪关系及匹配颜色
    2. 从用户需求PDF提取所有章节/条目 (完整覆盖)
    3. 匹配逆向矩阵行到PDF条目, 计算union coloring
    4. 按用户需求文档拆分, 每份文档一个sheet
    5. 生成Excel, 上游内容合并着色, 未追踪条目下游填NA

    Args:
        reverse_matrix_path: 逆向追踪矩阵Excel路径 (含匹配着色)
        user_pdf_dir: 用户需求PDF目录路径
        output_path: 输出正向矩阵Excel路径
        sys_req_dir: 系统需求PDF目录路径 (用于获取下游文档名)
    """
    # 0. 获取系统需求文档名
    sys_req_doc_name = ''
    if sys_req_dir and os.path.isdir(sys_req_dir):
        for fname in os.listdir(sys_req_dir):
            if fname.lower().endswith('.pdf'):
                sys_req_doc_name = fname.replace('.pdf', '')
                break

    # 1. 读取逆向矩阵数据
    print(">>> 读取逆向追踪矩阵数据...")
    reverse_rows = _read_reverse_matrix_data(reverse_matrix_path)
    print(f"    读取到 {len(reverse_rows)} 条追踪关系")

    # 2. 从上游PDF提取所有条目 (仅搜索user_pdf_dir)
    print(">>> 提取上游PDF条目...")
    pdf_items_by_doc = {}
    if user_pdf_dir and os.path.isdir(user_pdf_dir):
        # 多份文档先并行预解析, 下面的循环直接命中缓存
        prefetch_pdfs({
            f.replace('.pdf', ''): os.path.join(user_pdf_dir, f)
            for f in sorted(os.listdir(user_pdf_dir)) if f.lower().endswith('.pdf')
        })
        for fname in sorted(os.listdir(user_pdf_dir)):
            if not fname.lower().endswith('.pdf'):
                continue
            doc_name = fname.replace('.pdf', '')
            pdf_path = os.path.join(user_pdf_dir, fname)
            items = _extract_pdf_items(pdf_path, doc_name)
            pdf_items_by_doc[doc_name] = items
            print(f"    [{doc_name}]: {len(items)} 个条目")

    # 3. 构建正向矩阵数据 (匹配 + union coloring)
    print(">>> 构建正向矩阵数据 (匹配 + union coloring)...")
    forward_data = _build_forward_data(reverse_rows, pdf_items_by_doc)

    # 统计
    traced = sum(1 for d in forward_data.values() if d['has_trace'])
    untraced = len(forward_data) - traced
    total_sys = sum(len(d['sys_reqs']) for d in forward_data.values() if d['has_trace'])
    print(f"    总条目: {len(forward_data)}, 有追踪: {traced}, 未追踪: {untraced}")
    print(f"    系统需求追踪关系: {total_sys}")

    # 4. 按文档拆分
    by_doc = _split_by_user_doc(forward_data)

    # 过滤: 仅保留用户需求目录中存在的PDF文档
    user_doc_names = set(pdf_items_by_doc.keys())
    if user_doc_names:
        by_doc = {k: v for k, v in by_doc.items() if k in user_doc_names}

    for doc_name, entries in by_doc.items():
        n = len(entries)
        t = sum(1 for d in entries.values() if d['has_trace'])
        print(f"    [{doc_name}]: {n} 个条目 ({t} 有追踪, {n-t} 未追踪)")

    # 5. 生成Excel
    print(f">>> 生成正向追踪矩阵Excel...")
    generate_forward_excel(by_doc, output_path, sys_req_doc_name)
    print(f"    已保存至 {output_path}")

    return {
        'total_entries': len(forward_data),
        'traced': traced,
        'untraced': untraced,
        'total_sys_reqs': total_sys,
        'doc_count': len(by_doc),
    }


if __name__ == '__main__':
    from config import PROJECT_ROOT, OUTPUT_DIR, make_output_path

    reverse_matrix_path = os.path.join(OUTPUT_DIR, "追踪验证结果_v5.xlsx")
    user_pdf_dir = os.path.join(PROJECT_ROOT, "用户需求")
    sys_req_dir = os.path.join(PROJECT_ROOT, "系统需求")
    output_path = make_output_path("正向追踪矩阵.xlsx")

    result = generate_forward_matrix(
        reverse_matrix_path, user_pdf_dir, output_path, sys_req_dir,
    )
    print(f"\n=== 正向追踪矩阵生成完成 ===")
    print(f"  文档数: {result['doc_count']}")
    print(f"  总条目: {result['total_entries']}")
    print(f"  有追踪: {result['traced']}, 未追踪: {result['untraced']}")
    print(f"  系统需求追踪: {result['total_sys_reqs']}")
