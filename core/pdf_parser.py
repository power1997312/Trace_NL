"""
PDF双引擎解析模块
- PyMuPDF(fitz): 正文文本提取(段落结构好)
- pdfplumber: 表格提取(追踪矩阵附录)
"""
import re
import fitz  # PyMuPDF
import pdfplumber
from dataclasses import dataclass, field
from config import ITEM_ID_REGEX


@dataclass
class PageText:
    """单页文本"""
    page_number: int
    text: str


@dataclass
class TableData:
    """表格数据"""
    page_number: int
    headers: list[str]
    rows: list[list[str]]


@dataclass
class TraceRelation:
    """单条追踪关系"""
    downstream_id: str
    upstream_ref: str      # 上游条目号或章节号
    upstream_doc: str      # 上游文档名称


# 页眉页脚正则: 匹配 "DCS 需求说明书 版本：B 页码：8/90" 等格式
_HEADER_FOOTER_RE = re.compile(
    r'^\s*\S.*\s+版本[：:]\s*\S+\s+页码[：:]\s*\S+\s*$', re.MULTILINE
)
# 密级标记
_CLASSIFICATION_RE = re.compile(r'^\s*密级[：:]\s*\S+\s*$', re.MULTILINE)

# ---- 文本清洗正则 ----
# PDF私有区Unicode字符(Wingdings等字体映射的bullet符号)
_PUA_RE = re.compile(r'[\uf000-\uf8ff]')
# CJK与Latin/Number之间的PDF伪空格 (PyMuPDF根据字形间距自动插入)
# 注意: 使用[ \t]+而非\s+，避免匹配换行符导致跨行合并
_CJK_LATIN_SPACE_RE = re.compile(
    r'(?<=[\u4e00-\u9fff\u3000-\u303f])[ \t]+(?=[A-Za-z0-9])'
    r'|'
    r'(?<=[A-Za-z0-9])[ \t]+(?=[\u4e00-\u9fff\u3000-\u303f])'
)
# Latin字母与数字之间的PDF伪空格 (如 "GB/T 12727" → "GB/T12727")
_LATIN_DIGIT_SPACE_RE = re.compile(
    r'(?<=[A-Za-z/])[ \t]+(?=\d)'
)
# 列表编号后的PDF伪空格 (如 "1) 自动停堆" → "1)自动停堆")
_LIST_MARKER_SPACE_RE = re.compile(
    r'^(\s*\d+[)）.])[ \t]+', re.MULTILINE
)
# 章节标题行: 匹配 "3.2 系统功能" "3供货要求" "3 供货要求" "3.2.2.1F-SC1级要求" "2.协调和集成" 等
# 规则: 必须含小数点(如3.2) 或 单位数字+标题文本(如3供货要求、3 供货要求)
_SECTION_HEADING_RE = re.compile(
    r'^(?:'
    r'\d+(?:\.\d+)+[\s]*\S.{0,30}'                       # 带点章节号: 3.2, 3.2.2.1 + 标题
    r'|'
    r'\d+\.\S.{0,30}'                                     # 数字.标题: 2.协调和集成
    r'|'
    r'\d(?=[^\d\s.])\S{0,30}'                             # 单位数字直接连非数字: 3供货要求
    r'|'
    r'\d\s(?=[\u4e00-\u9fff])[\u4e00-\u9fff].{0,30}'     # 单位数字+空格+CJK: 3 供货要求
    r')$', re.MULTILINE
)
# 孤立bullet碎片: 单独一行只有一个 "-" 或 "·" 等
# 使用负前瞻确保后面没有非空白内容(避免误删正常列表项)
_LONELY_BULLET_RE = re.compile(r'\n[ \t]*[-•●·][ \t]*(?=\n)')


