from __future__ import annotations
"""
PDF 解析适配器层

策略:
- 优先使用新模块(pdfparser)进行结构化提取(五层管道: 章节层级/列表/表格/图片)
- 降级回退到原始模块(core/pdf_parser)保证已有功能不受影响
- 对外接口与 core/pdf_parser 完全一致, 下游模块无需改动

后端选择(环境变量 TRACE_NL_PDF_BACKEND):
- auto(默认): 优先新模块, 失败时降级
- new: 强制使用新模块
- legacy: 强制使用原始模块
"""
import os
import re
import threading
from collections import OrderedDict
from typing import Optional

from config import ITEM_ID_REGEX, PDF_BACKEND

# 中间产物 dump(便于排查解析/提取问题)
from core.debug_dump import (
    dump_document_result, dump_page_texts, dump_tables, dump_trace_table,
)

# ============================================================
# 复用原始模块的 dataclass 和解析函数(不变)
# ============================================================
from core.pdf_parser import (
    PageText,
    TableData,
    TraceRelation,
    # 表格解析函数直接复用(接收原始 TableData 格式, 与解析引擎无关)
    parse_traceability_table,
    parse_traceability_table_design,
    # 文本清洗正则和辅助函数
    _clean_cell,
    _CJK_LATIN_SPACE_RE,
    _LATIN_DIGIT_SPACE_RE,
    _LIST_MARKER_SPACE_RE,
    _PUA_RE,
    _LONELY_BULLET_RE,
    _SECTION_HEADING_RE,
    # 页眉页脚正则(新模块的跨页重复检测无法覆盖页码每页不同的场景)
    _HEADER_FOOTER_RE,
    _PAGE_NUMBER_RE,
    _DOC_NAME_PAGE_RE,
    _CLASSIFICATION_RE,
    _VERSION_RE,
    _PAGE_LABEL_RE,
    _DOC_NUMBER_RE,
)

# 原始模块函数(降级用)
from core.pdf_parser import (
    extract_body_text as _legacy_extract_body_text,
    extract_full_text as _legacy_extract_full_text,
    extract_tables as _legacy_extract_tables,
    find_traceability_table as _legacy_find_traceability_table,
    find_traceability_table_design as _legacy_find_traceability_table_design,
    prefetch_pdfs as _legacy_prefetch_pdfs,
    clear_pdf_cache as _legacy_clear_pdf_cache,
    _pdf_fingerprint,
    _cache_get,
    _cache_put,
)

# 尝试导入新解析模块
try:
    from pdfparser import DocumentParser, DocumentResult, Block
    _NEW_PARSER_AVAILABLE = True
except ImportError:
    _NEW_PARSER_AVAILABLE = False

# 后端选择(单一事实来源: config.PDF_BACKEND, 其初值读自 TRACE_NL_PDF_BACKEND 环境变量)
_BACKEND = PDF_BACKEND

# 新模块解析结果缓存(DocumentResult + PageText 双层缓存)
_CACHE_MAX_DOCS = 24
_cache_lock = threading.Lock()
_result_cache: OrderedDict = OrderedDict()    # pdf指纹 -> DocumentResult
_pagetext_cache: OrderedDict = OrderedDict()   # pdf指纹 -> list[PageText]
# 显式失败标记缓存(扫描件/解析异常): 指纹 -> 失败原因。
# _cache_get 无法区分"缓存了None"与"未缓存", 失败单独存,
# 避免每次调用都重付一遍完整的失败解析代价。
_fail_cache: OrderedDict = OrderedDict()


def _use_new_parser() -> bool:
    """是否使用新解析模块"""
    if _BACKEND == "legacy":
        return False
    if _BACKEND == "new":
        return True
    return _NEW_PARSER_AVAILABLE  # auto


# ============================================================
# 文本清洗辅助(与原始模块行为一致)
# ============================================================

_CJK_EXTRA = set("，。；：""''《》（）…—！？、·【】")


