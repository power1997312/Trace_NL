from __future__ import annotations
"""
PDF双引擎解析模块
- PyMuPDF(fitz): 正文文本提取(段落结构好)
- pdfplumber: 表格提取(追踪矩阵附录)
"""
import os
import re
import threading
from collections import OrderedDict

import fitz  # PyMuPDF
import pdfplumber
from dataclasses import dataclass, field
from config import ITEM_ID_REGEX


# ============================================================
# 解析结果缓存
#
# 同一份 PDF 在一次运行中会被反复解析:
#   - extract_body_text / extract_full_text: 逆向矩阵、正向矩阵、条目提取各调一次
#   - 它们内部都要跑 _detect_page_regions(内含昂贵的 find_tables)
# 实测 30 页的设计文档, _detect_page_regions 被调用 60 次, 其中一半是纯重复。
#
# 缓存以 (绝对路径, mtime_ns, 文件大小) 为键, 文件一旦改动自动失效;
# 采用 LRU 上限防止批量处理大量文档时内存无限增长。
# ============================================================

_CACHE_MAX_DOCS = 24        # 文档级缓存最多保留的文档数
_CACHE_MAX_PAGES = 4000     # 页级区域缓存最多保留的页数

_cache_lock = threading.Lock()
_body_text_cache: OrderedDict = OrderedDict()
_page_regions_cache: OrderedDict = OrderedDict()
_tables_cache: OrderedDict = OrderedDict()