def extract_body_text(pdf_path: str) -> list[PageText]:
    """
    用PyMuPDF提取PDF正文文本，保持段落结构，并执行文本清洗

    清洗步骤:
    1. 清除页眉页脚和密级标记
    2. 去除PDF私有区Unicode字符(Wingdings bullet等)
    3. 合并句内换行(非段落边界的硬换行)
    4. 消除CJK与Latin/Number之间的PDF伪空格
    5. 清理孤立bullet碎片

    Args:
        pdf_path: PDF文件路径

    Returns:
        list[PageText]: 每页的文本内容
    """
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        page = doc[i]
        text = page.get_text()
        # 1. 清除页眉页脚和密级标记
        text = _HEADER_FOOTER_RE.sub('', text)
        text = _CLASSIFICATION_RE.sub('', text)
        # 2. 去除私有区Unicode字符
        text = _clean_pua_chars(text)
        # 3. 合并句内换行
        text = _merge_intra_sentence_breaks(text)
        # 4. 消除CJK-Latin伪空格
        text = _clean_cjk_spaces(text)
        # 5. 清理孤立bullet碎片
        text = _clean_lonely_bullets(text)
        pages.append(PageText(page_number=i + 1, text=text))
    doc.close()
    return pages


def extract_full_text(pdf_path: str) -> str:
    """提取PDF全文(所有页拼接)"""
    pages = extract_body_text(pdf_path)
    return '\n'.join(p.text for p in pages)


# ============================================================
# 文本清洗函数
# ============================================================

def _clean_pua_chars(text: str) -> str:
    """
    去除PDF私有区Unicode字符(U+F000-U+F8FF)
    这些字符通常是Wingdings等字体的bullet符号
    """
    return _PUA_RE.sub('', text)


def _merge_intra_sentence_breaks(text: str) -> str:
    """
    合并句内换行: 如果上一行以CJK字符/标点结尾，且下一行以CJK字符开头
    (不是章节标题、不是列表项、不是空行)，则将换行合并为无间隔

    保留的换行:
    - 空行(段落分隔)
    - 下一行是章节标题(如 "3.2 系统功能", "3供货要求", "3.2.2.1F-SC1级要求")
    - 下一行是列表项(如 "1) xxx", "- xxx")
    """
    lines = text.split('\n')
    if len(lines) <= 1:
        return text

    # 预扫描: 标记所有章节标题行(在合并之前!)
    # 避免前面的行合并后导致后续章节标题失去行首位置
    heading_flags = [False] * len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and _SECTION_HEADING_RE.match(stripped):
            heading_flags[i] = True

    merged = [lines[0]]
    for i in range(1, len(lines)):
        prev = merged[-1]
        curr = lines[i]

        # 空行 → 保留(段落分隔)
        if not curr.strip() or not prev.strip():
            merged.append(curr)
            continue

        # 下一行是列表项(数字编号、字母编号、符号) → 保留换行
        # 注意: 必须在章节标题检查之前，避免 "1) xxx" 被误判为章节标题
        # 支持 "- " 和 "-"直接跟CJK字符 两种列表格式
        if re.match(r'^\s*(\d+[)）.]\s|[a-zA-Z][)）]\s|[-•●](?:\s|(?=[\u4e00-\u9fff])))', curr):
            merged.append(curr)
            continue

        # 下一行是章节标题(预扫描标记) → 保留换行
        if heading_flags[i]:
            merged.append(curr)
            continue

        # 上一行以纯章节号结尾(如 "3.2") → 保留换行
        if re.match(r'^\d+(\.\d+)*$', prev.strip()):
            merged.append(curr)
            continue

        # 判断是否可以合并
        prev_last = prev.rstrip()[-1] if prev.rstrip() else ''
        curr_first = curr.lstrip()[0] if curr.lstrip() else ''

        prev_is_cjk = _is_cjk_or_punct(prev_last)
        curr_is_cjk = _is_cjk_char(curr_first)
        prev_is_latin = prev_last.isascii() and prev_last.isalpha()
        curr_is_latin = curr_first.isascii() and curr_first.isalpha()
        curr_is_digit = curr_first.isdigit()

        # 句号(。)、冒号(：:)后保留换行 — 句子边界和列表引出
        if prev_last in '。：:':
            merged.append(curr)
        elif (prev_is_cjk and curr_is_cjk):
            # CJK/CJK标点 → CJK: 合并(句号和冒号已在上面单独处理)
            merged[-1] = prev + curr
        elif prev_is_latin and curr_is_cjk:
            # Latin字母 → CJK: 合并 (如 "RPS\n系统" → "RPS系统")
            merged[-1] = prev + curr
        elif prev_is_latin and (curr_is_latin or curr_is_digit):
            # Latin → Latin/Digit: 合并 (如 "F-SC1\nF-SC2" → "F-SC1F-SC2")
            merged[-1] = prev + curr
        else:
            merged.append(curr)

    return '\n'.join(merged)