def _is_cjk(ch: str) -> bool:
    """判断字符是否为CJK字符或中文标点"""
    if not ch:
        return False
    return '\u4e00' <= ch <= '\u9fff' or ch in _CJK_EXTRA


def _join_lines_compat(text: str) -> str:
    """
    合并块内行: CJK相邻不插空格, Latin间插空格.
    保留条目ID行、章节标题行、列表项的换行边界.

    与原始模块的 _merge_intra_sentence_breaks 保持一致的换行守卫,
    确保 requirement_extractor 的正则匹配不受影响.
    """
    lines = text.split('\n')
    if len(lines) <= 1:
        return text.strip()

    # 预扫描: 标记章节标题行(在合并之前)
    heading_flags = [False] * len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and _SECTION_HEADING_RE.match(stripped):
            heading_flags[i] = True

    merged = [lines[0].strip()]
    for i in range(1, len(lines)):
        prev = merged[-1]
        curr = lines[i].strip()
        if not curr:
            continue
        if not prev:
            merged.append(curr)
            continue

        # 守卫1: 条目ID所在行不与下一行合并(保留条目号行与正文的换行)
        if re.search(ITEM_ID_REGEX, prev):
            merged.append(curr)
            continue

        # 守卫2: 下一行是章节标题 → 保留换行
        if heading_flags[i]:
            merged.append(curr)
            continue

        # 守卫3: 下一行是列表项 → 保留换行
        if re.match(r'^\s*(\d+[)）.]\s|[a-zA-Z][)）]\s|[-•●](?:\s|(?=[\u4e00-\u9fff])))', curr):
            merged.append(curr)
            continue

        # 守卫4: 句号/冒号后保留换行
        if prev[-1] in '。：:':
            merged.append(curr)
            continue

        # 合并: CJK相邻不插空格, 其余插空格
        a, b = prev[-1], curr[0]
        if _is_cjk(a) or _is_cjk(b):
            merged[-1] = prev + curr
        else:
            merged[-1] = prev + ' ' + curr

    return '\n'.join(merged)


def _clean_cjk_spaces(text: str) -> str:
    """消除CJK与Latin/Number、Latin与Digit、列表编号后的PDF伪空格"""
    text = _CJK_LATIN_SPACE_RE.sub('', text)
    text = _LATIN_DIGIT_SPACE_RE.sub('', text)
    text = _LIST_MARKER_SPACE_RE.sub(r'\1', text)
    return text


def _clean_pua(text: str) -> str:
    """去除PDF私有区Unicode字符"""
    return _PUA_RE.sub('', text)


_ListMark = re.compile(r'^\s*([-•●·○■□►◆\-])\s*$')

# 跨页重复行过滤(页眉页脚安全网)阈值:
# 短行在 >= max(3, 页数×比例) 页上原样出现 且 跨度覆盖文档首尾 -> 判为页眉页脚
_HF_REPEAT_RATIO = 0.35
_HF_REPEAT_MIN_PAGES = 3
_HF_SPAN_RATIO = 0.7
# 保护形态: 列表项标记开头 / 条目ID / 句末标点结尾的完整句子(正文特征)
_LIST_ITEM_PREFIX = re.compile(
    r'^\s*(?:[-•●·○■□►◆]\s*|\d+[)）.]\s*|[a-zA-Z][)）]\s*)')