def _pdf_fingerprint(pdf_path: str) -> tuple:
    """以路径+修改时间+大小作为缓存键, 文件变更后缓存自动失效"""
    try:
        st = os.stat(pdf_path)
        return (os.path.abspath(pdf_path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (os.path.abspath(pdf_path), 0, 0)


def _cache_get(cache: OrderedDict, key):
    with _cache_lock:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _cache_put(cache: OrderedDict, key, value, limit: int):
    with _cache_lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def prefetch_pdfs(pdfs, max_workers: int = 0) -> None:
    """
    并行预解析多份PDF, 把正文/分页区域检测结果灌入缓存。

    处理多份文档时, 原本是"解析A→匹配A→解析B→匹配B"的串行流程。
    PyMuPDF 在页面文本抽取期间会释放 GIL, 因此用线程池并行预解析
    能真实压缩墙钟时间; 随后各文档的正式解析直接命中缓存。

    解析结果只与文档内容有关, 与解析顺序无关, 因此并行不会改变任何输出。
    单份文档或线程池不可用时自动退回串行。

    Args:
        pdfs: {文档名: PDF路径} 字典, 或 PDF 路径列表。
              传字典时图缓存会按 "文档名_" 前缀预热, 与正式调用保持同一缓存键。
        max_workers: 并行线程数, 0 表示按文档数与CPU自动决定
    """
    if isinstance(pdfs, dict):
        tasks = [(p, name + '_') for name, p in pdfs.items()]
    else:
        tasks = [(p, '') for p in pdfs]

    # 去重并过滤掉不存在的路径
    seen = set()
    jobs = []
    for path, prefix in tasks:
        if not path or not os.path.isfile(path) or (path, prefix) in seen:
            continue
        seen.add((path, prefix))
        jobs.append((path, prefix))

    if len(jobs) <= 1:
        for path, prefix in jobs:
            _prefetch_one(path, prefix)
        return

    if max_workers <= 0:
        max_workers = min(len(jobs), (os.cpu_count() or 4), 8)

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(lambda j: _prefetch_one(j[0], j[1]), jobs))
    except Exception:
        for path, prefix in jobs:
            _prefetch_one(path, prefix)


def _prefetch_one(pdf_path: str, id_prefix: str) -> None:
    """预解析单份PDF; 单份失败不影响其它文档(后续正式解析会再报错)"""
    try:
        extract_body_text(pdf_path)
    except Exception:
        pass


def clear_pdf_cache() -> None:
    """清空PDF解析缓存(长驻服务中如需强制重新解析可调用)"""
    with _cache_lock:
        _body_text_cache.clear()
        _page_regions_cache.clear()
        _tables_cache.clear()


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


# 页眉页脚正则: 匹配多种常见格式
# 格式1: "DCS 需求说明书 版本：B 页码：8/90" (单行完整页眉)
_HEADER_FOOTER_RE = re.compile(
    r'^\s*\S.*\s+版本[：:]\s*\S+\s+页码[：:]\s*\S+\s*$', re.MULTILINE
)
# 格式2: 纯页码行 "第8页" "8/90" "- 8 -" "Page 8" "第8页 共90页"
_PAGE_NUMBER_RE = re.compile(
    r'^\s*(?:第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?|\d+\s*/\s*\d+|-\s*\d+\s*-|Page\s+\d+(?:\s+of\s+\d+)?)\s*$',
    re.MULTILINE | re.IGNORECASE
)
# 格式3: 文档名+页码 "DCS需求说明书 8" "RPS系统需求规范书 第8页"
_DOC_NAME_PAGE_RE = re.compile(
    r'^\s*[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9\s\-_]{2,30}\s+(?:第\s*)?\d+(?:\s*/\s*\d+)?(?:\s*页)?\s*$',
    re.MULTILINE
)
# 格式4: 密级标记 "密级：秘密" "密级: 内部"
_CLASSIFICATION_RE = re.compile(r'^\s*密级[：:]\s*\S+\s*$', re.MULTILINE)
# 格式5: 纯版本行 "版本：B" "Version: 1.0"
_VERSION_RE = re.compile(
    r'^\s*(?:版本|Version)[：:]\s*\S+\s*$', re.MULTILINE | re.IGNORECASE
)
# 格式6: "页码：X/Y" 或 "页码:X" (带"页码"前缀的页码行)
_PAGE_LABEL_RE = re.compile(
    r'^\s*页码[：:]\s*\S+\s*$', re.MULTILINE
)
# 格式7: 文档编号行 (纯字母数字+连字符, 如 "FFGKJDGJS-345645-SD003")
_DOC_NUMBER_RE = re.compile(
    r'^\s*[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){1,5}\s*$', re.MULTILINE
)
# 格式7: 独立文档名行 (短文本, 仅含CJK/Latin/数字/空格/连字符, 长度5-40)
# 用于匹配多行页眉中的文档名行, 如 " DCS 需求说明书 "
_DOC_NAME_LINE_RE = re.compile(
    r'^\s*[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9\s\-_（）()]{3,38}\s*$'
)

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


def _filter_edge_doc_name_lines(text: str) -> str:
    """
    位置过滤: 检测并移除页首/页尾的多行页眉页脚块

    多行页眉典型结构(如仪控报警.pdf):
      行1: "FFGKJDGJS-345645-SD003"  (文档编号)
      行2: "版本：A"                  (版本)
      行3: "页码：4/4"               (页码)
      行4: "测试核电集团公司"          (公司名)
      行5: "开会核电6号机组..."        (文档标题)
      行6: "123456789"               (编号/密级)

    策略: 在前8行中查找版本/页码/密级关键词, 如果找到,
    则从页首到该关键词行(含)之间的所有短文本行(≤50字符)视为页眉块, 整体移除。
    这样避免逐行过滤时因正则先清空版本/页码行导致上下文丢失。
    """
    lines = text.split('\n')
    if len(lines) <= 2:
        return text

    _HEADER_KW = ('版本', '页码', '密级', 'Version', 'Page')
    _MAX_HEADER_LINES = 8   # 最多检查前8行
    _MAX_LINE_LEN = 30      # 页眉行最大长度(超过则认为是正文, 典型页眉≤22字符)

    # === 页首检测 ===
    # 从前向后扫描, 找到页眉块的最后一行
    # 页眉块 = 从页首开始, 到第一个"长正文行"之前的所有短文本行
    # 触发条件: 前_MAX_HEADER_LINES行内存在版本/页码/密级关键词
    header_end = -1
    found_keyword = False
    for i in range(min(_MAX_HEADER_LINES, len(lines))):
        stripped = lines[i].strip()
        if not stripped:
            # 空行: 如果在页眉块内则跳过, 否则可能是段落分隔
            if header_end >= 0:
                continue
            else:
                continue
        # 检查是否包含页眉关键词
        if any(kw in stripped for kw in _HEADER_KW):
            found_keyword = True
        # 短文本行(≤_MAX_LINE_LEN)视为页眉候选
        if len(stripped) <= _MAX_LINE_LEN:
            header_end = i
        else:
            # 长文本行 → 正文开始, 停止扫描
            break

    # 仅当找到关键词时才移除页眉块(避免误删纯短文本正文)
    if found_keyword and header_end >= 0:
        lines = lines[header_end + 1:]

    # === 页尾检测 ===
    # 检查最后5行, 移除密级/页码/文档编号等页脚行
    footer_start = len(lines)
    for i in range(max(0, len(lines) - 5), len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            continue
        # 关键词匹配
        if any(kw in stripped for kw in _HEADER_KW) and len(stripped) <= _MAX_LINE_LEN:
            footer_start = i
            break
        # 页码模式匹配 (第X页, X/Y, - X -, Page X)
        if _PAGE_NUMBER_RE.match(stripped):
            footer_start = i
            break
        # 文档编号模式匹配 (FFGKJDGJS-345645-SD003)
        if _DOC_NUMBER_RE.match(stripped):
            footer_start = i
            break
        # 页码标签匹配 (页码：X/Y)
        if _PAGE_LABEL_RE.match(stripped):
            footer_start = i
            break

    if footer_start < len(lines):
        lines = lines[:footer_start]

    return '\n'.join(lines)


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
    fp = _pdf_fingerprint(pdf_path)
    cached = _cache_get(_body_text_cache, fp)
    if cached is not None:
        return list(cached)

    doc = fitz.open(pdf_path)
    pages = []
    for i in range(doc.page_count):
        page = doc[i]
        # 忽略非正文区域(位图/矢量图/表格)内的文字, 避免图内文字/表内文字
        # 混入正文造成混乱。区域检测见 _detect_page_regions。
        # 无区域的页走原 get_text() 逻辑, 保证零影响。
        region_rects = [b for _, b in _page_regions_cached(page, fp, i)]
        text = _extract_text_excluding_regions(page, region_rects)
        # 1. 位置过滤: 检测并移除页首/页尾的多行页眉页脚块
        #    必须在正则过滤之前执行, 否则版本/页码行被清空后无法检测页眉块边界
        text = _filter_edge_doc_name_lines(text)
        # 2. 正则过滤: 清除残留的页眉页脚和密级标记(多种格式)
        text = _HEADER_FOOTER_RE.sub('', text)
        text = _PAGE_NUMBER_RE.sub('', text)
        text = _DOC_NAME_PAGE_RE.sub('', text)
        text = _CLASSIFICATION_RE.sub('', text)
        text = _VERSION_RE.sub('', text)
        text = _PAGE_LABEL_RE.sub('', text)
        text = _DOC_NUMBER_RE.sub('', text)
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
    _cache_put(_body_text_cache, fp, pages, _CACHE_MAX_DOCS)
    return list(pages)


def extract_full_text(pdf_path: str) -> str:
    """提取PDF全文(所有页拼接)"""
    pages = extract_body_text(pdf_path)
    return '\n'.join(p.text for p in pages)


def _point_in_regions(x: float, y: float, regions: list) -> bool:
    """判断点(x,y)是否落在任一 region 包围盒内(用于正文清理时剔除图/表内的文字)"""
    for r in regions:
        if r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1:
            return True
    return False


def _detect_diagram_regions(page, exclude_bboxes: list) -> list:
    """
    通过矢量绘图"覆盖率"聚类定位"Visio 图"等矢量绘图区域。

    Visio 图在 PDF 中通常是矢量绘图(get_images 抓不到), 其文字是页面真实文字,
    会被当作正文提取, 造成混乱。与文档模板的边框/分隔线(细线、覆盖率低)不同,
    图的局部区域内布满细小图形, 单格"绘图面积占格面积的比例"(覆盖率)明显更高。
    这里按覆盖率找密集网格, 再做连通域分组得到图区域; 并排除落在表格/位图区域
    内的网格(避免重复)。

    Args:
        page: fitz.Page
        exclude_bboxes: 需要排除的包围盒(表格、位图)列表(fitz.Rect)

    Returns:
        list[fitz.Rect]: 检测到的矢量图区域
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    if not drawings:
        return []

    W, H = page.rect.width, page.rect.height
    if W <= 0 or H <= 0:
        return []
    G = 24
    cw, ch = W / G, H / G
    cell_area = cw * ch

    # 每格累计绘图面积(裁剪到格内), 计算覆盖率
    covered = [[0.0] * G for _ in range(G)]
    for d in drawings:
        r = d.get('rect')
        if not r:
            continue
        # 取绘图与所在格的交集面积(近似: 用绘图面积按落点格累加)
        ar = max(0.0, (r.x1 - r.x0)) * max(0.0, (r.y1 - r.y0))
        cx = min(G - 1, int((r.x0 + r.x1) / 2 / cw))
        cy = min(G - 1, int((r.y0 + r.y1) / 2 / ch))
        covered[cy][cx] += min(ar, cell_area)

    # 覆盖率阈值: 模板细线覆盖率低, 图内部覆盖率高
    cov_thr = 0.10
    dense = set()
    for y in range(G):
        for x in range(G):
            cov = covered[y][x] / cell_area
            if cov >= cov_thr:
                ccx = (x + 0.5) * cw
                ccy = (y + 0.5) * ch
                if any(b.x0 <= ccx <= b.x1 and b.y0 <= ccy <= b.y1
                       for b in exclude_bboxes):
                    continue
                dense.add((y, x))

    if not dense:
        return []

    # 连通域分组(4邻接)
    visited = set()
    groups = []
    for cell in dense:
        if cell in visited:
            continue
        stack = [cell]
        comp = []
        visited.add(cell)
        while stack:
            c = stack.pop()
            comp.append(c)
            cy, cx = c
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (cy + dy, cx + dx)
                if n in dense and n not in visited:
                    visited.add(n)
                    stack.append(n)
        groups.append(comp)

    regions = []
    for comp in groups:
        xs = [c[1] for c in comp]
        ys = [c[0] for c in comp]
        x0 = min(xs) * cw
        x1 = (max(xs) + 1) * cw
        y0 = min(ys) * ch
        y1 = (max(ys) + 1) * ch
        # 过滤过小的连通域(细线/边框/零散标注)
        if (x1 - x0) < cw * 3 or (y1 - y0) < ch * 3:
            continue
        # 过滤占比过小的区域(非主要图示)
        if (x1 - x0) * (y1 - y0) < W * H * 0.04:
            continue
        regions.append(fitz.Rect(x0, y0, x1, y1))
    return regions


def _merge_page_regions(regions: list) -> list:
    """
    将同一页内相邻/密集的图块合并为一个图, 避免一张 Visio 图被拆成几十个零散位图。

    合并规则: 包围盒向外扩展 _MERGE_MARGIN 后相交即合并。
    类型优先级: table > diagram > image(用于附带标签)。
    """
    if not regions:
        return []
    M = _MERGE_MARGIN
    boxes = sorted(((typ, fitz.Rect(b)) for typ, b in regions),
                   key=lambda x: (round(x[1].y0 / 20), x[1].x0))
    merged = []
    for typ, b in boxes:
        placed = False
        for m in merged:
            mb = m['bbox']
            if not (b.x1 < mb.x0 - M or b.x0 > mb.x1 + M
                    or b.y1 < mb.y0 - M or b.y0 > mb.y1 + M):
                m['bbox'] = fitz.Rect(min(mb.x0, b.x0), min(mb.y0, b.y0),
                                      max(mb.x1, b.x1), max(mb.y1, b.y1))
                m['types'].add(typ)
                placed = True
                break
        if not placed:
            merged.append({'bbox': fitz.Rect(b), 'types': {typ}})

    out = []
    for m in merged:
        ts = m['types']
        t = 'table' if 'table' in ts else ('diagram' if 'diagram' in ts else 'image')
        out.append((t, m['bbox']))
    return out


_MERGE_MARGIN = 50.0
_FIG_PAD = 15.0


def _detect_page_regions(page) -> list:
    """
    检测单页中所有"非正文区域"(位图/矢量图/表格), 返回 [(type, fitz.Rect), ...]

    仅计算包围盒。供正文清理(剔除图/表内文本)使用,
    保证正文提取时跳过位图、矢量图与表格内的文字。
    """
    regions = []

    # 1) 位图
    try:
        for img in page.get_images(full=True):
            try:
                r = page.get_image_bbox(img)
                regions.append(('image', fitz.Rect(r.x0, r.y0, r.x1, r.y1)))
            except Exception:
                pass
    except Exception:
        pass

    # 2) 表格 (fitz 原生表格检测, 坐标与 get_text 一致)
    try:
        tf = page.find_tables()
        for t in tf.tables:
            b = t.bbox
            regions.append(('table', fitz.Rect(b.x0, b.y0, b.x1, b.y1)))
    except Exception:
        pass

    # 3) 矢量图 (排除落在表格/位图区域内的密集绘图)
    exclude = [b for _, b in regions]
    for d in _detect_diagram_regions(page, exclude):
        regions.append(('diagram', d))

    # 4) 同页相邻图块合并, 避免零散碎片
    regions = _merge_page_regions(regions)

    # 5) 同一页位图过多(>=5)时, 几乎必然是 Visio 图被拆成多块位图导出,
    #    将所有位图合并为一张图(取并集包围盒), 避免附图页出现几十个碎片。
    img_boxes = [b for t, b in regions if t == 'image']
    if len(img_boxes) >= 5:
        ux0 = min(b.x0 for b in img_boxes)
        uy0 = min(b.y0 for b in img_boxes)
        ux1 = max(b.x1 for b in img_boxes)
        uy1 = max(b.y1 for b in img_boxes)
        rest = [(t, b) for t, b in regions if t != 'image']
        regions = rest + [('image', fitz.Rect(ux0, uy0, ux1, uy1))]

    # 6) 外扩边距: 矢量图/表格的标签常画在图形包围盒外侧, 外扩一点既能把标签
    #    纳入截取的图(便于审核), 也能在清理正文时移除这些外部标签文字。
    pw, ph = page.rect.width, page.rect.height
    padded = []
    for t, b in regions:
        nb = fitz.Rect(max(0.0, b.x0 - _FIG_PAD), max(0.0, b.y0 - _FIG_PAD),
                        min(pw, b.x1 + _FIG_PAD), min(ph, b.y1 + _FIG_PAD))
        padded.append((t, nb))
    return padded


def _page_regions_cached(page, fp: tuple, pno: int) -> list:
    """
    带缓存的页面区域检测。

    _detect_page_regions 内部的 find_tables()/get_drawings() 开销很大, 而正文
    提取与图截取会对同一页各调一次。此处按 (文档指纹, 页号) 缓存包围盒坐标,
    使每页只真正检测一次。缓存的是纯坐标元组, 不持有 fitz 对象。
    """
    key = (fp, pno)
    hit = _cache_get(_page_regions_cache, key)
    if hit is None:
        hit = [(t, (b.x0, b.y0, b.x1, b.y1)) for t, b in _detect_page_regions(page)]
        _cache_put(_page_regions_cache, key, hit, _CACHE_MAX_PAGES)
    return [(t, fitz.Rect(*b)) for t, b in hit]


def _join_line(word_tuples) -> str:
    """将同一行的词按原顺序拼接; CJK 相邻词不插空格, 其余插一个空格"""
    out = []
    prev = None
    for _, w in word_tuples:
        if prev is None:
            out.append(w)
        else:
            sep = '' if (_is_cjk_char(prev[-1]) and _is_cjk_char(w[0])) else ' '
            out.append(sep + w)
        prev = w
    return ''.join(out)


def _extract_text_excluding_regions(page, regions: list) -> str:
    """
    提取单页文本, 忽略落在非正文区域(位图/矢量图/表格)内的文字。

    策略:
    1. regions 为空 -> 直接 page.get_text() (与无图页原逻辑完全一致, 零影响)。
    2. 文本块整体落在某区域内(重叠>0.5) -> 整块忽略。
    3. 部分重叠的块 -> 仅丢弃落在区域内的词, 其余词按行重建(保留正文)。
    """
    if not regions:
        return page.get_text()

    blocks = page.get_text("blocks")
    words = page.get_text("words")
    text_blocks = [(tuple(b[:4]), b[4], b[5]) for b in blocks
                   if len(b) > 6 and b[6] == 0 and b[4].strip()]
    if not text_blocks:
        return page.get_text()

    kept = []
    for tb, txt, bn in text_blocks:
        # 整块压在区域上 -> 忽略
        if any(_bbox_overlap_ratio(tb, r) > 0.5 for r in regions):
            continue
        bw = [w for w in words if w[5] == bn]
        if not bw:
            kept.append(txt.rstrip('\n'))
            continue
        # 仅保留中心不在任何区域内的词
        inside = [w for w in bw
                  if _point_in_regions((w[0] + w[2]) / 2, (w[1] + w[3]) / 2, regions)]
        if not inside:
            kept.append(txt.rstrip('\n'))
            continue
        if len(inside) == len(bw):
            continue
        # 部分落入区域 -> 按行重建, 去掉区域词
        bw_kept = [w for w in bw
                   if not _point_in_regions((w[0] + w[2]) / 2, (w[1] + w[3]) / 2, regions)]
        lines = {}
        for w in bw_kept:
            lines.setdefault(w[6], []).append((w[7], w[4]))
        recon = '\n'.join(_join_line(lines[ln]) for ln in sorted(lines))
        if recon.strip():
            kept.append(recon)
    return '\n'.join(kept)


def _bbox_overlap_ratio(a, b) -> float:
    """计算 bbox a 与 b 的重叠面积占 a 面积的比例 (0~1)"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(1.0, (ax1 - ax0) * (ay1 - ay0))
    return inter / area_a





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

        # 条目ID所在行: 不与下一行合并(保留条目号行与其后正文/标题的换行, 问题5)
        if re.search(ITEM_ID_REGEX, prev):
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
        prev_is_digit = prev_last.isdigit()
        curr_is_digit = curr_first.isdigit()

        # 句号(。)、冒号(：:)后保留换行 — 句子边界和列表引出
        if prev_last in '。：:':
            merged.append(curr)
        elif (prev_is_cjk and curr_is_cjk):
            # CJK/CJK标点 → CJK: 合并
            merged[-1] = prev + curr
        elif prev_is_latin and curr_is_cjk:
            # Latin字母 → CJK: 合并 (如 "RPS\n系统" → "RPS系统")
            merged[-1] = prev + curr
        elif prev_is_latin and (curr_is_latin or curr_is_digit):
            # Latin → Latin/Digit: 合并 (如 "F-SC1\nF-SC2" → "F-SC1F-SC2")
            merged[-1] = prev + curr
        elif prev_is_cjk and (curr_is_latin or curr_is_digit):
            # CJK → Latin/Digit: 合并 (如 "系统采用\nDCS" → "系统采用DCS")
            merged[-1] = prev + curr
        elif prev_is_digit and (curr_is_cjk or curr_is_latin or curr_is_digit):
            # Digit → CJK/Latin/Digit: 合并 (如 "100\n摄氏度" → "100摄氏度")
            merged[-1] = prev + curr
        else:
            # 其他情况(如标点→标点等): 默认合并, 上方守卫条件已排除不应合并的场景
            merged[-1] = prev + curr

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
    fp = _pdf_fingerprint(pdf_path)
    cached = _cache_get(_tables_cache, (fp, last_n_pages))
    if cached is not None:
        return list(cached)

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
    _cache_put(_tables_cache, (fp, last_n_pages), tables, _CACHE_MAX_DOCS)
    return list(tables)


def find_traceability_table(pdf_path: str) -> TableData | None:
    """
    在PDF中查找追踪矩阵附录表
    特征: 表头包含 "需求条目号" 或 "追踪" 相关关键词

    扫描最后30页，支持跨页表格合并（当表格跨页时，将多页的表格行合并为一个完整表格）

    跨页合并策略:
    1. 表头归一化后相同 → 直接合并行
    2. 续页表头不含追踪关键词(非真表头,是数据行) → 将"表头"行也作为数据行合并
    3. 表头不同但都含追踪关键词 → 取行数最多的表

    Args:
        pdf_path: PDF文件路径

    Returns:
        TableData or None: 找到的追踪矩阵表
    """
    # 扫描最后30页
    tables = extract_tables(pdf_path, last_n_pages=30)

    # 追踪表头关键词
    _TRACE_KEYWORDS = ('追踪', '上游', '需求编号', '标志号', '章节号')

    def _is_trace_header(headers: list[str]) -> bool:
        """判断表头是否为追踪关系表的表头
        需要同时包含"条目号/编号"类关键词和"追踪/上游"类关键词,
        避免匹配普通内容表(如仅有"序号|条目号|说明"的表)
        """
        text = ''.join(re.sub(r'\s+', '', h or '') for h in headers)
        has_id = ('条目号' in text) or ('编号' in text) or ('标志号' in text)
        has_trace = any(kw in text for kw in _TRACE_KEYWORDS)
        return has_id and has_trace

    def _norm_headers(headers: list[str]) -> str:
        """归一化表头用于比较(去空白、换行)"""
        return '|'.join(re.sub(r'\s+', '', h or '') for h in headers)

    # 筛选追踪矩阵表
    trace_tables = []
    for table in tables:
        if _is_trace_header(table.headers):
            trace_tables.append(table)

    if not trace_tables:
        return None

    # 如果只有一个表，直接返回
    if len(trace_tables) == 1:
        return trace_tables[0]

    # 多个表：合并跨页表格
    merged_headers = trace_tables[0].headers
    merged_rows = list(trace_tables[0].rows)
    first_page = trace_tables[0].page_number
    merged_norm = _norm_headers(merged_headers)

    for table in trace_tables[1:]:
        table_norm = _norm_headers(table.headers)

        # 策略1: 归一化表头相同 → 跨页延续, 直接合并行
        if table_norm == merged_norm:
            merged_rows.extend(table.rows)
            continue

        # 策略2: 续页"表头"不含追踪关键词 → 不是真表头, 是数据行
        # (跨页时续页可能不重复表头, 第一行数据被extract_tables误当表头)
        if not _is_trace_header(table.headers):
            # 将"表头"行也作为数据行合并
            merged_rows.append([_clean_cell(c) for c in table.headers])
            merged_rows.extend(table.rows)
            continue

        # 策略3: 表头不同但都含追踪关键词 → 可能是另一个表格
        # 取行数最多的
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
    # 支持多种表头格式:
    #   格式A: 本文的需求条目号 | 上游文件章节/需求号 | 说明
    #   格式B: 序号 | 本文档中的设计标志号 | 安全级DCS需求规范书中的需求标志号
    for i, h in enumerate(headers):
        h_norm = re.sub(r'\s+', '', h) if h else ''
        # 下游条目号列: "本文"/"本文档" + ("条目号"/"标志号"/"编号")
        if ('本文' in h_norm) and ('条目号' in h_norm or '标志号' in h_norm or '编号' in h_norm):
            id_col = i
        # 上游引用列: "上游"/"章节" 或 "需求"+("标志号"/"条目号"/"编号"/"章节号")
        elif '上游' in h_norm or '章节' in h_norm:
            ref_col = i
        elif '需求' in h_norm and ('标志号' in h_norm or '条目号' in h_norm or '编号' in h_norm or '章节号' in h_norm):
            ref_col = i
        # 文档名列: "说明"/"文档"/"文件"
        elif '说明' in h_norm or ('文档' in h_norm and '本文' not in h_norm) or ('文件' in h_norm and '本文' not in h_norm):
            doc_col = i

    # 如果doc_col与id_col或ref_col重叠, 说明没有独立的文档名列
    if doc_col == id_col or doc_col == ref_col:
        doc_col = -1

    current_downstream_id = None

    for row in table.rows:
        if len(row) <= max(id_col, ref_col):
            continue

        ds_id = (row[id_col] or '').strip()
        if ds_id:
            current_downstream_id = ds_id

        if not current_downstream_id:
            continue

        # 处理多值单元格
        upstream_refs_raw = row[ref_col] or ''
        upstream_docs_raw = (row[doc_col] or '') if doc_col >= 0 and doc_col < len(row) else ''

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


def find_traceability_table_design(pdf_path: str, last_n_pages: int = 30) -> TableData | None:
    """
    在系统设计PDF中查找追踪矩阵附录表

    系统设计文档的追踪关系表特征:
    - 表头包含 "设计条目号" / "设计标志号" / "本文档中的设计标志号" 等（设计侧标识）
      以及 "需求条目号" / "需求编号" / "需求标志号" 等（需求侧标识）
    - 表头中常含换行(如 "需求标志\\n号")，故先做空白归一化再匹配
    - 表结构有两类:
      Type A: [设计条目号, 需求条目号] — 2列
      Type B: [其他列, 设计条目号, 需求条目号] — 3列

    扫描最后30页，支持跨页表格合并。

    Args:
        pdf_path: 系统设计PDF文件路径
        last_n_pages: 扫描最后N页

    Returns:
        TableData or None: 找到的追踪矩阵表
    """
    tables = extract_tables(pdf_path, last_n_pages=last_n_pages)

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

    # 筛选追踪矩阵表
    trace_tables = [t for t in tables if _is_design_trace_header(t.headers)]

    if not trace_tables:
        return None

    if len(trace_tables) == 1:
        return trace_tables[0]

    # 跨页合并
    merged_headers = trace_tables[0].headers
    merged_rows = list(trace_tables[0].rows)
    first_page = trace_tables[0].page_number
    merged_norm = _norm_header(merged_headers)

    for table in trace_tables[1:]:
        table_norm = _norm_header(table.headers)

        # 策略1: 归一化表头相同 → 跨页延续
        if table_norm == merged_norm:
            merged_rows.extend(table.rows)
            continue

        # 策略2: 续页"表头"不含追踪关键词 → 是数据行
        if not _is_design_trace_header(table.headers):
            merged_rows.append([_clean_cell(c) for c in table.headers])
            merged_rows.extend(table.rows)
            continue

        # 策略3: 表头不同 → 取行数最多的
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