def _clean_cjk_spaces(text: str) -> str:
    """消除CJK与Latin/Number、Latin与Digit、列表编号后的PDF伪空格"""
    text = _CJK_LATIN_SPACE_RE.sub('', text)
    text = _LATIN_DIGIT_SPACE_RE.sub('', text)
    text = _LIST_MARKER_SPACE_RE.sub(r'\1', text)
    return text


def _clean_lonely_bullets(text: str) -> str:
    """清理孤立的bullet碎片(单独一行只有 "-" 或 bullet符号)
    正则使用负前瞻(?=\\n)不消耗尾部换行，替换为\\n保持行结构"""
    return _LONELY_BULLET_RE.sub('\n', text)


def _is_cjk_or_punct(ch: str) -> bool:
    """判断字符是否为CJK字符或中文标点"""
    if not ch:
        return False
    cp = ord(ch)
    # CJK统一汉字
    if 0x4e00 <= cp <= 0x9fff:
        return True
    # CJK标点 + 全角标点 + 常见中文标点
    if 0x3000 <= cp <= 0x303f:
        return True
    if ch in '，。；：、！？）"''】》…—·':
        return True
    return False


def _is_cjk_char(ch: str) -> bool:
    """判断字符是否为CJK字符(不含标点)"""
    if not ch:
        return False
    cp = ord(ch)
    return 0x4e00 <= cp <= 0x9fff


def extract_tables(pdf_path: str, last_n_pages: int = 5) -> list[TableData]:
    """
    用pdfplumber提取PDF最后N页中的表格

    Args:
        pdf_path: PDF文件路径
        last_n_pages: 扫描最后N页

    Returns:
        list[TableData]: 提取到的表格
    """
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        start = max(0, total - last_n_pages)
        for i in range(start, total):
            page = pdf.pages[i]
            page_tables = page.extract_tables()
            for table in page_tables:
                if not table or len(table) < 2:
                    continue
                # 第一行为表头
                headers = [_clean_cell(c) for c in table[0]]
                rows = []
                for row in table[1:]:
                    rows.append([_clean_cell(c) for c in row])
                tables.append(TableData(
                    page_number=i + 1,
                    headers=headers,
                    rows=rows,
                ))
    return tables


def find_traceability_table(pdf_path: str) -> TableData | None:
    """
    在PDF中查找追踪矩阵附录表
    特征: 表头包含 "需求条目号" 或 "追踪" 相关关键词

    Args:
        pdf_path: PDF文件路径

    Returns:
        TableData or None: 找到的追踪矩阵表
    """
    # 扫描最后8页
    tables = extract_tables(pdf_path, last_n_pages=8)
    for table in tables:
        header_text = ' '.join(table.headers)
        if '条目号' in header_text or '追踪' in header_text or '上游' in header_text:
            return table
    return None