def _strip_cross_page_repeats(pages_dict: dict[int, list[str]]) -> None:
    """
    文本级跨页重复行过滤 — 页眉页脚识别的安全网(就地修改)。

    独立于新解析器的块分类与坐标信息: 内网文档常见"独立成行的文档名/
    公司名"页眉, 若行级检测因坐标漂移(>20pt)或长度(>60字符)未命中,
    该行会在每页正文中原样重复。此类"在大量页面上原样出现的短行"几乎
    必为页眉页脚装饰, 按以下保护规则过滤:

    - 长度 <=60 字符, 非纯符号, 非列表项开头, 不含条目ID;
    - 不以句末标点结尾(页眉页脚极少是完整句子);
    - 出现页数 >= max(3, 页数×0.35) 且 页码跨度 >= 文档长度70%
      (真页眉从文档头贯穿到尾; 正文重复短语通常只局部聚集)。
    """
    n = len(pages_dict)
    if n < _HF_REPEAT_MIN_PAGES:
        return

    where: dict[str, list[int]] = {}
    for pno, lines in pages_dict.items():
        seen = set()
        for ln in lines:
            t = ln.strip()
            if not t or len(t) > 60 or t in seen:
                continue
            if _ListMark.match(t):
                continue
            if _LIST_ITEM_PREFIX.match(t):
                continue
            if re.search(ITEM_ID_REGEX, t):
                continue
            if t[-1] in '。！？；，、':
                continue
            seen.add(t)
            where.setdefault(t, []).append(pno)

    thr = max(_HF_REPEAT_MIN_PAGES, int(n * _HF_REPEAT_RATIO))
    span_need = max(1, int(n * _HF_SPAN_RATIO))
    hf_texts = {t for t, pnos in where.items()
                if len(pnos) >= thr and (max(pnos) - min(pnos)) >= span_need}
    if not hf_texts:
        return

    for pno in pages_dict:
        pages_dict[pno] = [ln for ln in pages_dict[pno]
                           if ln.strip() not in hf_texts]


def _merge_standalone_list_marks(text: str) -> str:
    """
    将独立成行的列表标记("-"/"•"/"·"等)合并到下一行开头。

    新模块 layout 将列表项符号提取为独立 TextLine(如 "-\n单一故障准则"),
    而 legacy 的 get_text 输出是 "-单一故障准则" 同行。此函数把独立标记
    拼到下一行, 模拟 legacy 格式, 避免 _clean_lonely_bullets 误删。
    若标记行后没有内容(孤立bullet), 保留不动交给 _clean_lonely_bullets 清理。
    """
    lines = text.split('\n')
    merged = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        m = _ListMark.match(cur)
        if m and i + 1 < len(lines) and lines[i + 1].strip():
            marker = m.group(1)
            merged.append(marker + lines[i + 1].lstrip())
            i += 2
        else:
            merged.append(cur)
            i += 1
    return '\n'.join(merged)


def _clean_lonely_bullets(text: str) -> str:
    """清理孤立的bullet碎片"""
    return _LONELY_BULLET_RE.sub('\n', text)


# ============================================================
# DocumentResult → list[PageText] 转换
# ============================================================

def _render_list_items(items: list[dict], output: list[str], depth: int = 0) -> None:
    """将列表项渲染为文本行(保持缩进和标记)"""
    for item in items:
        marker = item.get("marker", "")
        text = item.get("text", "")
        indent = "  " * depth
        output.append(f"{indent}{marker}{text}")
        if item.get("children"):
            _render_list_items(item["children"], output, depth + 1)


def _walk_all_blocks(blocks) -> list:
    """深度优先遍历Block树, 返回平铺列表"""
    result = []
    for block in blocks:
        result.append(block)
        if block.children:
            result.extend(_walk_all_blocks(block.children))
    return result


