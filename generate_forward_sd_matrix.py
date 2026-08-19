"""
正向追踪矩阵生成模块（系统需求 → 系统设计）

从已完成的逆向追踪矩阵Excel（系统设计→系统需求）中提取匹配数据，
结合系统需求PDF的完整条目结构，生成正向追踪矩阵（系统需求 → 系统设计）。

核心逻辑:
1. 从系统需求PDF提取所有条目 (完整覆盖)
2. 从逆向矩阵获取追踪关系和匹配着色
3. 按系统需求条目分组, 上游内容合并着色 (union coloring: GREEN > BLUE > BLACK)
4. 未追踪的系统需求条目, 下游填"NA"
"""
from __future__ import annotations
import os
import sys
import re
import zipfile
import xml.etree.ElementTree as ET
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

        # 读取所有sheet (逆向追踪矩阵可能有多个sheet, 每份设计文档一个)
        sheet_paths = sorted([n for n in z.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')])
        if not sheet_paths:
            raise ValueError("未找到逆向追踪矩阵sheet")

        all_roots = [ET.parse(z.open(sp)).getroot() for sp in sheet_paths]
        rows = []

        for root in all_roots:
          current_ds = None  # 每个sheet重置合并单元格追踪器
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

            # A=序号, B=下游ID(系统设计), C=下游内容, D=上游文档(系统需求), E=上游引用(系统需求条目号), F=上游内容, G=下游文档(实际来源文档名)
            seq = cells.get('A', {}).get('text', '')
            ds_id = cells.get('B', {}).get('text', '')
            ds_data = cells.get('C', {})
            up_doc = cells.get('D', {}).get('text', '')
            up_ref = cells.get('E', {}).get('text', '')
            up_data = cells.get('F', {})
            ds_doc = cells.get('G', {}).get('text', '')

            # 更新当前下游条目追踪器 (处理下游侧合并单元格)
            # 仅当ds_id和ds_text同时存在时才更新缓存
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
                'ds_doc': ds_doc,
                'up_doc': up_doc,
                'up_ref': up_ref,
                'up_text': up_data.get('text', ''),
                'up_runs': up_data.get('runs'),
                'up_category': up_data.get('category', MatchCategory.BLACK),
            })

    return rows