def parse_traceability_table(table: TableData) -> list[TraceRelation]:
    """
    解析追踪矩阵表格，处理多值单元格(以换行分隔)

    典型表头: 本文的需求条目号 | 上游文件章节/需求号 | 说明
    多值情况: 上游文件列和说明列可能包含换行分隔的多个值

    Args:
        table: 追踪矩阵表格数据

    Returns:
        list[TraceRelation]: 解析出的追踪关系列表
    """
    relations = []

    # 确定列索引
    headers = table.headers
    id_col = 0      # 本文的需求条目号
    ref_col = 1     # 上游文件章节/需求号
    doc_col = 2     # 说明(文档名称)

    # 尝试更智能地匹配列
    for i, h in enumerate(headers):
        h_lower = h.lower() if h else ''
        if '条目号' in h_lower and '本文' in h_lower:
            id_col = i
        elif '上游' in h_lower or '章节' in h_lower:
            ref_col = i
        elif '说明' in h_lower or '文档' in h_lower:
            doc_col = i

    current_downstream_id = None

    for row in table.rows:
        if len(row) <= max(id_col, ref_col, doc_col):
            continue

        ds_id = (row[id_col] or '').strip()
        if ds_id:
            current_downstream_id = ds_id

        if not current_downstream_id:
            continue

        # 处理多值单元格
        upstream_refs_raw = row[ref_col] or ''
        upstream_docs_raw = row[doc_col] or ''

        # 按换行分割
        upstream_refs = [r.strip() for r in upstream_refs_raw.split('\n') if r.strip()]
        upstream_docs = [d.strip() for d in upstream_docs_raw.split('\n') if d.strip()]

        # 清理文档名称中的书名号
        upstream_docs = [_clean_doc_name(d) for d in upstream_docs]

        # 位置配对
        if len(upstream_refs) == len(upstream_docs):
            for ref, doc in zip(upstream_refs, upstream_docs):
                relations.append(TraceRelation(
                    downstream_id=current_downstream_id,
                    upstream_ref=ref,
                    upstream_doc=doc,
                ))
        elif len(upstream_refs) > 0 and len(upstream_docs) == 1:
            # 只有一个文档名，所有引用都来自该文档
            for ref in upstream_refs:
                relations.append(TraceRelation(
                    downstream_id=current_downstream_id,
                    upstream_ref=ref,
                    upstream_doc=upstream_docs[0],
                ))
        elif len(upstream_refs) > 0:
            # 兜底: 逐条记录
            for ref in upstream_refs:
                doc = upstream_docs[0] if upstream_docs else ''
                relations.append(TraceRelation(
                    downstream_id=current_downstream_id,
                    upstream_ref=ref,
                    upstream_doc=doc,
                ))

    return relations


def _norm_header(headers: list) -> str:
    """将表头列表归一化：去除每个单元格内的所有空白(含换行)，再拼接。
    用于关键词匹配，避免 "需求标志\\n号" 因换行导致 "标志号" 匹配失败。"""
    return ''.join(re.sub(r'\s+', '', h or '') for h in headers)


def find_traceability_table_design(pdf_path: str, last_n_pages: int = 8) -> TableData | None:
    """
    在系统设计PDF中查找追踪矩阵附录表

    系统设计文档的追踪关系表特征:
    - 表头包含 "设计条目号" / "设计标志号" / "本文档中的设计标志号" 等（设计侧标识）
      以及 "需求条目号" / "需求编号" / "需求标志号" 等（需求侧标识）
    - 表头中常含换行(如 "需求标志\\n号")，故先做空白归一化再匹配
    - 表结构有两类:
      Type A: [设计条目号, 需求条目号] — 2列
      Type B: [其他列, 设计条目号, 需求条目号] — 3列

    Args:
        pdf_path: 系统设计PDF文件路径
        last_n_pages: 扫描最后N页

    Returns:
        TableData or None: 找到的追踪矩阵表
    """
    tables = extract_tables(pdf_path, last_n_pages=last_n_pages)
    for table in tables:
        header_norm = _norm_header(table.headers)
        # 原有规则（保持兼容）：设计条目号 / 条目号+需求 / 追踪关系 / 追溯
        if ('设计条目号' in header_norm
                or ('条目号' in header_norm and '需求' in header_norm)
                or '追踪关系' in header_norm
                or '追溯' in header_norm):
            return table
        # 扩展规则：本文/本文档侧编号(设计条目) + 需求侧编号/章节号
        # 覆盖 "本文档中的设计标志号" + "需求标志号"
        #   以及 "本文的需求编号" + "需求说明书 的需求编号/章节号"
        has_self = ('本文' in header_norm) or ('本文档' in header_norm)
        has_design_flag = ('设计' in header_norm
                           and ('编号' in header_norm or '标志号' in header_norm or '条目号' in header_norm))
        has_req = ('需求' in header_norm
                   and ('编号' in header_norm or '标志号' in header_norm
                        or '条目号' in header_norm or '章节号' in header_norm))
        if has_req and (has_self or has_design_flag):
            return table
    return None