def _document_result_to_page_texts(result: DocumentResult) -> list[PageText]:
    """
    将 DocumentResult 转换为 list[PageText]

    转换规则(与原始模块 extract_body_text 行为一致):
    - heading 块: 标题文本独占一行(供 extract_sections 正则匹配)
    - text/paragraph 块: 块内行合并后输出
    - list 块: 每个列表项独占一行, 保持标记格式
    - caption 块: 图题/表题文本独占一行
    - table/image/figure 块: 跳过(文字不入正文, 与原始模块"区域排除"行为一致)
    - header_footer/consumed 块: 跳过
    - 应用文本清洗: PUA清除、CJK空格清除、孤立bullet清理
    """
    pages_dict: dict[int, list[str]] = {}  # page(0-based) -> 行列表

    for block in _walk_all_blocks(result.body):
        # 跳过非正文块
        if block.type in ("consumed", "header_footer"):
            continue

        page = block.page
        if page not in pages_dict:
            pages_dict[page] = []

        if block.type == "heading":
            t = block.text.strip()
            if t:
                pages_dict[page].append(t)
        elif block.type in ("text", "paragraph"):
            text = _join_lines_compat(block.text)
            if text:
                pages_dict[page].append(text)
        elif block.type == "list":
            for region in (block.list_items or []):
                if region.get("type") == "intro":
                    t = region.get("text", "").strip()
                    if t:
                        pages_dict[page].append(t)
                else:
                    _render_list_items(region.get("items", []), pages_dict[page])
        elif block.type == "caption":
            t = block.text.strip()
            if t:
                pages_dict[page].append(t)
        # table, image, figure → 跳过

    # 安全网: 文本级跨页重复行过滤(独立成行的文档名/公司名页眉等,
    # 逃过新解析器行级检测的残留装饰)
    _strip_cross_page_repeats(pages_dict)

    pages = []
    for pno in sorted(pages_dict.keys()):
        raw_text = "\n".join(pages_dict[pno])
        # 合并独立成行的列表标记("-"/"•"/"·"等)到下一行开头:
        # 新模块将列表项"-"提取为独立 TextLine(与内容分离), 而 legacy 的
        # get_text 输出是 "-内容" 同行。若直接对独立"-"行应用 _clean_lonely_bullets
        # 会误删列表项前缀。先合并模拟 legacy 格式, 再走清洗管道。
        raw_text = _merge_standalone_list_marks(raw_text)
        # 应用文本清洗(与原始模块 extract_body_text 一致的管道, 但跳过位置过滤)
        # 注意: 不调用 _filter_edge_doc_name_lines, 因为新模块已通过块分类(header_footer)
        #       处理了页眉页脚, 位置过滤会误删短正文行(如"本文档所使用的缩略语及其中文描述见表2。"仅20字符)
        # 1. 正则过滤: 清除新模块未识别为 header_footer 块的残留页眉页脚(如页码每页不同)
        text = _HEADER_FOOTER_RE.sub('', raw_text)
        text = _PAGE_NUMBER_RE.sub('', text)
        text = _DOC_NAME_PAGE_RE.sub('', text)
        text = _CLASSIFICATION_RE.sub('', text)
        text = _VERSION_RE.sub('', text)
        text = _PAGE_LABEL_RE.sub('', text)
        text = _DOC_NUMBER_RE.sub('', text)
        # 2. 去除私有区Unicode字符
        text = _clean_pua(text)
        # 3. 消除CJK-Latin伪空格
        text = _clean_cjk_spaces(text)
        # 4. 清理孤立bullet碎片
        text = _clean_lonely_bullets(text)
        pages.append(PageText(page_number=pno + 1, text=text))  # 转为1-based页码
    return pages


# ============================================================
# 新模块解析(带缓存)
# ============================================================

def _parse_with_new(pdf_path: str) -> Optional[DocumentResult]:
    """使用新模块解析PDF, 返回DocumentResult或None(失败时)"""
    fp = _pdf_fingerprint(pdf_path)
    cached = _cache_get(_result_cache, fp)
    if cached is not None:
        return cached
    # 已知失败(扫描件/此前解析异常)直接降级, 不重付失败解析的代价
    if _cache_get(_fail_cache, fp) is not None:
        return None
    try:
        # 图片资产导出到统一输出目录, 避免污染源PDF所在目录
        # (新模块默认导出到 PDF 同目录 assets/, 会污染 系统需求/ 等源目录)
        try:
            from config import OUTPUT_DIR
            asset_dir = os.path.join(
                OUTPUT_DIR, "pdf_assets",
                os.path.splitext(os.path.basename(pdf_path))[0],
            )
        except Exception:
            asset_dir = None
        parser = DocumentParser(pdf_path, prefer_camelot=False, asset_dir=asset_dir)
        result = parser.parse()
        if not result.meta.get("has_text_layer", True):
            _cache_put(_fail_cache, fp, '扫描件(无文本层)', _CACHE_MAX_DOCS)
            print(f"    [pdf] 新模块不适用, 降级legacy: {os.path.basename(pdf_path)} (扫描件/无文本层)")
            return None  # 扫描件, 新模块无法处理
        _cache_put(_result_cache, fp, result, _CACHE_MAX_DOCS)
        dump_document_result(pdf_path, result)  # 中间产物: 解析阶段
        return result
    except Exception as e:
        # 记录失败并暴露原因: 静默吞异常会把新引擎的真实bug掩盖成"降级"
        reason = f'{type(e).__name__}: {e}'
        _cache_put(_fail_cache, fp, reason, _CACHE_MAX_DOCS)
        print(f"    [pdf] 新模块解析失败, 降级legacy: {os.path.basename(pdf_path)} — {reason[:160]}")
        return None


