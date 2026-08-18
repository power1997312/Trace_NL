from __future__ import annotations
"""
需求条目/章节内容提取模块
- 自动检测文档类型(ID型 vs 章节型)
- 提取需求条目内容和章节内容
- 构建内容映射，支持多策略引用解析
"""
import re
import difflib
from dataclasses import dataclass
from config import ITEM_ID_REGEX, MIN_PHRASE_LENGTH


@dataclass
class RequirementItem:
    """需求条目"""
    item_id: str           # 归一化后的ID (如 <DCS-SyRS001>)
    raw_id: str            # 原始ID (如 < DCS-SyRS001>)
    content: str           # 条目正文
    page_number: int = 0   # 所在页码


@dataclass
class SectionItem:
    """章节条目"""
    section_number: str    # 章节号 (如 3.2.2.1)
    section_title: str     # 章节标题 (如 F-SC1级要求)
    content: str           # 章节正文
    full_key: str = ''     # 完整键: 章节号+标题


class ContentMap:
    """文档内容映射，支持多策略引用解析"""

    def __init__(self):
        self._by_id: dict[str, str] = {}        # item_id -> content
        self._by_section: dict[str, str] = {}    # section_number -> content
        self._by_heading: dict[str, str] = {}    # heading_text -> content
        self._by_full_key: dict[str, str] = {}   # section_number+title -> content
        self._all_keys: list[str] = []           # 所有键(用于模糊匹配)

    def add_item(self, item_id: str, content: str):
        normalized = _normalize_id(item_id)
        self._by_id[normalized] = content
        self._all_keys.append(normalized)

    def add_section(self, section_number: str, section_title: str, content: str):
        self._by_section[section_number] = content
        full_key = f"{section_number}{section_title}"
        self._by_full_key[full_key] = content
        self._all_keys.append(full_key)
        if section_title:
            self._by_heading[section_title] = content
            # 也存储 "章节号 标题" 格式
            full_key_with_space = f"{section_number} {section_title}"
            self._by_full_key[full_key_with_space] = content

    def resolve(self, reference: str) -> str | None:
        """
        多策略引用解析

        策略顺序:
        1. 精确ID匹配
        2. 归一化ID匹配(去空格、统一标点)
        3. 精确章节号匹配
        4. 精确full_key匹配
        5. 子串匹配(引用串包含在某键中，或某键包含引用串)
        6. 模糊匹配(difflib >= 0.7)
        """
        ref = reference.strip()
        ref_normalized = _normalize_reference(ref)

        # 1. 精确ID匹配
        if ref in self._by_id:
            return self._by_id[ref]

        # 2. 归一化ID匹配
        norm_id = _normalize_id(ref)
        if norm_id in self._by_id:
            return self._by_id[norm_id]

        # 3. 精确章节号
        if ref in self._by_section:
            return self._by_section[ref]

        # 4. 精确full_key
        if ref in self._by_full_key:
            return self._by_full_key[ref]
        if ref_normalized in self._by_full_key:
            return self._by_full_key[ref_normalized]

        # 5. 子串匹配
        for key, content in self._by_full_key.items():
            key_norm = _normalize_reference(key)
            if ref_normalized in key_norm or key_norm in ref_normalized:
                return content

        # 标题子串匹配
        for heading, content in self._by_heading.items():
            heading_norm = _normalize_reference(heading)
            if ref_normalized in heading_norm or heading_norm in ref_normalized:
                return content

        # 章节号前缀匹配 (如 "3.2.2.1" 匹配 "3.2.2.1 F-SC1级要求")
        ref_num = _extract_leading_number(ref)
        if ref_num:
            best_num = None
            best_content = None
            for sec_num, content in self._by_section.items():
                if not content:  # 跳过空内容
                    continue
                if sec_num == ref_num:
                    # 精确匹配 - 最高优先
                    return content
                if sec_num.startswith(ref_num) or ref_num.startswith(sec_num):
                    # 保留最长匹配
                    if best_num is None or len(sec_num) > len(best_num):
                        best_num = sec_num
                        best_content = content
            if best_content:
                return best_content

        # 6. 模糊匹配
        best_match = None
        best_ratio = 0.0
        for key in self._all_keys:
            key_norm = _normalize_reference(key)
            ratio = difflib.SequenceMatcher(None, ref_normalized, key_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = key

        if best_ratio >= 0.7:
            if best_match in self._by_id:
                return self._by_id[best_match]
            if best_match in self._by_full_key:
                return self._by_full_key[best_match]

        return None

    def get_all_items(self) -> dict[str, str]:
        """获取所有ID型条目"""
        return dict(self._by_id)

    def get_all_sections(self) -> dict[str, str]:
        """获取所有章节型条目"""
        return dict(self._by_section)


def detect_document_type(text: str) -> str:
    """
    自动检测文档类型

    检查全文(而不仅是前5000字符)中是否存在足够的需求条目ID

    Args:
        text: 文档全文

    Returns:
        "id_based" 或 "chapter_based"
    """
    matches = re.findall(ITEM_ID_REGEX, text)
    # 去重后计数
    unique_ids = set(_normalize_id(m) for m in matches)
    if len(unique_ids) >= 3:
        return "id_based"
    return "chapter_based"


def extract_requirement_items(text: str) -> list[RequirementItem]:
    """
    从文本中提取所有需求条目(ID型文档)

    每个条目的内容 = 从该ID结束位置到下一个ID开始位置之间的文本
    当同一ID出现多次时(如缩略语表和正文)，保留内容最长的版本

    Args:
        text: 文档全文

    Returns:
        list[RequirementItem]: 提取的需求条目列表(按首次出现顺序，去重)
    """
    items_dict: dict[str, RequirementItem] = {}
    items_order: list[str] = []

    # 找到所有ID的位置
    id_pattern = re.compile(ITEM_ID_REGEX)
    matches = list(id_pattern.finditer(text))

    if not matches:
        return []

    for i, match in enumerate(matches):
        raw_id = match.group()
        normalized_id = _normalize_id(raw_id)

        # 内容从ID结束到下一个ID开始(或文本结束)
        start = match.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(text)

        content = text[start:end].strip()
        content = _clean_content(content)
        # 章节号及其内容保留在条目正文中(不再截断)
        # 问题5: 去除条目号所在行尾随的标题(与条目号同行的简短标题)
        content = _drop_entry_title_line(content)

        # 去重合并: 同一 ID 多次出现(表格型文档的特征, 如 RPS 功能描述表)
        # 表格型文档中, 同一 ID 在连续行中重复出现, 每行是需求的一个描述片段
        # 策略: 同一 ID 的所有出现内容合并(去重行), 拼成完整需求描述
        # (比"保留最长"更优: 表格型文档的最长出现常吞噬相邻 ID 的内容)
        if normalized_id in items_dict:
            prev_content = items_dict[normalized_id].content
            # 合并所有出现的内容
            merged_lines = list(prev_content.split('\n')) + list(content.split('\n'))
            # 去重行
            seen = set()
            out_lines = []
            for line in merged_lines:
                s = line.strip()
                if s and s not in seen:
                    seen.add(s)
                    out_lines.append(s)
            items_dict[normalized_id] = RequirementItem(
                item_id=normalized_id,
                raw_id=raw_id,
                content='\n'.join(out_lines),
            )
        else:
            items_order.append(normalized_id)
            items_dict[normalized_id] = RequirementItem(
                item_id=normalized_id,
                raw_id=raw_id,
                content=content,
            )

    # 过滤: 去除附录表中的引用条目(内容过短的条目可能是表格中的引用而非实际定义)
    if len(items_dict) > 5:
        lengths = sorted(len(items_dict[iid].content) for iid in items_order)
        median_len = lengths[len(lengths) // 2]
        threshold = max(20, median_len * 0.15)
        items_order = [iid for iid in items_order if len(items_dict[iid].content) >= threshold]

        # 二次清洗: 仅截断包含明显ASCII噪声的超长条目
        # 阈值放宽: median*8 且 >500字符, 避免误伤正常长条目
        for iid in items_order:
            item = items_dict[iid]
            if len(item.content) > median_len * 8 and len(item.content) > 500:
                cleaned = _truncate_noisy_content(item.content, median_len)
                if len(cleaned) < len(item.content):
                    items_dict[iid] = RequirementItem(
                        item_id=item.item_id,
                        raw_id=item.raw_id,
                        content=cleaned,
                        page_number=item.page_number,
                    )

    return [items_dict[iid] for iid in items_order]


def extract_sections(text: str) -> list[SectionItem]:
    """
    从文本中提取所有章节(章节型文档)

    匹配模式: 数字章节号 + 标题文本
    如: "3.2.2.1 F-SC1级要求" 或 "4 RPS的功能描述"

    Args:
        text: 文档全文

    Returns:
        list[SectionItem]: 提取的章节列表
    """
    sections = []

    # 匹配章节号: 支持 "3.2.2.1 F-SC1级要求" "3.2系统功能" "4 RPS功能描述" "2.协调和集成" 等
    # \.? 处理单数字+句点格式(如"2.协调和集成"), 句点纳入章节号
    # 标题必须以CJK字符或大写字母开头
    section_pattern = re.compile(
        r'^(\d+(?:\.\d+)*\.?)\s*([\u4e00-\u9fffA-Z].*?)(?:\n|$)',
        re.MULTILINE
    )

    matches = list(section_pattern.finditer(text))

    if not matches:
        return sections

    for i, match in enumerate(matches):
        section_number = match.group(1)
        section_title = match.group(2).strip()

        # 内容从标题结束到下一个同级或更高级章节开始
        start = match.end()
        if i + 1 < len(matches):
            next_match = matches[i + 1]
            next_number = next_match.group(1)
            # 如果下一个章节号是当前章节的子章节，继续往后找
            # 简化处理: 取到下一个章节标题前
            end = next_match.start()
        else:
            end = len(text)

        content = text[start:end].strip()
        content = _clean_content(content)

        full_key = f"{section_number}{section_title}"

        sections.append(SectionItem(
            section_number=section_number,
            section_title=section_title,
            content=content,
            full_key=full_key,
        ))

    return sections


def build_content_map(text: str, doc_name: str = '') -> ContentMap:
    """
    根据文档类型自动构建内容映射

    Args:
        text: 文档全文
        doc_name: 文档名称(用于日志)

    Returns:
        ContentMap: 内容映射对象
    """
    content_map = ContentMap()
    doc_type = detect_document_type(text)

    if doc_type == "id_based":
        items = extract_requirement_items(text)
        for item in items:
            content_map.add_item(item.item_id, item.content)
    else:
        sections = extract_sections(text)
        for section in sections:
            content_map.add_section(
                section.section_number,
                section.section_title,
                section.content,
            )

    # 无论什么类型，都提取章节信息作为补充
    sections = extract_sections(text)
    for section in sections:
        content_map.add_section(
            section.section_number,
            section.section_title,
            section.content,
        )

    return content_map


def _normalize_id(raw_id: str) -> str:
    """
    归一化条目ID: 去除 < 后的空格和 > 前的空格
    如: "< DCS-SyRS001>" -> "<DCS-SyRS001>"
    """
    raw_id = raw_id.strip()
    if raw_id.startswith('<') and raw_id.endswith('>'):
        inner = raw_id[1:-1].strip()
        return f"<{inner}>"
    return raw_id


def _normalize_reference(ref: str) -> str:
    """
    归一化引用字符串: 去空格、统一全半角
    """
    ref = ref.strip()
    # 全角→半角标点
    ref = ref.replace('（', '(').replace('）', ')').replace('：', ':')
    ref = ref.replace('，', ',').replace('；', ';')
    # 中文枚举逗号"、"统一去除(章节号"3、系统架构设计要求"→"3系统架构设计要求")
    ref = ref.replace('、', '')
    # 去除多余空格
    ref = re.sub(r'\s+', '', ref)
    return ref


def _extract_leading_number(text: str) -> str | None:
    """提取文本开头的数字章节号"""
    match = re.match(r'^(\d+(?:\.\d+)*)', text.strip())
    if match:
        return match.group(1)
    return None


# ============================================================
# 问题1修复: 条目内容边界清洗
# ============================================================

# 行首章节号标题: 如 "2." "1.2" "3." 后跟章节标题(CJK/大写字母)
# 注意: 必须位于行首(\n之后), 以避开 "子系统2.ESFAC" 这类句中的数字
# 允许编号与标题之间隔一个换行(部分PDF中 "2." 独占一行, 标题在下一行)
# 优化: 要求多级编号(如3.2)或单级编号+句点+空格(如"2. 安全级DCS")
#        或单级编号+空格(如"2 安全级DCS"), 防止"2楼"误判
_CHAPTER_HEADING_RE = re.compile(
    r'\n[ \t]*(?:'
    r'\d+(?:\.\d+)+\.?[ \t]*'          # 多级编号: 3.2, 3.2.1, 3.2.1.
    r'|'
    r'\d+\.[ \t]+'                      # 单级编号+句点+空格: 2. xxx
    r'|'
    r'\d+[ \t]+'                        # 单级编号+空格: 2 xxx (不含句点)
    r')(?:\n[ \t]*)?[\u4e00-\u9fffA-Z]'
)


def _truncate_at_chapter_heading(text: str) -> str:
    """
    截断条目内容尾部泄漏的其他章节标题/内容(问题1第二条)

    文档中两个条目号之间常夹杂其他章节(如 "2. 安全级DCS系统总体结构"),
    这些内容应由 extract_sections 按章节号另行提取, 不应混入当前条目正文。
    仅匹配行首的章节号, 避免误删正文中的 "子系统2." 等句中数字。
    """
    if not text:
        return text
    m = _CHAPTER_HEADING_RE.search(text)
    if m:
        return text[:m.start()].strip()
    return text


# ============================================================
# 问题5修复: 丢弃条目号所在行尾随的标题
# ============================================================

_TITLE_DROP_PUNCT = set('。；！？')


def _drop_entry_title_line(text: str) -> str:
    """
    丢弃条目内容首行的简短标题(问题5)

    由于 _merge_intra_sentence_breaks 已保留"条目ID行"与正文的换行,
    条目号同行尾随的标题会成为内容的第一行。若首行为无句末标点的
    简短标题(<=15字)且内容含多行, 则丢弃首行, 从正文开始。
    例: <FZSDCS34-ICADS001> 模块类故障及诊断\\n每一类模块中...
        -> 丢弃"模块类故障及诊断", 保留"每一类模块中..."
    """
    if not text:
        return text
    lines = text.split('\n')
    if len(lines) < 2:
        return text
    first = lines[0].strip()
    if (len(first) <= 15
            and not any(p in first for p in _TITLE_DROP_PUNCT)
            and not first.endswith('：') and not first.endswith(':')):
        return '\n'.join(lines[1:]).strip()
    return text


# ============================================================
# 问题4修复(补充): 矢量架构图文字块移除
# ============================================================
# 架构图节点/连线文字的特征词(矢量图无图片对象, 不能靠图片包围盒排除)
_ARCH_TOKENS = re.compile(
    r'(NC DCS|/DAS|ECP/BUP|RSP/|PIPS-|GW-A|GW-A/B|SVDU|PAC-|PAC-B|协转|'
    r'隔离|点对点光纤通信|硬接线驱动器|停堆断路器|驱动器|架构示意图|示意图|'
    r'ESFAC-|ESFSC|F-SC\d?)'
)
# 架构图大写缩写(要求CJK较少, 避免误判正文如"应包含PIPS、RTC")
_ABBR_RE = re.compile(
    r'\b(PIII|PII|PIV|PTRAIN|ATRAIN|BTU|RTC|TU|PIPS|ESFAC|ESFSC|SVDU|'
    r'PAC|F-SC\d?|NC|DAS|ECP|BUP|RSP|GW)\b'
)


def _is_diagram_like(line: str) -> bool:
    """判断单行是否为架构图文字(节点标签/连线关系)"""
    s = line.strip()
    if not s:
        return False
    cjk = len(re.findall(r'[一-鿿]', s))
    # 真实中文句子(以句末标点结尾)绝不可能是图内节点标签
    # (如 "本项目安全级DCS采用图3所示的系统架构。" 含 F-SC/图3 但属正文)
    if re.search(r'[。！？]$', s):
        return False
    # 含较多CJK且带中文标点(；：、，)的描述性句子/条款, 也非图内文字
    # (如 "PIPS：用于现场传感器信号的调理、供电、分配和隔离；")
    if cjk >= 8 and re.search(r'[。；：、，]', s):
        return False
    # 短缩写/编号行(如 "A1" "≥1" "F-SC2"): 基本无CJK且含字母/符号
    # 注意: 排除纯章节号(如 "3.4.1", 无字母/≥)
    if cjk <= 3 and len(s) <= 12 and re.search(r'[A-Za-z≥]', s):
        return True
    # 含多个 '/' 的连接关系行(如 "NC DCS/DASNC DCS/DASECPRSP/")
    if s.count('/') >= 2 and re.search(r'[A-Z]', s):
        return True
    # 架构图大写缩写节点标签(要求CJK较少且行较短, 避免误判正文)
    if cjk <= 5 and len(s) <= 30 and _ABBR_RE.search(s):
        return True
    # 架构图专用短标签词(如"停堆断路器"、"点对点光纤通信"等),
    # 要求行短且CJK较少(避免误删正文中提及这些词的正常描述句)
    if cjk <= 15 and len(s) <= 40 and _ARCH_TOKENS.search(s):
        return True
    return False


def _remove_arch_diagram_blocks(text: str) -> str:
    """
    移除内容中的矢量架构图文字块(问题4)

    机制:
    - 架构图(如图2/图3 安全级DCS架构示意)常为矢量绘制, 无图片对象,
      其节点标签/连线文字被当作普通文本提取, 污染条目内容。
    - 这些文字表现为连续多行简短标签(>=6行)或带有"图N...架构示意图"
      图注的块。检测并整块删除, 同时保留其前的真实正文。
    """
    if not text:
        return text
    lines = text.split('\n')
    n = len(lines)
    keep = [True] * n

    # 1) 连续 >=5 行图内文字 -> 整段删除
    i = 0
    while i < n:
        if _is_diagram_like(lines[i]):
            j = i
            while j < n and _is_diagram_like(lines[j]):
                j += 1
            if j - i >= 5:
                for k in range(i, j):
                    keep[k] = False
            i = j
        else:
            i += 1

    # 2) 图注行("图N...架构示意图"): 删除图注及其前方连续图内文字
    #    仅匹配"以图N开头"的真正图题行, 避免误删正文中提及"如图3所示"的句子
    #    (如 "本项目安全级DCS采用图3所示的系统架构。" 含 图3+F-SC 但不应以图注删除)
    for i in range(n):
        if keep[i] and re.match(r'图\s*\d+', lines[i].strip()) and _ARCH_TOKENS.search(lines[i]):
            keep[i] = False
            j = i - 1
            while j >= 0 and _is_diagram_like(lines[j]):
                keep[j] = False
                j -= 1

    return '\n'.join(lines[k] for k in range(n) if keep[k])


def _clean_content(text: str) -> str:
    """清理条目内容"""
    # 1. 移除空行(消除PDF提取中的多余空行, 问题1)
    #    原逻辑 '\n'.join(lines) 会保留空字符串 -> 产生 \n\n 多余空行
    lines = [line.strip() for line in text.split('\n')]
    lines = [ln for ln in lines if ln]
    text = '\n'.join(lines)
    # 2. 拆分被 '；' 粘连的列表项(如 "4)…；5)…" -> 分行, 问题1)
    text = re.sub(r'；\s*(?=\d+[)）])', '；\n', text)
    # 3. 移除架构图文字块(矢量图无图片对象, 问题4)
    text = _remove_arch_diagram_blocks(text)
    # 4. 移除ASCII架构图/表格噪声
    text = _remove_ascii_diagrams(text)
    # 5. 剥离尾部章节标题泄漏
    text = _strip_trailing_headings(text)
    # 6. 剥离尾部附录表引用
    text = _strip_trailing_appendix_refs(text)
    return text.strip()


# ============================================================
# ASCII图表/架构图噪声检测与清除
# ============================================================

# ASCII图表特征字符(制表符、框线、箭头等)
_ASCII_DIAGRAM_CHARS = re.compile(r'[│├└┌┐┘┬┴┼─┤├└┌┐┘┬┴┼─╔╗╚╝║═╠╣╦╩╬→←↑↓▲▼►◄]')


def _is_ascii_noise_line(line: str) -> bool:
    """
    判断单行是否为ASCII图表/架构图噪声

    噪声特征:
    1. 包含大量制表/框线字符
    2. CJK字符占比极低(正常需求文本CJK占比通常>30%)
    3. 含大量空格分隔的短英文缩写(如"PIPS-1 ESFAC-A1 >=1")
    4. 全ASCII短行(图表标签, 如"PIPS-1", "ESFAC-A1", ">=1")
    """
    if not line.strip():
        return False
    # 特征1: 包含图表专用字符
    if _ASCII_DIAGRAM_CHARS.search(line):
        return True

    # 特征2: 全ASCII短行且含图表标签特征(大写字母-数字, >=, 等)
    chars = line.strip()
    if len(chars) < 3:
        return False

    cjk_count = sum(1 for c in chars if '\u4e00' <= c <= '\u9fff')
    cjk_ratio = cjk_count / len(chars) if len(chars) > 0 else 0

    # 特征4: 全ASCII图表标签 — 无CJK, 短(<30), 含大写字母/特殊符号
    if cjk_count == 0 and len(chars) <= 30:
        import re as re_inner
        # 图表标签典型模式: 大写字母+连字符+数字, 或以>=/<=开头
        if re_inner.search(r'^[>=<]', chars) or re_inner.search(r'[A-Z]{2,}[-\d]|\d[-][A-Z]', chars):
            return True

    # 特征2: CJK占比极低 + 多token
    if cjk_ratio < 0.10 and len(chars) > 5:
        tokens = chars.split()
        short_tokens = [t for t in tokens if len(t) <= 8]
        if len(short_tokens) >= 3 and len(tokens) >= 3:
            return True

    return False


def _remove_ascii_diagrams(text: str) -> str:
    """
    检测并移除PDF提取中混入的ASCII架构图/表格

    当连续多行(>=3)为图表噪声时，将这些行移除。
    对于分散的单行噪声也予以清除。
    """
    if not text:
        return text

    lines = text.split('\n')
    if len(lines) < 3:
        return text

    # 标记每行是否为噪声
    noise_flags = [_is_ascii_noise_line(line) for line in lines]

    # 策略1: 连续>=3行噪声 → 整块移除
    i = 0
    while i < len(noise_flags):
        if noise_flags[i]:
            j = i
            while j < len(noise_flags) and noise_flags[j]:
                j += 1
            if j - i >= 3:
                for k in range(i, j):
                    noise_flags[k] = True  # 确认标记
            i = j
        else:
            i += 1

    # 策略2: 统计总噪声行占比，如果>30%且文本较长(>100字符)，
    # 则移除所有噪声行
    noise_count = sum(noise_flags)
    total_chars = sum(len(line) for line in lines)
    if noise_count > 0 and noise_count / len(lines) > 0.30 and total_chars > 100:
        cleaned = [line for line, is_noise in zip(lines, noise_flags) if not is_noise]
        return '\n'.join(cleaned)

    # 策略3: 即使噪声行不多，如果存在连续噪声块也移除
    if noise_count >= 3:
        cleaned = [line for line, is_noise in zip(lines, noise_flags) if not is_noise]
        return '\n'.join(cleaned)

    return text


def _truncate_noisy_content(text: str, median_len: int = 0) -> str:
    """
    检测并截断包含ASCII噪声的过长内容

    仅当检测到大段非CJK文本(ASCII图表/架构图)时才截断。
    对于纯CJK文本的长条目(如详细描述的系统需求)，不做截断。
    """
    if not text or len(text) < 200:
        return text

    lines = text.split('\n')
    # 找到第一个CJK文本密度骤降的位置
    consecutive_non_cjk = 0
    for i, line in enumerate(lines):
        chars = line.strip()
        if not chars:
            consecutive_non_cjk += 1
            continue
        cjk_count = sum(1 for c in chars if '\u4e00' <= c <= '\u9fff')
        if cjk_count / max(len(chars), 1) < 0.10:
            consecutive_non_cjk += 1
        else:
            consecutive_non_cjk = 0

        # 连续3行非CJK文本 → 截断到此处
        if consecutive_non_cjk >= 3:
            truncated = '\n'.join(lines[:i - consecutive_non_cjk + 1])
            if len(truncated.strip()) >= 30:
                return truncated.strip()

    # 无ASCII噪声 → 不截断，保留完整内容
    return text


# 尾部章节标题正则: 匹配 "\n3.2系统功能" "\n3.3 系统架构" "\n2 安全级DCS" 等
# 与_CHAPTER_HEADING_RE保持一致: 要求多级编号/单级+句点空格/单级+空格
# 防止"4楼绿色33..."等表格数据被误判为章节标题
_TRAILING_HEADING_RE = re.compile(
    r'\n+'
    r'(?:'
    r'\d+(?:\.\d+)+\.?[ \t]*[\u4e00-\u9fffA-Za-z].{0,40}'   # 多级编号+标题: 3.2系统功能
    r'|\d+(?:\.\d+)+\.?[ \t]*(?=\n)'                        # 多级编号独占行: 3.4.1
    r'|\d+\.[ \t]+[\u4e00-\u9fffA-Za-z].{0,40}'              # 单级+句点+空格: 2. 安全级DCS
    r'|\d+[ \t]+[\u4e00-\u9fffA-Za-z].{0,40}'                # 单级+空格: 2 安全级DCS
    r'|[\u4e00-\u9fff]{2,25}[（(][A-Za-z][A-Za-z0-9/\-,]*[)）]'  # 子标题(全角括号): 紧急停堆系统（RTS）
    r')'
    r'(?:\n+(?:'
    r'\d+(?:\.\d+)+\.?[ \t]*[\u4e00-\u9fffA-Za-z].{0,40}'
    r'|\d+(?:\.\d+)+\.?[ \t]*(?=\n)'
    r'|\d+\.[ \t]+[\u4e00-\u9fffA-Za-z].{0,40}'
    r'|\d+[ \t]+[\u4e00-\u9fffA-Za-z].{0,40}'
    r'|[\u4e00-\u9fff]{2,25}[（(][A-Za-z][A-Za-z0-9/\-,]*[)）]'
    r'))*'
    r'\s*$',  # 直到文本末尾
)
# 尾部子章节标题: 如 "反应堆保护单元(RTC)" "专设安全设施驱动系统单元(ESFAC)"
# 允许前面有换行或直接附在正文尾部; 支持半角/全角括号
_TRAILING_SUBHEADING_RE = re.compile(
    r'(?:\n+|(?<=[\u3002\uff0e.]))'          # 前面是换行或句号
    r'([\u4e00-\u9fff]{2,20})'                # CJK文本(2-20字)
    r'[（(][A-Za-z][A-Za-z0-9/\-,]*[)）]'      # 括号内英文缩写(全角/半角)
    r'\s*$',                                   # 直到文本末尾
)


def _strip_trailing_headings(text: str) -> str:
    """
    剥离条目内容尾部泄漏的章节标题

    处理场景:
    1. 编号章节标题: "3.2系统的功能要求", "3.4.1紧急停堆系统（RTS）"
    2. 连续多个标题: "3.4各子系统设计要求\\n3.4.1\\n紧急停堆系统"
    3. 子章节标题: "反应堆保护单元(RTC)"
    """
    if not text:
        return text

    # 尝试匹配编号章节标题
    match = _TRAILING_HEADING_RE.search(text)
    if match:
        # 验证匹配的内容确实是章节标题(不是正常文本)
        heading_text = match.group().strip()
        # 如果标题文本很短(<60字符)，大概率是泄漏的标题
        if len(heading_text) < 60:
            text = text[:match.start()]

    # 尝试匹配子章节标题
    match = _TRAILING_SUBHEADING_RE.search(text)
    if match:
        heading_text = match.group().strip()
        # 子章节标题特征: 以括号内英文缩写结尾，且较短
        if len(heading_text) < 60:
            text = text[:match.start()]

    return text


# 尾部附录表引用正则: 匹配 "附录A需求追踪" "附录B" "下表A所示" 等
_TRAILING_APPENDIX_RE = re.compile(
    r'(?:\n+|(?<=[。\n]))'                    # 前面是换行或句号
    r'(?:\d+[)）]\s*)?'                       # 可选的编号前缀(如"3)")
    r'(?:附录[A-Za-z\d]|下表[A-Za-z]所示|'    # "附录A" 或 "下表A所示"
    r'需求追踪[关关]?系|'                       # "需求追踪关系"
    r'本文件与上游文件|'                         # "本文件与上游文件"
    r'追踪关系间?[下间])'                       # "追踪关系间下表"
    r'.*$',                                    # 直到文本末尾
    re.DOTALL
)


def _strip_trailing_appendix_refs(text: str) -> str:
    """
    剥离条目内容尾部泄漏的附录表引用文本

    处理场景:
    1. "3)附录A需求追踪本文件与上游文件的需求追踪关系间下表A所示..."
    2. "附录A 追踪关系表..."
    3. "下表A所示。表A与上游文件的需求追踪关系表..."
    """
    if not text:
        return text

    match = _TRAILING_APPENDIX_RE.search(text)
    if match:
        matched = match.group().strip()
        # 安全校验: 匹配文本不应太长(防止误删正常内容)
        if len(matched) < 200:
            text = text[:match.start()]

    return text.strip()