def parse_traceability_table_design(
    table: TableData,
    upstream_doc_name: str = '系统需求',
) -> list[TraceRelation]:
    """
    解析系统设计文档的追踪矩阵表

    表结构有两类:
      Type A (2列): [系统设计条目号, 系统需求条目号]
      Type B (3列): [其他, 系统设计条目号, 系统需求条目号]
    第一行为表头。系统设计条目号可以一对多（一个设计条目对应多个需求条目）。

    多值单元格处理: 当"系统需求条目号"列包含换行分隔的多个值时，
    展开为多条追踪关系。

    Args:
        table: 追踪矩阵表格数据
        upstream_doc_name: 上游文档名称，固定为"系统需求"

    Returns:
        list[TraceRelation]: 解析出的追踪关系列表
    """
    relations = []
    # 归一化表头(去除空白/换行)，便于关键词匹配
    headers = [re.sub(r'\s+', '', h or '') for h in table.headers]
    n_cols = len(headers)

    def _is_design_col(h: str) -> bool:
        self_ref = ('本文' in h) or ('本文档' in h)
        has_num = ('编号' in h) or ('标志号' in h) or ('条目号' in h)
        return (self_ref and has_num) or (('设计' in h) and has_num)

    def _is_req_col(h: str) -> bool:
        has_num = ('编号' in h) or ('标志号' in h) or ('条目号' in h) or ('章节号' in h)
        return (('需求' in h) or ('追溯' in h) or ('追踪' in h)) and has_num

    # 检测列角色（设计侧列优先，避免 "本文的需求编号" 同时被当作需求列）
    design_candidates = [i for i, h in enumerate(headers) if _is_design_col(h)]
    req_candidates = [i for i, h in enumerate(headers)
                      if _is_req_col(h) and i not in design_candidates]

    if design_candidates and req_candidates:
        design_col = design_candidates[-1]
        req_col = req_candidates[-1]
    elif n_cols >= 2:
        # 兜底：取最后两列
        design_col = n_cols - 2
        req_col = n_cols - 1
    else:
        return relations  # 列数不足，无法解析

    current_design_id = None

    def _extract_id(raw: str) -> str:
        """从单元格中提取尖括号ID(如 <FZSDCS34-ICADS001>)，去除内部空白。
        若无尖括号ID，则去除所有空白后整体返回。"""
        raw = (raw or '').strip()
        found = re.findall(ITEM_ID_REGEX, raw)
        if found:
            tok = found[0].strip()
            if tok.startswith('<') and tok.endswith('>'):
                inner = tok[1:-1].strip()
                return f"<{inner}>"
            return tok
        # 无尖括号ID：去除所有空白(含换行)后返回
        return re.sub(r'\s+', '', raw)

    for row in table.rows:
        if len(row) <= max(design_col, req_col):
            continue

        design_raw = (row[design_col] or '').strip()
        if design_raw:
            current_design_id = _extract_id(design_raw)

        if not current_design_id:
            continue

        # 读取需求引用（可能多值）
        # 优先按尖括号ID提取(支持同一单元格内多个 <...> 引用)
        req_refs_raw = row[req_col] or ''
        found_refs = re.findall(ITEM_ID_REGEX, req_refs_raw)
        if found_refs:
            req_refs = []
            for r in found_refs:
                tok = r.strip()
                if tok.startswith('<') and tok.endswith('>'):
                    inner = tok[1:-1].strip()
                    req_refs.append(f"<{inner}>")
                else:
                    req_refs.append(tok)
        else:
            # 兜底：按换行分割(处理 "3、系统架构设计要求" 等章节型引用)
            req_refs = [re.sub(r'\s+', '', r.strip())
                        for r in req_refs_raw.split('\n') if r.strip()]

        if not req_refs:
            continue

        # 每条需求引用生成一条追踪关系
        for ref in req_refs:
            relations.append(TraceRelation(
                downstream_id=current_design_id,
                upstream_ref=ref,
                upstream_doc=upstream_doc_name,
            ))

    return relations


def _clean_cell(cell) -> str:
    """清理表格单元格内容，包括CJK-Latin、Latin-Digit和列表编号伪空格"""
    if cell is None:
        return ''
    text = str(cell).strip()
    text = _CJK_LATIN_SPACE_RE.sub('', text)
    text = _LATIN_DIGIT_SPACE_RE.sub('', text)
    text = _LIST_MARKER_SPACE_RE.sub(r'\1', text)
    return text


def _clean_doc_name(name: str) -> str:
    """清理文档名称，去除书名号等"""
    name = name.strip()
    name = name.strip('《》「」')
    return name.strip()