def _extract_tables_via_new_parser(pdf_path: str) -> Optional[list[TableData]]:
    """
    使用新模块提取所有表格, 转换为原始 TableData 格式

    新模块的 TableData.rows 是 list[list[TableCell]], 每个 TableCell 有 .text/.rowspan/.colspan
    原始模块的 TableData.rows 是 list[list[str]], headers 是 list[str]
    """
    result = _parse_with_new(pdf_path)
    if result is None:
        return None

    tables = []
    for block in _walk_all_blocks(result.body):
        if block.type != "table" or block.table is None:
            continue
        td = block.table
        if not td.rows:
            continue
        page_num = td.page_start + 1  # 转为1-based
        # 第一行作为表头
        header_row = td.rows[0]
        headers = [_clean_cell(c.text) for c in header_row]
        rows = [[_clean_cell(c.text) for c in row] for row in td.rows[1:]]
        tables.append(TableData(
            page_number=page_num,
            headers=headers,
            rows=rows,
        ))
    dump_tables(pdf_path, tables)  # 中间产物: 解析阶段
    return tables


# ============================================================
# 追踪矩阵表头匹配逻辑(从原始模块提取, 保持一致)
# ============================================================

_TRACE_KEYWORDS = ('追踪', '上游', '需求编号', '标志号', '章节号')


def _is_trace_header(headers: list[str]) -> bool:
    """判断表头是否为需求侧追踪关系表的表头"""
    text = ''.join(re.sub(r'\s+', '', h or '') for h in headers)
    has_id = ('条目号' in text) or ('编号' in text) or ('标志号' in text)
    has_trace = any(kw in text for kw in _TRACE_KEYWORDS)
    return has_id and has_trace


def _norm_headers(headers: list[str]) -> str:
    """归一化表头(需求侧: 用|拼接)"""
    return '|'.join(re.sub(r'\s+', '', h or '') for h in headers)


def _norm_header(headers: list[str]) -> str:
    """归一化表头(设计侧: 直接拼接)"""
    return ''.join(re.sub(r'\s+', '', h or '') for h in headers)


def _is_design_trace_header(headers: list[str]) -> bool:
    """判断表头是否为系统设计追踪关系表的表头"""
    header_norm = _norm_header(headers)
    if ('设计条目号' in header_norm
            or ('条目号' in header_norm and '需求' in header_norm)
            or '追踪关系' in header_norm
            or '追溯' in header_norm):
        return True
    has_self = ('本文' in header_norm) or ('本文档' in header_norm)
    has_design_flag = ('设计' in header_norm
                       and ('编号' in header_norm or '标志号' in header_norm or '条目号' in header_norm))
    has_req = ('需求' in header_norm
               and ('编号' in header_norm or '标志号' in header_norm
                    or '条目号' in header_norm or '章节号' in header_norm))
    return has_req and (has_self or has_design_flag)