# ============================================================
# 系统需求条目提取与引用匹配
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
    从系统需求PDF中提取所有需求条目。

    系统需求文档是ID型文档，因此只提取需求条目，不按章节重复提取

    Returns:
        items: 条目字典列表
    """
    from core.pdf_parser_adapter import extract_full_text
    from core.requirement_extractor import extract_requirement_items, detect_document_type

    text = extract_full_text(pdf_path)
    doc_type = detect_document_type(text)
    items = []

    if doc_type == "id_based":
        for item in extract_requirement_items(text):
            items.append({
                'key': item.item_id,
                'content': item.content,
                'section_number': '',
                'section_title': '',
                'item_id': item.item_id,
            })
    else:
        # 系统需求文档通常是ID型，但兜底处理章节型
        from core.requirement_extractor import extract_sections
        _MAX_TITLE_LEN = 12
        for section in extract_sections(text):
            title = section.section_title
            if len(title) > _MAX_TITLE_LEN:
                period_pos = title.find('。')
                colon_pos = title.find('：')
                cut = -1
                for pos in (period_pos, colon_pos):
                    if 4 <= pos <= _MAX_TITLE_LEN and (cut < 0 or pos < cut):
                        cut = pos
                if cut > 0:
                    title = title[:cut]
                else:
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

    # 4. 模糊匹配 (difflib)
    import difflib
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
# Union Coloring
# ============================================================

def _textruns_to_tuples(runs):
    """将 TextRun 列表转为 (rgb_hex, text) 元组列表, 供 union coloring / rich text 复用"""
    out = []
    for r in runs:
        rgb = _category_to_rgb(r.category)
        out.append((rgb, r.text))
    return out


def _compute_union_coloring(all_up_runs_list, base_text):
    """
    计算Union着色: 多条追踪关系的上游内容按字符取最高优先级颜色

    优先级: GREEN(3) > BLUE(2) > BLACK(1)
    """
    priority = {MatchCategory.GREEN: 3, MatchCategory.BLUE: 2, MatchCategory.BLACK: 1}
    max_len = len(base_text)
    char_colors = [MatchCategory.BLACK] * max_len

    for runs in all_up_runs_list:
        if not runs:
            continue
        pos = 0
        for color, text in runs:
            cat = _hex_to_category(color)
            for k in range(len(text)):
                if pos + k < max_len and priority.get(cat, 0) > priority.get(char_colors[pos + k], 0):
                    char_colors[pos + k] = cat
            pos += len(text)

    # 转回runs
    runs_out = []
    cur_rgb = None
    cur_text = []
    for i, ch in enumerate(base_text):
        cat = char_colors[i] if i < len(char_colors) else MatchCategory.BLACK
        rgb = _category_to_rgb(cat)
        if cur_rgb is None:
            cur_rgb = rgb
            cur_text = [ch]
        elif rgb == cur_rgb:
            cur_text.append(ch)
        else:
            runs_out.append((cur_rgb, ''.join(cur_text)))
            cur_rgb = rgb
            cur_text = [ch]
    if cur_text:
        runs_out.append((cur_rgb, ''.join(cur_text)))
    return runs_out


# ============================================================
# 正向矩阵数据构建
# ============================================================

def _build_traced_entry(up_text, design_rows):
    """
    构建"有追踪关系"的正向条目, 并对每条(上游内容, 下游内容)对重新执行微块级文本匹配,
    不直接沿用逆向矩阵中保存的原匹配结果。

    Args:
        up_text: 上游(系统需求)完整内容
        design_rows: 与该上游条目关联的所有逆向矩阵行(每条含 ds_id/ds_text/ds_doc)

    Returns:
        dict: 含 up_runs(union着色), design_items(各自重新匹配的下游着色) 等
    """
    # 延迟导入: 仅在需要重新匹配时才加载匹配模型
    from core.text_matcher import match_text_pair

    up_text_full = up_text or ''
    design_items = []
    all_up_run_tuples = []

    for row in design_rows:
        ds_text = row['ds_text']
        # 重新匹配上游(系统需求)与下游(系统设计)内容
        mr = match_text_pair(ds_text, up_text_full)
        ds_run_tuples = _textruns_to_tuples(mr.downstream_runs)
        up_run_tuples = _textruns_to_tuples(mr.upstream_runs)
        design_items.append({
            'ds_id': row['ds_id'],
            'ds_text': ds_text,
            'ds_runs': ds_run_tuples,
            'ds_category': _dominant_category_from_runs(ds_run_tuples),
            'ds_doc': row.get('ds_doc', ''),
        })
        all_up_run_tuples.append(up_run_tuples)

    union_runs = _compute_union_coloring(all_up_run_tuples, up_text_full) if up_text_full else []

    return {
        'up_text': up_text_full,
        'up_runs': union_runs,
        'up_category': _dominant_category_from_runs(union_runs) if union_runs else MatchCategory.BLACK,
        'design_items': design_items,
        'has_trace': True,
    }


def _build_forward_data(reverse_rows, pdf_items_by_doc):
    """
    构建正向矩阵数据

    1. 以系统需求PDF完整条目为基准 (完整覆盖, 全部条目均补全内容)
    2. 匹配逆向矩阵行到PDF条目, 有追踪则对每条(上游,下游)重新执行文本匹配并合并着色
    3. 无追踪: 标记has_trace=False (下游填NA), 上游内容仍为PDF完整内容
    4. 逆向矩阵中引用匹配不到任何PDF条目的"孤儿"上游项, 用逆向矩阵记录的内容补全,
       避免追踪关系丢失 (满足"上游条目缺失需补全")

    Returns:
        OrderedDict: {(doc_name, item_key): {up_text, up_runs, up_category, design_items, has_trace}}
    """
    from core.text_matcher import match_text_pair  # 重新匹配需要

    forward_data = OrderedDict()

    # 为所有文档构建键映射
    doc_key_maps = {}
    for doc_name, pdf_items in pdf_items_by_doc.items():
        doc_key_maps[doc_name] = _build_item_key_map(pdf_items)

    # 将每条逆向矩阵行匹配到对应PDF条目 (跨文档搜索)
    doc_item_to_rows = {doc_name: defaultdict(list) for doc_name in pdf_items_by_doc}
    orphan_rows = []  # 引用匹配不到任何PDF条目的逆向行
    for row in reverse_rows:
        matched = False
        for doc_name, pdf_items in pdf_items_by_doc.items():
            if row.get('up_doc', '') == doc_name or row.get('up_doc', '') in doc_name or doc_name in row.get('up_doc', ''):
                idx = _match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    matched = True
                    break
        if not matched:
            for doc_name, pdf_items in pdf_items_by_doc.items():
                idx = _match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    matched = True
                    break
        if not matched:
            orphan_rows.append(row)

    # 1) 以PDF完整条目为基准构建正向数据
    for doc_name, pdf_items in pdf_items_by_doc.items():
        item_to_rows = doc_item_to_rows[doc_name]
        for item_idx, item in enumerate(pdf_items):
            key = (doc_name, item['key'])
            matching_rows = item_to_rows.get(item_idx, [])
            if matching_rows:
                # 上游内容统一使用PDF完整内容 (若为空则回退逆向矩阵记录内容)
                up_text_full = item['content'] or matching_rows[0]['up_text']
                entry = _build_traced_entry(up_text_full, matching_rows)
                forward_data[key] = entry
            else:
                # 无追踪关系: 上游内容仍为PDF完整内容, 下游填NA
                forward_data[key] = {
                    'up_text': item['content'],
                    'up_runs': None,
                    'up_category': MatchCategory.BLACK,
                    'design_items': [],
                    'has_trace': False,
                }

    # 2) 补全孤儿上游条目: 引用匹配不到PDF条目, 用逆向矩阵记录的内容补全, 保留追踪关系
    if orphan_rows:
        orphan_groups = defaultdict(list)
        for row in orphan_rows:
            up_doc = row.get('up_doc', '')
            target_doc = None
            for dn in pdf_items_by_doc:
                if dn == up_doc or up_doc in dn or dn in up_doc:
                    target_doc = dn
                    break
            if target_doc is None:
                # 单上游文档场景: 归入唯一文档
                target_doc = next(iter(pdf_items_by_doc))
            orphan_groups[(target_doc, row['up_ref'])].append(row)

        for (up_doc, up_ref), rows in orphan_groups.items():
            key = (up_doc, up_ref)
            if key in forward_data:
                # 与已存在条目合并: 收集全部设计行后整体重新匹配(保证union着色正确)
                existing = forward_data[key]
                combined = [
                    {'ds_id': d['ds_id'], 'ds_text': d['ds_text'], 'ds_doc': d.get('ds_doc', '')}
                    for d in existing['design_items']
                ]
                combined.extend(rows)
                forward_data[key] = _build_traced_entry(existing['up_text'], combined)
                continue
            # 用逆向矩阵记录的上游内容补全该缺失条目
            up_text = rows[0].get('up_text', '')
            forward_data[key] = _build_traced_entry(up_text, rows)

    return forward_data


def _split_by_doc(forward_data):
    """按文档名拆分"""
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


def _estimate_visible_lines(text, col_width, font_size=CONTENT_FONT_SIZE):
    """估算文本在指定列宽(Excel字符单位)下 wrap_text 后的可见行数。

    Excel 列宽 1 单位 ≈ 1 个半角字符宽度; 全角/CJK 字符按 2 单位计。
    wrap_text 时文本按列宽自动折行, 显式换行(\\n)强制分段。
    """
    if not text:
        return 1
    chars_per_line = max(4.0, col_width - 2.0)
    total = 0
    for seg in str(text).split('\n'):
        if not seg:
            total += 1
            continue
        w = 0.0
        for ch in seg:
            w += 2.0 if ord(ch) > 0x7F else 1.0
        total += max(1, -(-int(w) // int(chars_per_line)))
    return max(1, total)


def _set_row_height(ws, row, texts_with_widths, min_height=15.0,
                    line_height=17.0, pad=4.0):
    """根据单元格内容估算并设置行高, 使 wrap_text 多行可见。"""
    max_lines = 1
    for text, col_width in texts_with_widths:
        if text:
            max_lines = max(max_lines, _estimate_visible_lines(text, col_width))
    ws.row_dimensions[row].height = max(min_height, max_lines * line_height + pad)


def _set_merged_row_heights(ws, first_row, last_row, texts_with_widths,
                            min_height=15.0, line_height=17.0, pad=4.0):
    """合并单元格区域的行高按行数平均分摊。"""
    max_lines = 1
    for text, col_width in texts_with_widths:
        if text:
            max_lines = max(max_lines, _estimate_visible_lines(text, col_width))
    total_height = max(min_height, max_lines * line_height + pad)
    n = max(1, last_row - first_row + 1)
    per_row = max(min_height, total_height / n)
    for r in range(first_row, last_row + 1):
        ws.row_dimensions[r].height = per_row


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


def generate_forward_excel(forward_data_by_doc, output_path, design_doc_names=None):
    """
    生成正向追踪矩阵Excel

    - 每份上游文档一个独立sheet (当前为系统需求)
    - 上游内容(C列)合并, 使用union coloring
    - 下游内容(F列)按追踪关系独立着色
    - 无追踪关系的条目, 下游填"NA"
    - 下游文档名(D列)显示系统设计文档名
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
            design_items = data['design_items']
            has_trace = data['has_trace']

            if has_trace and design_items:
                # 有追踪关系: 一行系统需求对应多行系统设计
                start_row = row_num

                for di in design_items:
                    # A: 序号
                    _set_cell(ws, row_num, 1, seq,
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # B: 上游条目号 (系统需求条目号)
                    _set_cell(ws, row_num, 2, up_ref,
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # C: 上游内容 (union coloring, 合并)
                    up_rich = _make_rich_text_cell(data['up_runs'], data['up_text'])
                    ws.cell(row=row_num, column=3, value=up_rich)
                    ws.cell(row=row_num, column=3).alignment = _DATA_ALIGNMENT
                    ws.cell(row=row_num, column=3).border = _THIN_BORDER

                    # D: 下游文档 (系统设计文档名)
                    # 优先使用逆向矩阵中记录的实际来源文档名(ds_doc),
                    # 仅在缺失时回退到从条目号前缀推断
                    doc_val = di.get('ds_doc') or _infer_design_doc_name(di['ds_id'], design_doc_names)
                    _set_cell(ws, row_num, 4, doc_val,
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # E: 下游条目号 (系统设计条目号)
                    _set_cell(ws, row_num, 5, di['ds_id'],
                              Font(name=FONT_NAME, size=ID_FONT_SIZE))

                    # F: 下游内容 (系统设计内容, 按该条追踪关系着色)
                    ds_rich = _make_rich_text_cell(di['ds_runs'], di['ds_text'])
                    ws.cell(row=row_num, column=6, value=ds_rich)
                    ws.cell(row=row_num, column=6).alignment = _DATA_ALIGNMENT
                    ws.cell(row=row_num, column=6).border = _THIN_BORDER

                    # 数据行行高(临时按当前行内容估算, 合并组在下方统一分摊覆盖)
                    _set_row_height(ws, row_num, [
                        (data['up_text'], 55),
                        (di.get('ds_text', ''), 55),
                    ])

                    row_num += 1

                end_row = row_num - 1

                # 合并 A, B, C 列 (序号、上游条目号、上游内容)
                if end_row > start_row:
                    for col in [1, 2, 3]:
                        ws.merge_cells(
                            start_row=start_row, start_column=col,
                            end_row=end_row, end_column=col,
                        )
                    # 合并组行高: 以上游内容为基准, 按行数分摊
                    _set_merged_row_heights(ws, start_row, end_row, [
                        (data['up_text'], 55),
                    ])

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

                # 数据行行高: 上游内容按换行/列宽估算
                _set_row_height(ws, row_num, [
                    (data['up_text'], 55),
                ])

                row_num += 1
                seq += 1

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    wb.save(output_path)


def _infer_design_doc_name(design_item_id, design_doc_names):
    """
    从系统设计条目号推断所属的系统设计文档名

    系统设计条目号通常包含文档标识，如 <DESIGN-A001> 中的 "DESIGN-A"
    实际实现时根据项目情况调整

    Args:
        design_item_id: 系统设计条目号 (如 <DCS-DS001>)
        design_doc_names: 已知的系统设计文档名列表

    Returns:
        str: 推断出的文档名，或空字符串
    """
    if not design_item_id or not design_doc_names:
        return ''

    # 尝试从条目ID中提取文档标识前缀
    # 例如 <DCS-DS001> 中的 "DCS" 或 "DCS-DS"
    id_upper = design_item_id.upper()
    for doc_name in design_doc_names:
        # 检查文档名是否出现在条目ID中（去空格后）
        doc_key = doc_name.replace(' ', '').upper()
        if doc_key[:4] in id_upper or id_upper[:4] in doc_key:
            return doc_name

    # 兜底: 返回第一个文档名
    return design_doc_names[0] if design_doc_names else ''


# ============================================================
# 主入口
# ============================================================

def generate_forward_sd_matrix(
    reverse_matrix_path,
    sys_req_pdf_dir,
    output_path,
    design_pdf_dir=None,
):
    """
    从已完成的逆向追踪矩阵生成正向追踪矩阵（系统需求 → 系统设计）

    流程:
    1. 读取逆向矩阵Excel, 提取追踪关系及匹配颜色
    2. 从系统需求PDF提取所有条目 (完整覆盖)
    3. 匹配逆向矩阵行到系统需求条目, 计算union coloring
    4. 按文档拆分, 生成Excel
    5. 未追踪条目下游填NA

    Args:
        reverse_matrix_path: 逆向追踪矩阵Excel路径 (含匹配着色)
        sys_req_pdf_dir: 系统需求PDF目录路径
        output_path: 输出正向矩阵Excel路径
        design_pdf_dir: 系统设计PDF目录路径 (用于获取下游文档名)
    """
    # 0. 获取系统设计文档名列表
    design_doc_names = []
    if design_pdf_dir and os.path.isdir(design_pdf_dir):
        for fname in sorted(os.listdir(design_pdf_dir)):
            if fname.lower().endswith('.pdf'):
                design_doc_names.append(fname.replace('.pdf', ''))

    # 1. 读取逆向矩阵数据
    print(">>> 读取逆向追踪矩阵数据...")
    reverse_rows = _read_reverse_matrix_data(reverse_matrix_path)
    print(f"    读取到 {len(reverse_rows)} 条追踪关系")

    # 2. 从系统需求PDF提取所有条目
    print(">>> 提取系统需求PDF条目...")
    pdf_items_by_doc = {}
    if os.path.isdir(sys_req_pdf_dir):
        # 多份文档先并行预解析, 下面的循环直接命中缓存
        prefetch_pdfs({
            f.replace('.pdf', ''): os.path.join(sys_req_pdf_dir, f)
            for f in sorted(os.listdir(sys_req_pdf_dir)) if f.lower().endswith('.pdf')
        })
        for fname in sorted(os.listdir(sys_req_pdf_dir)):
            if not fname.lower().endswith('.pdf'):
                continue
            doc_name = fname.replace('.pdf', '')
            pdf_path = os.path.join(sys_req_pdf_dir, fname)
            items = _extract_pdf_items(pdf_path, doc_name)
            pdf_items_by_doc[doc_name] = items
            print(f"    [{doc_name}]: {len(items)} 个条目")

    # 3. 构建正向矩阵数据 (匹配 + union coloring)
    print(">>> 构建正向矩阵数据 (匹配 + union coloring)...")
    forward_data = _build_forward_data(reverse_rows, pdf_items_by_doc)

    # 统计
    traced = sum(1 for d in forward_data.values() if d['has_trace'])
    untraced = len(forward_data) - traced
    total_design = sum(len(d['design_items']) for d in forward_data.values() if d['has_trace'])
    print(f"    总条目: {len(forward_data)}, 有追踪: {traced}, 未追踪: {untraced}")
    print(f"    系统设计追踪关系: {total_design}")

    # 4. 按文档拆分
    by_doc = _split_by_doc(forward_data)

    # 过滤: 仅保留系统需求目录中存在的PDF文档
    doc_names = set(pdf_items_by_doc.keys())
    if doc_names:
        by_doc = {k: v for k, v in by_doc.items() if k in doc_names}

    for doc_name, entries in by_doc.items():
        n = len(entries)
        t = sum(1 for d in entries.values() if d['has_trace'])
        print(f"    [{doc_name}]: {n} 个条目 ({t} 有追踪, {n-t} 未追踪)")

    # 5. 生成Excel
    print(f">>> 生成正向追踪矩阵Excel...")
    generate_forward_excel(by_doc, output_path, design_doc_names)
    print(f"    已保存至 {output_path}")

    return {
        'total_entries': len(forward_data),
        'traced': traced,
        'untraced': untraced,
        'total_design_items': total_design,
        'doc_count': len(by_doc),
    }


if __name__ == '__main__':
    from config import PROJECT_ROOT, OUTPUT_DIR, make_output_path, SYSTEM_DESIGN_DIR, SYSTEM_REQUIREMENT_DIR

    reverse_matrix_path = os.path.join(OUTPUT_DIR, "追踪验证结果_系统设计.xlsx")
    sys_req_pdf_dir = SYSTEM_REQUIREMENT_DIR
    design_pdf_dir = SYSTEM_DESIGN_DIR
    output_path = make_output_path("正向追踪矩阵_系统设计.xlsx")

    # 确保目录存在
    if not os.path.isdir(sys_req_pdf_dir):
        print(f"[ERROR] 系统需求PDF目录不存在: {sys_req_pdf_dir}")
        sys.exit(1)
    if not os.path.isdir(design_pdf_dir):
        print(f"[WARN] 系统设计PDF目录不存在: {design_pdf_dir}")
        design_pdf_dir = None

    if not os.path.exists(reverse_matrix_path):
        print(f"[ERROR] 逆向矩阵文件不存在: {reverse_matrix_path}")
        print("请先运行逆向矩阵生成脚本。")
        sys.exit(1)

    result = generate_forward_sd_matrix(
        reverse_matrix_path, sys_req_pdf_dir, output_path, design_pdf_dir,
    )
    print(f"\n=== 正向追踪矩阵（系统需求→系统设计）生成完成 ===")
    print(f"  文档数: {result['doc_count']}")
    print(f"  总条目: {result['total_entries']}")
    print(f"  有追踪: {result['traced']}, 未追踪: {result['untraced']}")
    print(f"  系统设计追踪: {result['total_design_items']}")