def _find_trace_table(tables: list[TableData], is_design: bool) -> Optional[TableData]:
    """
    从表格列表中查找追踪矩阵表并合并跨页表

    逻辑与原始模块的 find_traceability_table / find_traceability_table_design 一致:
    1. 按表头关键词筛选追踪表
    2. 单表直接返回
    3. 多表合并跨页(表头归一化相同→合并行; 续页非真表头→作为数据行; 表头不同→取行数最多)
    """
    if is_design:
        trace_tables = [t for t in tables if _is_design_trace_header(t.headers)]
        norm_fn = _norm_header
        header_check = _is_design_trace_header
    else:
        trace_tables = [t for t in tables if _is_trace_header(t.headers)]
        norm_fn = _norm_headers
        header_check = _is_trace_header

    if not trace_tables:
        return None
    if len(trace_tables) == 1:
        return trace_tables[0]

    # 跨页合并
    merged_headers = trace_tables[0].headers
    merged_rows = list(trace_tables[0].rows)
    first_page = trace_tables[0].page_number
    merged_norm = norm_fn(merged_headers)

    for table in trace_tables[1:]:
        table_norm = norm_fn(table.headers)

        # 策略1: 归一化表头相同 → 跨页延续, 直接合并行
        if table_norm == merged_norm:
            merged_rows.extend(table.rows)
            continue

        # 策略2: 续页"表头"不含追踪关键词 → 不是真表头, 是数据行
        if not header_check(table.headers):
            merged_rows.append([_clean_cell(c) for c in table.headers])
            merged_rows.extend(table.rows)
            continue

        # 策略3: 表头不同但都含追踪关键词 → 取行数最多的
        if len(table.rows) > len(merged_rows):
            merged_headers = table.headers
            merged_rows = list(table.rows)
            first_page = table.page_number
            merged_norm = table_norm

    return TableData(
        page_number=first_page,
        headers=merged_headers,
        rows=merged_rows,
    )


# ============================================================
# 公共 API (与 core/pdf_parser 接口完全一致)
# ============================================================

def extract_body_text(pdf_path: str) -> list[PageText]:
    """
    提取PDF正文文本(每页一个PageText)

    优先使用新模块的结构化提取, 失败时降级到原始模块.
    DocumentResult 和 PageText 转换结果均缓存, 二次调用零开销.
    """
    if not _use_new_parser():
        pages = _legacy_extract_body_text(pdf_path)
        dump_page_texts(pdf_path, pages)  # 中间产物: 解析阶段
        return pages

    fp = _pdf_fingerprint(pdf_path)

    # 先查 PageText 缓存(转换结果)
    cached_pt = _cache_get(_pagetext_cache, fp)
    if cached_pt is not None:
        return list(cached_pt)

    result = _parse_with_new(pdf_path)
    if result is None:
        pages = _legacy_extract_body_text(pdf_path)
        dump_page_texts(pdf_path, pages)  # 中间产物: 解析阶段(降级路径)
        return pages

    pages = _document_result_to_page_texts(result)
    _cache_put(_pagetext_cache, fp, pages, _CACHE_MAX_DOCS)
    dump_page_texts(pdf_path, pages)  # 中间产物: 解析阶段
    return list(pages)


def extract_full_text(pdf_path: str) -> str:
    """提取PDF全文(所有页拼接)"""
    if not _use_new_parser():
        return _legacy_extract_full_text(pdf_path)

    pages = extract_body_text(pdf_path)
    return '\n'.join(p.text for p in pages)


def extract_tables(pdf_path: str, last_n_pages: int = 5) -> list[TableData]:
    """
    提取PDF中的表格

    新模块提取全文档表格, 按last_n_pages过滤(与原始模块行为一致).
    """
    if not _use_new_parser():
        return _legacy_extract_tables(pdf_path, last_n_pages)

    tables = _extract_tables_via_new_parser(pdf_path)
    if tables is None:
        return _legacy_extract_tables(pdf_path, last_n_pages)

    # 按last_n_pages过滤(保留最后N页的表格)
    if last_n_pages and tables:
        max_page = max(t.page_number for t in tables)
        min_page = max(1, max_page - last_n_pages + 1)
        tables = [t for t in tables if t.page_number >= min_page]

    return tables


def find_traceability_table(pdf_path: str) -> TableData | None:
    """
    在PDF中查找追踪矩阵附录表(需求侧)

    新模块提取表格 → 表头匹配 → 找不到时降级到原始模块
    """
    if not _use_new_parser():
        return _legacy_find_traceability_table(pdf_path)

    tables = _extract_tables_via_new_parser(pdf_path)
    if tables is None:
        result = _legacy_find_traceability_table(pdf_path)
        if result is not None:
            dump_trace_table(pdf_path, result, "需求侧追踪表(legacy)")
        return result

    result = _find_trace_table(tables, is_design=False)
    if result is None:
        # 新模块未找到追踪表, 降级到原始模块
        result = _legacy_find_traceability_table(pdf_path)
        if result is not None:
            dump_trace_table(pdf_path, result, "需求侧追踪表(legacy)")
        return result
    dump_trace_table(pdf_path, result, "需求侧追踪表")  # 中间产物: 提取阶段
    return result


def find_traceability_table_design(
    pdf_path: str, last_n_pages: int = 30
) -> TableData | None:
    """
    在系统设计PDF中查找追踪矩阵附录表(设计侧)

    新模块提取表格 → 表头匹配 → 找不到时降级到原始模块
    """
    if not _use_new_parser():
        return _legacy_find_traceability_table_design(pdf_path, last_n_pages)

    tables = _extract_tables_via_new_parser(pdf_path)
    if tables is None:
        result = _legacy_find_traceability_table_design(pdf_path, last_n_pages)
        if result is not None:
            dump_trace_table(pdf_path, result, "设计侧追踪表(legacy)")
        return result

    result = _find_trace_table(tables, is_design=True)
    if result is None:
        # 新模块未找到追踪表, 降级到原始模块
        result = _legacy_find_traceability_table_design(pdf_path, last_n_pages)
        if result is not None:
            dump_trace_table(pdf_path, result, "设计侧追踪表(legacy)")
        return result
    dump_trace_table(pdf_path, result, "设计侧追踪表")  # 中间产物: 提取阶段
    return result


def _prefetch_one(pdf_path: str) -> None:
    """预解析单份PDF: 同时预热 DocumentResult 和 PageText 缓存"""
    try:
        # extract_body_text 内部会先查 _pagetext_cache, 再查 _result_cache,
        # 都 miss 时调用新模块解析并缓存两层结果
        extract_body_text(pdf_path)
    except Exception:
        pass


def prefetch_pdfs(pdfs, max_workers: int = 0) -> None:
    """
    并行预解析多份PDF, 把解析结果灌入缓存(仅当前生效的后端)。

    legacy 缓存不再无条件预热: 降级是小概率事件, 双引擎各解析一遍
    会让每次全流程的解析时间近乎翻倍; 真发生降级时 legacy 按需解析
    一次并写入自己的缓存, 结果不受影响。
    """
    if not _use_new_parser():
        return _legacy_prefetch_pdfs(pdfs, max_workers)

    if isinstance(pdfs, dict):
        tasks = [(p, name) for name, p in pdfs.items()]
    else:
        tasks = [(p, '') for p in pdfs]

    # 过滤有效路径(注意: tasks 中元组顺序为 (path, name))
    jobs = [(p, name) for p, name in tasks if p and os.path.isfile(p)]

    if len(jobs) <= 1:
        for p, _ in jobs:
            _prefetch_one(p)
    else:
        if max_workers <= 0:
            max_workers = min(len(jobs), (os.cpu_count() or 4), 8)
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                list(pool.map(lambda j: _prefetch_one(j[0]), jobs))
        except Exception:
            for p, _ in jobs:
                _prefetch_one(p)


def clear_pdf_cache() -> None:
    """清空所有PDF解析缓存(新模块+原始模块)"""
    with _cache_lock:
        _result_cache.clear()
        _pagetext_cache.clear()
        _fail_cache.clear()
    _legacy_clear_pdf_cache()
