from __future__ import annotations
"""
微块级文本匹配引擎
- 三级微块分解: 段落 → 句子 → 短语
- 两阶段匹配: 嵌入相似度粗筛 + 字符级精确验证 / NLI语义验证
- 双向匹配 + 结果合并
- 子短语GREEN扫描 + 结构化短语提纯 + 最优匹配
"""
import re
import numpy as np
from dataclasses import dataclass, field
from config import (
    MatchCategory,
    EXACT_CHAR_THRESHOLD,
    EMBEDDING_EXACT_THRESHOLD,
    EMBEDDING_SEMANTIC_THRESHOLD,
    EMBEDDING_UNMATCHED_THRESHOLD,
    NLI_ENTAILMENT_THRESHOLD,
    NLI_NEUTRAL_EMBEDDING_THRESHOLD,
    MIN_PHRASE_LENGTH,
)


@dataclass
class MicroBlock:
    """微块"""
    text: str
    level: int  # 0=paragraph, 1=sentence, 2=phrase
    start: int = 0
    end: int = 0
    category: MatchCategory = MatchCategory.BLACK
    children: list = field(default_factory=list)
    clean_text: str = ''  # 提纯后的文本(去编号前缀+功能后缀, 用于嵌入编码)


@dataclass
class TextRun:
    """着色文本运行(用于Excel RichText输出)"""
    text: str
    category: MatchCategory


@dataclass
class MatchResult:
    """单对文本的匹配结果"""
    downstream_runs: list[TextRun]   # 下游文本着色结果
    upstream_runs: list[TextRun]     # 上游文本着色结果
    overall_category: MatchCategory  # 整体匹配类别
    downstream_text: str
    upstream_text: str


# ============================================================
# 三级微块分解
# ============================================================

# 句子分割标点
_SENTENCE_SPLIT_RE = re.compile(r'(?<=[。；;！!？?])\s*')
# 短语分割标点 (包含\n以拆分不同行的列表项)
_PHRASE_SPLIT_RE = re.compile(r'(?<=[，、,：:；;.。\n])\s*|(?=——)')
# 编号列表项 (支持 "- " 和 "-"直接跟CJK字符 两种格式)
_LIST_ITEM_RE = re.compile(r'^\s*(\d+[)）]|[a-zA-Z][)）]|[-•●](?:\s|(?=[\u4e00-\u9fff])))')


def decompose_text(text: str) -> list[MicroBlock]:
    """
    将文本分解为三级微块(段落→句子→短语)
    返回最细粒度(短语级)的微块列表，保持原始文本顺序
    每个微块记录其在原文中的start/end位置(用于保真重建)

    Args:
        text: 待分解的文本

    Returns:
        list[MicroBlock]: 短语级微块列表(含位置信息)
    """
    if not text or not text.strip():
        return []

    # 段落分割
    paragraphs = _split_paragraphs(text)
    all_phrases = []
    search_offset = 0  # 用于在原文中追踪位置

    for para in paragraphs:
        # 句子分割
        sentences = _split_sentences(para.text)
        for sent in sentences:
            # 短语分割
            phrases = _split_phrases(sent)
            for phrase_text in phrases:
                stripped = phrase_text.strip()
                if stripped:
                    # 在原文中查找此短语的位置
                    pos = text.find(stripped, search_offset)
                    if pos == -1:
                        # 回退: 用核心子串查找
                        core = stripped[:max(4, len(stripped) // 2)]
                        pos = text.find(core, search_offset)
                    if pos == -1:
                        pos = search_offset  # 兜底

                    all_phrases.append(MicroBlock(
                        text=stripped,
                        level=2,
                        start=pos,
                        end=pos + len(stripped),
                    ))
                    search_offset = pos + len(stripped)

    # 合并过短的短语
    all_phrases = _merge_short_phrases(all_phrases, text)

    # 合并PDF换行导致的碎片化短语(如"每个系列的\n"+"ESFAC接收...")
    # 注意: 此函数会合并跨\n的短短语, 但基准标注使用细粒度短语分割,
    # 合并反而导致与基准不对齐。暂时禁用。
    # all_phrases = _merge_newline_trailing_phrases(all_phrases, text)

    # 合并枚举分割短语: 修复"、"导致的过度碎片化(如"A、"+"B逻辑系列")
    # all_phrases = _merge_enumeration_phrases(all_phrases, text)  # 暂时禁用

    # 结构化短语提纯: 去除编号前缀和功能类型词后缀
    for phrase in all_phrases:
        phrase.clean_text = _purify_phrase(phrase.text)

    return all_phrases


def _split_paragraphs(text: str) -> list[MicroBlock]:
    """按换行分割段落，保留编号列表项"""
    lines = text.split('\n')
    paragraphs = []
    current = []

    for line in lines:
        line = line.strip()
        if not line:
            if current:
                paragraphs.append(MicroBlock(
                    text='\n'.join(current), level=0
                ))
                current = []
            continue

        # 编号列表项作为独立段落
        if _LIST_ITEM_RE.match(line) and current:
            paragraphs.append(MicroBlock(
                text='\n'.join(current), level=0
            ))
            current = [line]
        else:
            current.append(line)

    if current:
        paragraphs.append(MicroBlock(
            text='\n'.join(current), level=0
        ))

    return paragraphs


def _split_sentences(text: str) -> list[str]:
    """按句末标点分割"""
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _split_phrases(text: str) -> list[str]:
    """按逗号/顿号/冒号/破折号分割短语"""
    if len(text) <= MIN_PHRASE_LENGTH:
        return [text]

    parts = _PHRASE_SPLIT_RE.split(text)
    result = []
    for p in parts:
        p = p.strip()
        if p:
            # 保留分割标点在前一个短语末尾
            result.append(p)
    return result if result else [text]


def _merge_short_phrases(phrases: list[MicroBlock], original_text: str = '') -> list[MicroBlock]:
    """合并过短的短语(少于MIN_PHRASE_LENGTH字符)与相邻短语，保持位置追踪"""
    if len(phrases) <= 1:
        return phrases

    merged = []
    buffer_blocks = []

    for p in phrases:
        buffer_blocks.append(p)
        # 计算合并后文本长度(用原文切片)
        if original_text and buffer_blocks:
            buf_start = buffer_blocks[0].start
            buf_end = buffer_blocks[-1].end
            buf_len = buf_end - buf_start
        else:
            buf_len = sum(len(b.text) for b in buffer_blocks)

        if buf_len >= MIN_PHRASE_LENGTH:
            if len(buffer_blocks) == 1:
                merged.append(buffer_blocks[0])
            else:
                # 合并: 使用第一个到最后一个的位置范围
                merged.append(MicroBlock(
                    text=original_text[buffer_blocks[0].start:buffer_blocks[-1].end] if original_text
                         else ''.join(b.text for b in buffer_blocks),
                    level=2,
                    start=buffer_blocks[0].start,
                    end=buffer_blocks[-1].end,
                ))
            buffer_blocks = []

    if buffer_blocks:
        if merged:
            # 追加到最后一个短语
            last = merged[-1]
            merged[-1] = MicroBlock(
                text=original_text[last.start:buffer_blocks[-1].end] if original_text
                     else last.text + ''.join(b.text for b in buffer_blocks),
                level=2,
                start=last.start,
                end=buffer_blocks[-1].end,
            )
        else:
            b = buffer_blocks[0]
            merged.append(MicroBlock(
                text=original_text[b.start:buffer_blocks[-1].end] if original_text
                     else ''.join(bl.text for bl in buffer_blocks),
                level=2,
                start=b.start,
                end=buffer_blocks[-1].end,
            ))

    return merged


def _merge_enumeration_phrases(
    phrases: list[MicroBlock], original_text: str = ''
) -> list[MicroBlock]:
    """
    合并枚举分割短语: 当短语在"、"(枚举逗号)处被拆分时，
    将相邻的短枚举片段合并回一个完整短语。

    触发条件:
    - 当前短语以"、"结尾(枚举分割标记)
    - 当前短语长度 ≤ 25字符
    - 合并后总长度 ≤ 50字符

    例: "保护组I、" + "保护组II、" → "保护组I、保护组II、"
        "包括A、" + "B逻辑系列，" → "包括A、B逻辑系列，"
    """
    if len(phrases) <= 1:
        return phrases

    _MAX_SINGLE_LEN = 25   # 单个短语最大长度(触发合并)
    _MAX_COMBINED_LEN = 50  # 合并后最大长度

    merged = []
    i = 0
    while i < len(phrases):
        p = phrases[i]

        # 检查是否以"、"结尾(枚举逗号)
        ends_with_enum = p.text.rstrip().endswith('、')

        if ends_with_enum and len(p.text) <= _MAX_SINGLE_LEN and i + 1 < len(phrases):
            # 尝试向后合并
            combined_text = p.text
            combined_end = p.end
            j = i + 1

            while j < len(phrases):
                next_text = combined_text + phrases[j].text
                if len(next_text) > _MAX_COMBINED_LEN:
                    break

                combined_text = next_text
                combined_end = phrases[j].end
                j += 1

                # 如果上一个合并后的短语不再以"、"结尾，停止合并
                if not combined_text.rstrip().endswith('、'):
                    break

            if j > i + 1:
                # 合并成功
                merged.append(MicroBlock(
                    text=original_text[p.start:combined_end] if original_text
                         else combined_text,
                    level=2,
                    start=p.start,
                    end=combined_end,
                ))
                i = j
                continue

        merged.append(p)
        i += 1

    return merged


def _merge_newline_trailing_phrases(
    phrases: list[MicroBlock], original_text: str = ''
) -> list[MicroBlock]:
    """
    合并尾部为\n的短短语与下一个短语

    PDF提取中，\n经常出现在短语中间(如"每个系列的\\nESFAC接收...")。
    短语分割在\n处拆分会产生碎片化的短语(如"每个系列的"和"ESFAC接收...")。
    当两个短语之间在原文中仅以\\n分隔，且前一个短语较短(≤20字符)时，合并它们。
    """
    if len(phrases) <= 1 or not original_text:
        return phrases

    merged = []
    skip_next = False

    for i in range(len(phrases)):
        if skip_next:
            skip_next = False
            continue

        p = phrases[i]

        if i + 1 < len(phrases) and len(p.text) <= 20:
            next_p = phrases[i + 1]
            # 检查原文中两个短语之间的间隔
            if p.end <= next_p.start:
                gap = original_text[p.end:next_p.start]
                # 如果间隔仅为换行符(可能带少量空白)，合并
                if gap.strip() == '' and '\n' in gap and len(gap) <= 3:
                    merged_text = original_text[p.start:next_p.end].strip()
                    merged.append(MicroBlock(
                        text=merged_text,
                        level=2,
                        start=p.start,
                        end=next_p.end,
                    ))
                    skip_next = True
                    continue

        merged.append(p)

    return merged


# ============================================================
# 结构化短语提纯 (Solution 4)
# ============================================================

# 编号前缀: "1)" "a)" "- " "• " 等
_PHRASE_PREFIX_RE = re.compile(
    r'^\s*(?:\d+[)）]|[a-zA-Z][)）]|[-•●·]\s*)'
)

# 功能类型词后缀: "功能" "系统" "模块" "装置" "单元" "组件" + 可选标点
# 注意: 标点部分是可选的, 因为结果短语可能以功能词直接结尾(无尾部标点)
_PHRASE_SUFFIX_RE = re.compile(
    r'(?:功能|系统|模块|装置|单元|组件)[；;。，,：:\s]*$'
)
# 无标点版本: 匹配纯功能词结尾
_PHRASE_SUFFIX_BARE_RE = re.compile(
    r'(?:功能|系统|模块|装置|单元|组件)$'
)


def _purify_phrase(text: str) -> str:
    """
    提纯短语: 去除编号前缀和功能类型词后缀

    例: "1)自动停堆功能；" → "自动停堆"
         "2)专设安全设施驱动系统功能；" → "专设安全设施驱动"
         "-在发生EMA/EMB低压事件时启动柴油发电机" → 不变(过短)
    """
    clean = text.strip()

    # 去除编号前缀
    clean = _PHRASE_PREFIX_RE.sub('', clean).strip()

    # 去除尾部标点(保留有意义的冒号)
    clean = re.sub(r'[；;。，,\s]+$', '', clean)

    # 去除功能类型词后缀(仅对10字符以上的短语，保护短短语完整性)
    if len(clean) > 10:
        new_clean = _PHRASE_SUFFIX_RE.sub('', clean).strip()
        if len(new_clean) >= 3:
            clean = new_clean
        else:
            # 尝试无标点版本(结果短语可能以功能词直接结尾)
            new_clean = _PHRASE_SUFFIX_BARE_RE.sub('', clean).strip()
            if len(new_clean) >= 3 and new_clean != clean:
                clean = new_clean

    return clean if clean else text.strip()


# ============================================================
# 字符级相似度
# ============================================================

# Unicode罗马数字→ASCII 映射表(模块级常量, 避免每次归一化都重建字典)
_ROMAN_ALL = {
    'Ⅰ': 'I', 'Ⅱ': 'II', 'Ⅲ': 'III', 'Ⅳ': 'IV', 'Ⅴ': 'V', 'Ⅵ': 'VI',
    'Ⅶ': 'VII', 'Ⅷ': 'VIII', 'Ⅸ': 'IX', 'Ⅹ': 'X', 'Ⅺ': 'XI', 'Ⅻ': 'XII',
    'ⅰ': 'i', 'ⅱ': 'ii', 'ⅲ': 'iii', 'ⅳ': 'iv', 'ⅴ': 'v', 'ⅵ': 'vi',
    'ⅶ': 'vii', 'ⅷ': 'viii', 'ⅸ': 'ix', 'ⅹ': 'x', 'ⅺ': 'xi', 'ⅻ': 'xii',
}
# 全角→半角 + 罗马数字, 合并为一次 str.translate(比逐个 replace 快得多)
_TRANSLATE_MAP = str.maketrans({
    '（': '(', '）': ')', '：': ':', '，': ',', '；': ';',
    '！': '!', '？': '?', '、': ',',
    **_ROMAN_ALL,
})

# 归一化用正则(预编译, 避免每次调用重新解析)
_RE_PUA = re.compile(r'[\uf000-\uf8ff]')
_RE_BULLET = re.compile(r'(?m)^[\s]*[-•●·]\s*')
_RE_NUM_PREFIX = re.compile(r'(?m)^\s*(?:\d+[)）]|[a-zA-Z][)）])')
_RE_WS = re.compile(r'\s+')
_RE_TAIL_PUNCT = re.compile(r'[；;。，,：:!\?]+$')
_RE_FUNC_SUFFIX = re.compile(r'(?:功能|系统|模块|装置|单元|组件)$')
_RE_L_TO_1_A = re.compile(r'(?<=[A-Z])l(?=[A-Z\d])')
_RE_L_TO_1_B = re.compile(r'(?<=[A-Z])l$')
_RE_L_TO_1_C = re.compile(r'(?<=\d)l(?=[A-Z])')
_RE_O_TO_0_A = re.compile(r'(?<=\d)O(?=\d)')
_RE_O_TO_0_B = re.compile(r'(?<=\d)o(?=\d)')

# 归一化结果缓存。同一段文本会被 LCS/Jaccard/子串扫描等反复归一化,
# 而归一化本身是纯函数, 缓存后重复调用直接返回。
_NORM_CACHE_MAX = 100000
_norm_cache: dict[str, str] = {}


def _normalize_for_char_compare(text: str) -> str:
    """归一化文本用于字符级比较(纯函数, 结果带缓存)"""
    cached = _norm_cache.get(text)
    if cached is not None:
        return cached

    t = text.strip()
    # 全角→半角 + Unicode罗马数字→ASCII (Ⅰ→I, Ⅱ→II, Ⅲ→III, Ⅳ→IV 等)
    t = t.translate(_TRANSLATE_MAP)
    # 去除PDF私有区字符(Wingdings bullet等，防御性清理)
    t = _RE_PUA.sub('', t)
    # 去除列表标记和bullet符号
    t = _RE_BULLET.sub('', t)
    # 去除数字/字母编号前缀 (如 "1)", "2）", "a)", "B）")
    # 使GREEN匹配对上下游编号差异免疫
    t = _RE_NUM_PREFIX.sub('', t)
    # 去除空格和换行
    t = _RE_WS.sub('', t)
    # 去除尾部标点(使后缀$匹配能生效)
    t = _RE_TAIL_PUNCT.sub('', t)
    # 去除功能类型词后缀 (使GREEN匹配对"功能"等后缀差异免疫)
    t = _RE_FUNC_SUFFIX.sub('', t)

    # PDF字符混淆归一化 (Solution 5)
    # 上下文感知替换: 仅在高置信度场景下应用
    # l→1: 全大写上下文 (如 "QAl" → "QA1", "QA2" 等质保等级编号)
    t = _RE_L_TO_1_A.sub('1', t)
    t = _RE_L_TO_1_B.sub('1', t)
    t = _RE_L_TO_1_C.sub('1', t)
    # O→0: 数字序列中 (如 "1O" → "10")
    t = _RE_O_TO_0_A.sub('0', t)
    t = _RE_O_TO_0_B.sub('0', t)

    if len(_norm_cache) < _NORM_CACHE_MAX:
        _norm_cache[text] = t
    return t


# 字符级相似度的结果缓存。
# 三个指标都是对称的(交并集/LCS 与参数顺序无关), 故用有序对做键, 命中率翻倍。
_SIM_CACHE_MAX = 200000
_bigram_cache: dict[tuple[str, str], float] = {}
_charset_cache: dict[tuple[str, str], float] = {}
_lcs_cache: dict[tuple[str, str], float] = {}


def clear_matcher_cache() -> None:
    """清空文本匹配相关的纯函数缓存"""
    _norm_cache.clear()
    _bigram_cache.clear()
    _charset_cache.clear()
    _lcs_cache.clear()


def _char_bigram_jaccard(text_a: str, text_b: str) -> float:
    """计算字符bigram的Jaccard系数"""
    key = (text_a, text_b) if text_a <= text_b else (text_b, text_a)
    hit = _bigram_cache.get(key)
    if hit is not None:
        return hit

    a = _normalize_for_char_compare(text_a)
    b = _normalize_for_char_compare(text_b)

    if not a or not b:
        val = 0.0
    elif a == b:
        val = 1.0
    else:
        # 生成bigram集合
        set_a = set(a[i:i+2] for i in range(len(a) - 1)) if len(a) > 1 else {a}
        set_b = set(b[i:i+2] for i in range(len(b) - 1)) if len(b) > 1 else {b}

        intersection = len(set_a & set_b)
        union = len(set_a | set_b)

        val = intersection / union if union > 0 else 0.0

    if len(_bigram_cache) < _SIM_CACHE_MAX:
        _bigram_cache[key] = val
    return val


def _char_set_jaccard(text_a: str, text_b: str) -> float:
    """
    计算字符集合(去重)的Jaccard系数
    比bigram Jaccard更容忍后缀差异，用于GREEN精确匹配
    """
    key = (text_a, text_b) if text_a <= text_b else (text_b, text_a)
    hit = _charset_cache.get(key)
    if hit is not None:
        return hit

    a = _normalize_for_char_compare(text_a)
    b = _normalize_for_char_compare(text_b)

    if not a or not b:
        val = 0.0
    elif a == b:
        val = 1.0
    else:
        set_a = set(a)
        set_b = set(b)

        intersection = len(set_a & set_b)
        union = len(set_a | set_b)

        val = intersection / union if union > 0 else 0.0

    if len(_charset_cache) < _SIM_CACHE_MAX:
        _charset_cache[key] = val
    return val


def _lcs_ratio(text_a: str, text_b: str) -> float:
    """
    计算最长公共子序列比率
    LCS比率 = |LCS(a,b)| / max(|a|, |b|)
    衡量两段文本的序列重叠度，对插入/删除更鲁棒
    """
    key = (text_a, text_b) if text_a <= text_b else (text_b, text_a)
    hit = _lcs_cache.get(key)
    if hit is not None:
        return hit

    val = _lcs_ratio_uncached(text_a, text_b)
    if len(_lcs_cache) < _SIM_CACHE_MAX:
        _lcs_cache[key] = val
    return val


def _lcs_ratio_uncached(text_a: str, text_b: str) -> float:
    a = _normalize_for_char_compare(text_a)
    b = _normalize_for_char_compare(text_b)

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # 限制长度避免O(n²)性能问题
    max_len = 200
    if len(a) > max_len or len(b) > max_len:
        a = a[:max_len]
        b = b[:max_len]

    m, n = len(a), len(b)
    # 使用滚动数组优化空间
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)

    for i in range(1, m + 1):
        ai = a[i - 1]
        prev_row = prev
        # 逐列递推; 局部变量缓存上一格的值, 省去重复索引
        left = 0
        for j in range(1, n + 1):
            if ai == b[j - 1]:
                left = prev_row[j - 1] + 1
            else:
                up = prev_row[j]
                left = up if up > left else left
            curr[j] = left
        prev, curr = curr, prev

    lcs_len = prev[n]
    return lcs_len / max(m, n)


# ============================================================
# PDF ASCII图表噪声清理 (用于匹配前预处理)
# ============================================================

# 技术标签: 纯ASCII短标签，如 "PIPS-1", "ESFAC-A1", "≥1", "A1/B2"
_TECH_LABEL_RE = re.compile(
    r'^[A-Za-z0-9][A-Za-z0-9+\-/≥≤><._]{0,5}[A-Za-z0-9+\-/≥≤><.]?$'
)


def _strip_ascii_noise_for_matching(text: str) -> tuple[str, list[int]]:
    """
    从PDF提取的文本中剥离ASCII图表噪声，用于匹配前的预处理。

    返回:
        (cleaned_text, pos_map):
        cleaned_text: 清理后的文本
        pos_map: pos_map[i] = cleaned_text第i个字符在原文中的位置
    """
    if not text or len(text) < 100:
        return text, list(range(len(text)))

    lines = text.split('\n')

    # 检测连续短ASCII行序列(≥2行连续短纯ASCII行 = 图表噪声)
    noise_line_indices = set()
    consecutive_ascii = 0
    consecutive_start = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            # 空行重置计数，但如果已有≥2行则标记
            if consecutive_ascii >= 2:
                for k in range(consecutive_start, i):
                    noise_line_indices.add(k)
            consecutive_ascii = 0
            consecutive_start = -1
            continue

        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in stripped)
        if not has_cjk and (len(stripped) <= 8 or _TECH_LABEL_RE.match(stripped)):
            if consecutive_ascii == 0:
                consecutive_start = i
            consecutive_ascii += 1
        else:
            if consecutive_ascii >= 2:
                for k in range(consecutive_start, i):
                    noise_line_indices.add(k)
            consecutive_ascii = 0
            consecutive_start = -1

    # 处理尾部连续ASCII行
    if consecutive_ascii >= 2:
        for k in range(consecutive_start, len(lines)):
            noise_line_indices.add(k)

    if not noise_line_indices:
        return text, list(range(len(text)))

    # 构建清理后文本和位置映射
    cleaned_parts = []
    pos_map = []
    current_pos = 0

    for i, line in enumerate(lines):
        if i > 0:
            cleaned_parts.append('\n')
            pos_map.append(current_pos)  # \n的位置
            current_pos += 1  # 跳过原文中的\n

        if i not in noise_line_indices:
            for ch in line:
                cleaned_parts.append(ch)
                pos_map.append(current_pos)
                current_pos += 1
        else:
            # 跳过噪声行的字符(但保留换行位置追踪)
            current_pos += len(line)

    cleaned = ''.join(cleaned_parts)

    # 清理可能产生的多余连续换行
    final_parts = []
    final_pos_map = []
    prev_newlines = 0

    idx = 0
    while idx < len(cleaned):
        ch = cleaned[idx]
        if ch == '\n':
            prev_newlines += 1
            if prev_newlines <= 2:
                final_parts.append(ch)
                final_pos_map.append(pos_map[idx])
        else:
            prev_newlines = 0
            final_parts.append(ch)
            final_pos_map.append(pos_map[idx])
        idx += 1

    return ''.join(final_parts), final_pos_map


# 段落级前缀归一化正则 (用于分解前清理，不影响短语提纯)
_LINE_PREFIX_RE = re.compile(
    r'^(\s*)(?:\d+[)）]|[a-zA-Z][)）]|[-•●·]\s*)'
)


def _normalize_text_for_decomposition(text: str) -> tuple[str, list[int]]:
    """
    段落级前缀归一化：去除每行开头的列表标记(序号、bullet)，
    并合并因PDF换行导致的碎片化短行。

    仅影响分解过程，原文通过位置映射还原用于输出着色。

    归一化操作:
    1. 去除行首前缀: "-"/"•"/"●"/"·" + 可选空格, "1)"/"2）"/"a)" 等
    2. 合并短行: 非列表项短行(stripped后<15字符)与前一行合并
       (解决PDF换行导致结果与基准段落结构不一致的问题)

    Args:
        text: ASCII噪声清理后的文本

    Returns:
        (normalized_text, pos_map): pos_map[norm_i] = input_i
    """
    if not text:
        return text, []

    lines = text.split('\n')
    processed: list[tuple[str, list[int]]] = []  # (line_text, original_positions)

    offset = 0
    for i, raw_line in enumerate(lines):
        positions = list(range(offset, offset + len(raw_line)))

        stripped = raw_line.strip()
        if stripped:
            # 查找并去除行首前缀
            m = _LINE_PREFIX_RE.match(raw_line)
            if m:
                prefix_end = m.end()
                content = raw_line[prefix_end:]
                positions = positions[prefix_end:]
                processed.append((content, positions))
            else:
                processed.append((raw_line, positions))
        else:
            processed.append(('', positions))

        offset += len(raw_line) + 1  # +1 for \n

    # 构建归一化文本，合并短行
    _MAX_MERGE_LEN = 15  # stripped长度阈值：短于此值的非列表项行合并到前一行
    norm_chars: list[str] = []
    norm_pos_map: list[int] = []

    for line_text, line_pos in processed:
        stripped = line_text.strip()

        # 空行 → 段落分隔(保留\n)
        if not stripped:
            norm_chars.append('\n')
            norm_pos_map.append(line_pos[0] if line_pos else 0)
            continue

        # 判断是否为编号列表项起始行(不应被合并到前一行)
        is_list_start = bool(_LIST_ITEM_RE.match(stripped))

        # 合并条件: 前一行非空 + 当前行短 + 非列表项起始
        should_merge = (
            norm_chars and norm_chars[-1] != '\n'
            and len(stripped) < _MAX_MERGE_LEN
            and not is_list_start
        )

        if should_merge:
            # 直接拼接(不加\n)，消除PDF换行碎片化
            for j, ch in enumerate(stripped):
                stripped_start = len(line_text) - len(line_text.lstrip())
                orig_idx = line_pos[stripped_start + j] if (stripped_start + j) < len(line_pos) else line_pos[-1]
                norm_chars.append(ch)
                norm_pos_map.append(orig_idx)
        else:
            # 新行开始(若非首行则加\n分隔)
            if norm_chars and norm_chars[-1] != '\n':
                # 在前一行末尾插入\n(映射到前一行的最后位置)
                norm_chars.append('\n')
                norm_pos_map.append(norm_pos_map[-1] if norm_pos_map else 0)

            # 写入当前行内容(保留前导空格)
            for j, ch in enumerate(line_text):
                if j < len(line_pos):
                    norm_chars.append(ch)
                    norm_pos_map.append(line_pos[j])

    normalized = ''.join(norm_chars)
    return normalized, norm_pos_map


def _remap_block_positions(
    blocks: list[MicroBlock],
    pos_map: list[int],
) -> None:
    """将块的位置从清理后文本坐标映射回原文坐标(原地修改)"""
    for block in blocks:
        if block.start < len(pos_map):
            block.start = pos_map[block.start]
        if block.end > 0 and (block.end - 1) < len(pos_map):
            block.end = pos_map[block.end - 1] + 1
        if block.children:
            new_children = []
            for s, e, l in block.children:
                if s < len(pos_map) and (e - 1) < len(pos_map):
                    new_s = pos_map[s]
                    new_e = pos_map[e - 1] + 1
                    new_children.append((new_s, new_e, new_e - new_s))
            block.children = new_children


# ============================================================
# 两阶段匹配流水线
# ============================================================

def match_text_pair(
    downstream_text: str,
    upstream_text: str,
) -> MatchResult:
    """
    对一对(下游内容, 上游内容)进行微块级匹配

    优化后流程:
    1. 分解微块 + 结构化短语提纯
    2. 嵌入相似度(使用提纯文本编码)
    3. 匈牙利算法最优匹配(替代argmax贪心)
    4. 分类判定(GREEN/BLUE/BLACK)
    5. 子短语GREEN扫描(对BLACK块查找内部完全一致子串)
    6. 双向标色一致性校验

    Args:
        downstream_text: 下游需求文本
        upstream_text: 上游需求文本

    Returns:
        MatchResult: 匹配结果，包含着色文本运行
    """
    if not downstream_text or not upstream_text:
        return MatchResult(
            downstream_runs=[TextRun(downstream_text or '', MatchCategory.BLACK)],
            upstream_runs=[TextRun(upstream_text or '', MatchCategory.BLACK)],
            overall_category=MatchCategory.BLACK,
            downstream_text=downstream_text or '',
            upstream_text=upstream_text or '',
        )

    # Stage 0: 清理ASCII图表噪声(用于匹配，不影响输出)
    ds_clean, ds_pos_map = _strip_ascii_noise_for_matching(downstream_text)
    up_clean, up_pos_map = _strip_ascii_noise_for_matching(upstream_text)

    # 分解为微块(使用清理后文本以获得更好的短语边界)
    ds_blocks = decompose_text(ds_clean)
    up_blocks = decompose_text(up_clean)

    # 将块位置映射回原文坐标(用于生成TextRun时正确切片原文)
    # 保存cleaned-text坐标供后续子短语GREEN使用
    for b in ds_blocks:
        b._clean_start = b.start
    for b in up_blocks:
        b._clean_start = b.start
    _remap_block_positions(ds_blocks, ds_pos_map)
    _remap_block_positions(up_blocks, up_pos_map)

    if not ds_blocks or not up_blocks:
        return MatchResult(
            downstream_runs=[TextRun(downstream_text, MatchCategory.BLACK)],
            upstream_runs=[TextRun(upstream_text, MatchCategory.BLACK)],
            overall_category=MatchCategory.BLACK,
            downstream_text=downstream_text,
            upstream_text=upstream_text,
        )

    # 延迟导入模型(避免循环导入和提前加载)
    from models.embedding_model import encode, cosine_similarity_matrix
    from models.nli_model import predict_batch

    # Stage 1: 嵌入相似度 (使用提纯文本编码, Solution 4)
    ds_texts = [b.clean_text if b.clean_text else b.text for b in ds_blocks]
    up_texts = [b.clean_text if b.clean_text else b.text for b in up_blocks]

    ds_embeddings = encode(ds_texts)
    up_embeddings = encode(up_texts)
    sim_matrix = cosine_similarity_matrix(ds_embeddings, up_embeddings)

    # Stage 2: 最优匹配 (Solution 3 - 匈牙利算法替代argmax贪心)
    ds_matches, up_matches = _optimal_matching(sim_matrix)

    # Stage 2.5: 预热NLI缓存
    # _classify_block 内部对每个块单独调一次 NLI, 逐条前向传播极慢。
    # 这里先把两个方向上所有可能走到 NLI 的文本对收集起来做一次批量推理,
    # 结果进入 NLI 缓存; 随后 _classify_block 的单条调用全部命中缓存。
    # 判定逻辑与取值完全不变, 只是把多次小前向合并成一次大前向。
    _prefetch_nli(ds_blocks, up_blocks, sim_matrix, ds_matches, up_matches, predict_batch)

    # Stage 3: 分类判定
    # 下游→上游匹配
    for i, ds_block in enumerate(ds_blocks):
        best_j = ds_matches.get(i, int(sim_matrix[i].argmax()))
        best_sim = float(sim_matrix[i][best_j])
        ds_block.category = _classify_block(
            ds_block.text, up_blocks[best_j].text,
            best_sim, predict_batch
        )

    # 上游→下游匹配(反向)
    for j, up_block in enumerate(up_blocks):
        best_i = up_matches.get(j, int(sim_matrix[:, j].argmax()))
        best_sim = float(sim_matrix[best_i][j])
        up_block.category = _classify_block(
            up_block.text, ds_blocks[best_i].text,
            best_sim, predict_batch
        )

    # Stage 4: 子短语GREEN扫描 (Solution 1)
    _apply_subphrase_green(ds_blocks, up_blocks, sim_matrix, ds_pos_map)
    _apply_subphrase_green(up_blocks, ds_blocks, sim_matrix.T, up_pos_map)

    # Stage 5: 双向标色一致性校验 (Solution 6)
    _ensure_bidirectional_consistency(ds_blocks, up_blocks, sim_matrix, ds_matches, up_matches)

    # 构建TextRun列表(使用原文切片保真重建)
    ds_runs = _blocks_to_runs(ds_blocks, downstream_text)
    up_runs = _blocks_to_runs(up_blocks, upstream_text)

    # Stage 6: TextRun级对称着色校验
    _symmetrize_text_runs(ds_runs, up_runs)
    _symmetrize_text_runs(up_runs, ds_runs)

    # 整体匹配类别 = 所有微块中的最高类别
    all_categories = [b.category for b in ds_blocks] + [b.category for b in up_blocks]
    overall = max(all_categories, key=lambda c: c.priority)

    return MatchResult(
        downstream_runs=ds_runs,
        upstream_runs=up_runs,
        overall_category=overall,
        downstream_text=downstream_text,
        upstream_text=upstream_text,
    )


# ============================================================
# 最优匹配 (Solution 3)
# ============================================================

def _optimal_matching(sim_matrix: np.ndarray) -> tuple[dict, dict]:
    """
    基于相似度矩阵计算最优匹配(匈牙利算法)

    返回:
        ds_matches: {ds_idx: up_idx} 下游→上游的最优匹配
        up_matches: {up_idx: ds_idx} 上游→下游的最优匹配
    """
    M, N = sim_matrix.shape
    if M == 0 or N == 0:
        return {}, {}

    # 约束: 相似度 < UNMATCHED_THRESHOLD 的匹配对排除
    # 设其代价为极大值
    cost = 1.0 - sim_matrix.copy()
    # 将低于门槛的位置设为极大代价(不会被选中)
    mask = sim_matrix < EMBEDDING_UNMATCHED_THRESHOLD
    cost[mask] = 100.0

    try:
        from scipy.optimize import linear_sum_assignment
        row_ind, col_ind = linear_sum_assignment(cost)
        ds_matches = {}
        up_matches = {}
        for r, c in zip(row_ind, col_ind):
            if sim_matrix[r, c] >= EMBEDDING_UNMATCHED_THRESHOLD:
                ds_matches[int(r)] = int(c)
                up_matches[int(c)] = int(r)
        return ds_matches, up_matches
    except ImportError:
        # 回退: 约束性贪心(避免多对一)
        return _constrained_greedy_matching(sim_matrix)


def _constrained_greedy_matching(sim_matrix: np.ndarray) -> tuple[dict, dict]:
    """
    约束性贪心匹配: 按相似度从高到低贪心选择，
    确保每个上游块最多被一个下游块匹配
    """
    M, N = sim_matrix.shape
    ds_matches = {}
    up_matches = {}
    used_up = set()
    used_ds = set()

    # 构建所有(ds, up, sim)三元组并按sim降序排序
    pairs = []
    for i in range(M):
        for j in range(N):
            if sim_matrix[i, j] >= EMBEDDING_UNMATCHED_THRESHOLD:
                pairs.append((float(sim_matrix[i, j]), i, j))
    pairs.sort(reverse=True)

    for sim, i, j in pairs:
        if i not in used_ds and j not in used_up:
            ds_matches[i] = j
            up_matches[j] = i
            used_ds.add(i)
            used_up.add(j)

    return ds_matches, up_matches


# ============================================================
# 子短语GREEN扫描 (Solution 1)
# ============================================================

# 子短语GREEN最小长度 (4字符，bigram Jaccard交叉验证防止误匹配)
_MIN_GREEN_SUBSTR_LEN = 4
# 每个BLACK块最多允许的GREEN子串数量
_MAX_GREEN_SUBSTRS_PER_BLOCK = 3
# 子短语GREEN扫描的最低块级嵌入相似度(低于此不扫描，避免跨语义上下文匹配)
_MIN_BLOCK_SIM_FOR_SUBPHRASE = 0.45
# 双向一致性中将块提升为GREEN所需的最低匹配相似度
# 防止大块文本因小块精确匹配被整体标绿(问题3: 下游标绿但上游不存在)
# 优化4: 从0.92降至0.85, 提升上下游着色一致性
_GREEN_PROMOTE_SIM = 0.85


def _apply_subphrase_green(
    blocks: list[MicroBlock],
    other_blocks: list[MicroBlock],
    sim_matrix: np.ndarray,
    pos_map: list[int] = None,
) -> None:
    """
    对BLACK微块进行子短语GREEN扫描

    当短语级别的匹配为BLACK时，在短语内部查找与上游文本
    完全一致的子串，将其标为GREEN。

    优化后的保守策略:
    - 仅当块级嵌入相似度 >= 0.60 时才扫描(避免跨语义上下文)
    - 仅使用原始文本(不用提纯文本，避免位置映射错误)
    - 过滤掉被更长子串包含的短子串
    - 每块最多保留3个最长非重叠GREEN子串

    算法:
    1. 对每个BLACK块，检查最佳候选的嵌入相似度
    2. 在BLACK块中查找与候选块的最长公共子串(≥8字符)
    3. 选择最优非重叠子串集合
    4. 将符合条件的子串标为GREEN(修改块的category)
    
    Args:
        blocks: 待处理的微块列表
        other_blocks: 另一侧的微块列表
        sim_matrix: 相似度矩阵
        pos_map: 位置映射表(cleaned-text坐标 → original-text坐标)，
                 用于将children位置从块文本本地坐标转换为原文坐标
    """
    if not blocks or not other_blocks:
        return

    for i, block in enumerate(blocks):
        if block.category != MatchCategory.BLACK:
            continue
        if len(block.text) < _MIN_GREEN_SUBSTR_LEN:
            continue

        # 获取此块的相似度行
        if i < sim_matrix.shape[0]:
            row = sim_matrix[i]
        else:
            continue

        # 找到最佳上游候选(扩大到5个以覆盖更多潜在匹配)
        top_indices = np.argsort(row)[::-1][:5]

        # 块级上下文相似度过滤: 最佳候选的相似度必须 >= 阈值
        best_sim = float(row[top_indices[0]])
        if best_sim < _MIN_BLOCK_SIM_FOR_SUBPHRASE:
            continue

        # 收集所有GREEN子串候选 (仅用原始文本)
        green_substrings = []  # [(start, end, length)]

        for j_idx in top_indices:
            j = int(j_idx)
            if row[j] < _MIN_BLOCK_SIM_FOR_SUBPHRASE:
                continue
            other_text = other_blocks[j].text
            if not other_text or len(other_text) < _MIN_GREEN_SUBSTR_LEN:
                continue

            # 查找最长公共子串 (仅用原始文本，避免位置映射偏差)
            found = _find_green_substrings(block.text, other_text)
            green_substrings.extend(found)

        if green_substrings:
            # 去重: 移除被更长子串完全包含的短子串
            green_substrings.sort(key=lambda x: x[2], reverse=True)
            filtered = []
            for s, e, l in green_substrings:
                contained = False
                for fs, fe, fl in filtered:
                    if s >= fs and e <= fe:
                        contained = True
                        break
                if not contained:
                    filtered.append((s, e, l))

            # 选择最优的非重叠子串集合(贪心: 按长度降序, 最多_MAX_GREEN_SUBSTRS_PER_BLOCK个)
            selected = _select_non_overlapping(filtered)
            if len(selected) > _MAX_GREEN_SUBSTRS_PER_BLOCK:
                selected = selected[:_MAX_GREEN_SUBSTRS_PER_BLOCK]

            if selected:
                # 验证: GREEN子串总长度应占块文本的合理比例(不超过90%)
                total_green_len = sum(e - s for s, e, _ in selected)
                if total_green_len <= len(block.text) * 0.90:
                    # 保存原始类别，用于_blocks_to_runs_subphrase中
                    # 子短语间隙使用原始类别着色而非总是BLACK
                    if not hasattr(block, '_orig_cat') or block._orig_cat is None:
                        block._orig_cat = block.category
                    # 坐标映射: children位置是相对于block.text(cleaned text)的本地坐标
                    # 需要转换为original-text坐标，以便_blocks_to_runs_subphrase正确切片
                    if pos_map is not None and hasattr(block, '_clean_start'):
                        remapped = []
                        for s, e, l in selected:
                            # 1. 转换为绝对cleaned-text坐标(使用 remapping 前保存的 _clean_start)
                            abs_clean_s = block._clean_start + s
                            abs_clean_e = block._clean_start + e
                            # 2. 通过pos_map映射到original-text坐标
                            if abs_clean_s < len(pos_map) and (abs_clean_e - 1) < len(pos_map):
                                new_s = pos_map[abs_clean_s]
                                new_e = pos_map[abs_clean_e - 1] + 1
                                remapped.append((new_s, new_e, new_e - new_s))
                        block.children = remapped
                    else:
                        # 无pos_map时保持原行为(位置可能不准确)
                        block.children = selected
                    block.category = MatchCategory.GREEN


# ============================================================
# 子短语GREEN公共子串查找
# ============================================================

def _find_green_substrings(text: str, other_text: str) -> list:
    norm_a = _normalize_for_char_compare(text)
    norm_b = _normalize_for_char_compare(other_text)
    if not norm_a or not norm_b or len(norm_a) < _MIN_GREEN_SUBSTR_LEN:
        return []
    # 长度护栏: 防止长块的 O(m·n) DP 退化(与 _lcs_ratio 的 200 字符截断口径一致)。
    # 匹配块为短语级(通常远短于200字符), 正常路径不受影响。
    _MAX_DP_LEN = 200
    if len(norm_a) > _MAX_DP_LEN or len(norm_b) > _MAX_DP_LEN:
        norm_a = norm_a[:_MAX_DP_LEN]
        norm_b = norm_b[:_MAX_DP_LEN]
    m, n = len(norm_a), len(norm_b)
    results = []
    seen = set()
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if norm_a[i-1] == norm_b[j-1]:
                dp[i][j] = dp[i-1][j-1] + 1
                length = dp[i][j]
                if length >= _MIN_GREEN_SUBSTR_LEN:
                    sub_start_norm = i - length
                    sub_text_norm = norm_a[sub_start_norm:i]
                    other_sub_norm = norm_b[j - length:j]
                    bigram_j = _char_bigram_jaccard(sub_text_norm, other_sub_norm)
                    if bigram_j >= 0.92:
                        search_key = sub_text_norm[:max(4, len(sub_text_norm)//2)]
                        pos = text.find(search_key)
                        if pos < 0:
                            pos = text.find(sub_text_norm)
                        if pos >= 0:
                            orig_start = pos
                            orig_end = pos + length
                            if orig_end <= len(text):
                                key = (orig_start, orig_end)
                                if key not in seen:
                                    seen.add(key)
                                    results.append((orig_start, orig_end, length))
            else:
                dp[i][j] = 0
    return results

def _select_non_overlapping(
    candidates: list[tuple[int, int, int]],
) -> list[tuple[int, int, int]]:
    """贪心选择非重叠子串(按长度降序)"""
    selected = []
    occupied = set()

    for start, end, length in candidates:
        # 检查是否与已选子串重叠
        overlap = False
        for pos in range(start, end):
            if pos in occupied:
                overlap = True
                break
        if not overlap:
            selected.append((start, end, length))
            for pos in range(start, end):
                occupied.add(pos)

    return selected


# ============================================================
# 双向标色一致性校验 (Solution 6)
# ============================================================

def _ensure_bidirectional_consistency(
    ds_blocks: list[MicroBlock],
    up_blocks: list[MicroBlock],
    sim_matrix: np.ndarray,
    ds_matches: dict = None,
    up_matches: dict = None,
) -> None:
    """
    确保双向标色一致性

    当下游块被标为GREEN/BLUE时，其对应的上游块不应为BLACK。
    反之亦然。取两者中较高的类别作为统一类别。

    使用匈牙利匹配结果确定块对应关系(比argmax更准确)。
    GREEN提升需高匹配置信度(_GREEN_PROMOTE_SIM), 避免假绿。
    """
    if not ds_blocks or not up_blocks:
        return

    def _promote(target: MicroBlock, source: MicroBlock, sim: float) -> None:
        """将target提升到source的类别(仅当source优先级更高)"""
        if source.category.priority <= target.category.priority:
            return
        # GREEN提升需高置信度, 避免假绿
        if source.category == MatchCategory.GREEN and sim < _GREEN_PROMOTE_SIM:
            return
        target.category = source.category

    # 下游→上游一致性 (使用匈牙利匹配结果)
    for i, ds_block in enumerate(ds_blocks):
        if ds_block.category == MatchCategory.BLACK:
            continue
        if i >= sim_matrix.shape[0]:
            continue
        # 优先使用匈牙利匹配结果, 回退到argmax
        if ds_matches and i in ds_matches:
            best_j = ds_matches[i]
        else:
            best_j = int(sim_matrix[i].argmax())
        if best_j < len(up_blocks) and sim_matrix[i][best_j] >= EMBEDDING_UNMATCHED_THRESHOLD:
            _promote(up_blocks[best_j], ds_block, float(sim_matrix[i][best_j]))

    # 上游→下游一致性 (使用匈牙利匹配结果)
    for j, up_block in enumerate(up_blocks):
        if up_block.category == MatchCategory.BLACK:
            continue
        if j >= sim_matrix.shape[1]:
            continue
        # 优先使用匈牙利匹配结果, 回退到argmax
        if up_matches and j in up_matches:
            best_i = up_matches[j]
        else:
            best_i = int(sim_matrix[:, j].argmax())
        if best_i < len(ds_blocks) and sim_matrix[best_i][j] >= EMBEDDING_UNMATCHED_THRESHOLD:
            _promote(ds_blocks[best_i], up_block, float(sim_matrix[best_i][j]))


def _prefetch_nli(
    ds_blocks: list[MicroBlock],
    up_blocks: list[MicroBlock],
    sim_matrix: np.ndarray,
    ds_matches: dict,
    up_matches: dict,
    predict_batch_fn,
) -> None:
    """
    把两个方向上所有可能触发 NLI 的文本对合并成一次批量推理, 预热结果缓存。

    _classify_block 只在 embedding_sim >= EMBEDDING_SEMANTIC_THRESHOLD 时才会
    调用 NLI, 因此这里按同一条件收集(取的是超集: 部分对可能提前返回 GREEN 而
    用不到)。多算的部分只是进了缓存, 不参与任何判定, 结果与逐条调用完全一致。
    """
    if not ds_blocks or not up_blocks:
        return

    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _collect(text_a: str, text_b: str, sim: float) -> None:
        if sim < EMBEDDING_SEMANTIC_THRESHOLD:
            return
        key = (text_a, text_b)
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    for i, ds_block in enumerate(ds_blocks):
        best_j = ds_matches.get(i, int(sim_matrix[i].argmax()))
        _collect(ds_block.text, up_blocks[best_j].text, float(sim_matrix[i][best_j]))

    for j, up_block in enumerate(up_blocks):
        best_i = up_matches.get(j, int(sim_matrix[:, j].argmax()))
        _collect(up_block.text, ds_blocks[best_i].text, float(sim_matrix[best_i][j]))

    if pairs:
        predict_batch_fn(pairs)


def _classify_block(
    text_a: str,
    text_b: str,
    embedding_sim: float,
    predict_batch_fn,
) -> MatchCategory:
    """
    根据嵌入相似度和字符级/NLI验证，分类单个微块

    分类策略(与《追踪设计说明》§7.4 判定树一致):
    - 最低相似度门槛: 嵌入<0.50 → 直接BLACK
    - Stage 2a (GREEN): 嵌入>=0.95 + (bigram>=0.92 或 char_set>=0.88且LCS>=0.80) + 长度比合理
    - Stage 2b (BLUE): 嵌入>=0.70 + NLI蕴含>=0.85 + char_set>=0.50
      (或 NLI中立>=0.80 + 嵌入>=0.80 + char_set>=0.50)
    - Stage 2c (BLUE fallback): 嵌入>=0.85 + (LCS>=0.55 或 char_set>=0.60), 无需NLI
    - Stage 2d (BLUE): 嵌入>=0.75 + char_set>=0.55 + LCS>=0.45
    - 其余 → BLACK
    """
    # 最低相似度门槛: 低于此直接判BLACK
    if embedding_sim < EMBEDDING_UNMATCHED_THRESHOLD:
        return MatchCategory.BLACK

    if embedding_sim >= EMBEDDING_EXACT_THRESHOLD:
        # Stage 2a: 精确匹配验证 (双重路径)
        # 路径1: bigram Jaccard (对完全一致最严格)
        # 路径2: 字符集Jaccard + LCS比率 (容忍后缀差异)
        bigram_j = _char_bigram_jaccard(text_a, text_b)
        is_exact = bigram_j >= EXACT_CHAR_THRESHOLD

        if not is_exact:
            # 路径2: 字符集Jaccard + LCS
            char_j = _char_set_jaccard(text_a, text_b)
            lcs_r = _lcs_ratio(text_a, text_b)
            if char_j >= 0.88 and lcs_r >= 0.80:
                is_exact = True

        if is_exact:
            # 额外检查: 长度比合理(避免长文本匹配短文本的子串)
            norm_a = _normalize_for_char_compare(text_a)
            norm_b = _normalize_for_char_compare(text_b)
            len_ratio = min(len(norm_a), len(norm_b)) / max(len(norm_a), len(norm_b), 1)
            if len_ratio >= 0.70:
                return MatchCategory.GREEN
            # 长度差异太大 → 降级到BLUE验证

    # Stage 2a-fast: 短文本完全一致快速通道
    # 对于10字符以内的短语，若两侧原始文本完全一致，直接判GREEN
    # 绕过嵌入阈值和bigram门槛，解决短语分解粒度差异导致的漏匹配
    if len(text_a) <= 10 and len(text_b) <= 10 and text_a == text_b:
        return MatchCategory.GREEN

    # Stage 2a-fallback: 归一化后文本完全一致 → GREEN
    # 处理嵌入模型对微小后缀差异(如"功能")过度敏感的情况
    # 注意: 需要足够长的文本 + 长度比合理, 避免短短语子串误匹配
    if embedding_sim >= 0.85:
        norm_a = _normalize_for_char_compare(text_a)
        norm_b = _normalize_for_char_compare(text_b)
        if norm_a == norm_b and len(norm_a) >= 4:
            # 额外检查: 原始文本长度比合理(避免长文本匹配短子串)
            raw_len_ratio = min(len(text_a), len(text_b)) / max(len(text_a), len(text_b), 1)
            if raw_len_ratio >= 0.50:
                return MatchCategory.GREEN

    if embedding_sim >= EMBEDDING_SEMANTIC_THRESHOLD:
        # Stage 2b: NLI语义验证
        nli_results = predict_batch_fn([(text_a, text_b)])
        probs = nli_results[0]

        entailment_p = probs.get('ENTAILMENT', 0.0)
        neutral_p = probs.get('NEUTRAL', 0.0)
        contradiction_p = probs.get('CONTRADICTION', 0.0)

        # 高矛盾 → BLACK (提高阈值，减少误杀)
        if contradiction_p >= 0.80:
            return MatchCategory.BLACK

        # 高蕴含概率 → BLUE
        if entailment_p >= NLI_ENTAILMENT_THRESHOLD:
            char_j = _char_set_jaccard(text_a, text_b)
            if char_j >= 0.50:
                return MatchCategory.BLUE

        # 高NLI中立 + 高嵌入 → BLUE
        if neutral_p >= 0.8 and embedding_sim >= NLI_NEUTRAL_EMBEDDING_THRESHOLD:
            char_j = _char_set_jaccard(text_a, text_b)
            if char_j >= 0.50:
                return MatchCategory.BLUE

        # NLI不确定但文本重叠显著 → 降级判定但不直接BLACK
        # 落入下方的Stage 2c回退路径

    # Stage 2c: 高嵌入回退BLUE (无需NLI, 基于文本重叠度)
    # 当嵌入相似度很高(>=0.85)且文本有显著重叠(LCS>=0.55)时，标为BLUE
    if embedding_sim >= 0.85:
        lcs_r = _lcs_ratio(text_a, text_b)
        if lcs_r >= 0.55:
            return MatchCategory.BLUE
        # 字符集重叠也是一种信号
        char_j = _char_set_jaccard(text_a, text_b)
        if char_j >= 0.60:
            return MatchCategory.BLUE

    # Stage 2d: 中等嵌入 + 高字符重叠 → BLUE
    # 处理上下游短语分解粒度不同导致的嵌入稀释(如34字符短语 vs 58字符短语)
    if embedding_sim >= 0.75:
        char_j = _char_set_jaccard(text_a, text_b)
        if char_j >= 0.55:
            lcs_r = _lcs_ratio(text_a, text_b)
            if lcs_r >= 0.45:
                return MatchCategory.BLUE

    return MatchCategory.BLACK



def _symmetrize_text_runs(target_runs, reference_runs):
    """
    对称化TextRun着色: 将reference_runs中的GREEN/BLUE子串同步到target_runs

    优化4: 使用归一化匹配替代精确匹配, 处理全角/半角、空格、标点差异
    优化5: 同时同步BLUE子串, 确保上下游语义匹配标色一致
    """
    if not target_runs or not reference_runs:
        return

    def _collect_texts(runs, category, min_len):
        """收集指定类别的文本"""
        texts = []
        for run in runs:
            if run.category == category and run.text.strip():
                t = run.text.strip()
                if len(t) >= min_len:
                    texts.append(t)
        return texts

    def _find_matches(text, ref_texts, ref_norms, min_len):
        """在text中查找ref_texts的子串匹配"""
        matches = []
        # 策略1: 精确子串匹配
        for rt in ref_texts:
            pos = text.find(rt)
            while pos >= 0:
                matches.append((pos, pos + len(rt)))
                pos = text.find(rt, pos + 1)
        # 策略2: 归一化匹配
        if not matches and ref_norms:
            text_norm = _normalize_for_char_compare(text)
            if len(text_norm) >= min_len:
                for rt_orig, rt_norm in ref_norms:
                    pos = text_norm.find(rt_norm)
                    while pos >= 0:
                        ratio = len(text) / max(len(text_norm), 1)
                        orig_start = int(pos * ratio)
                        orig_end = min(int((pos + len(rt_norm)) * ratio), len(text))
                        if orig_end > orig_start:
                            matches.append((orig_start, orig_end))
                        pos = text_norm.find(rt_norm, pos + 1)
        return matches

    def _apply_matches(text, matches, category):
        """将匹配区域标记为指定类别"""
        matches.sort()
        selected = []
        last_end = 0
        for s, e in matches:
            if s >= last_end:
                selected.append((s, e))
                last_end = e
        if not selected:
            return [TextRun(text=text, category=MatchCategory.BLACK)]
        result = []
        cursor = 0
        for s, e in selected:
            if s > cursor:
                result.append(TextRun(text=text[cursor:s], category=MatchCategory.BLACK))
            result.append(TextRun(text=text[s:e], category=category))
            cursor = e
        if cursor < len(text):
            result.append(TextRun(text=text[cursor:], category=MatchCategory.BLACK))
        return result

    # === 同步GREEN ===
    green_texts = _collect_texts(reference_runs, MatchCategory.GREEN, _MIN_GREEN_SUBSTR_LEN)
    green_norms = [(gt, _normalize_for_char_compare(gt)) for gt in green_texts
                   if len(_normalize_for_char_compare(gt)) >= _MIN_GREEN_SUBSTR_LEN]

    new_runs = []
    for run in target_runs:
        if run.category != MatchCategory.BLACK or not run.text.strip():
            new_runs.append(run)
            continue
        text = run.text
        if len(text) < _MIN_GREEN_SUBSTR_LEN:
            new_runs.append(run)
            continue
        matches = _find_matches(text, green_texts, green_norms, _MIN_GREEN_SUBSTR_LEN)
        if not matches:
            new_runs.append(run)
        else:
            new_runs.extend(_apply_matches(text, matches, MatchCategory.GREEN))

    # === 同步BLUE ===
    blue_texts = _collect_texts(reference_runs, MatchCategory.BLUE, _MIN_GREEN_SUBSTR_LEN)
    blue_norms = [(bt, _normalize_for_char_compare(bt)) for bt in blue_texts
                  if len(_normalize_for_char_compare(bt)) >= _MIN_GREEN_SUBSTR_LEN]

    if blue_texts:
        final_runs = []
        for run in new_runs:
            if run.category != MatchCategory.BLACK or not run.text.strip():
                final_runs.append(run)
                continue
            text = run.text
            if len(text) < _MIN_GREEN_SUBSTR_LEN:
                final_runs.append(run)
                continue
            matches = _find_matches(text, blue_texts, blue_norms, _MIN_GREEN_SUBSTR_LEN)
            if not matches:
                final_runs.append(run)
            else:
                final_runs.extend(_apply_matches(text, matches, MatchCategory.BLUE))
        new_runs = final_runs

    target_runs.clear()
    target_runs.extend(new_runs)

def _blocks_to_runs(blocks: list[MicroBlock], original_text: str = '') -> list[TextRun]:
    """
    将微块列表合并为TextRun列表
    相邻同色微块合并为一个run
    使用原文切片保真重建，避免逗号拼接破坏原文
    当颜色变化时，将run间的空白字符(包括换行)归入前一个run

    支持子短语GREEN: 当块有children(子串位置)时，
    生成细粒度的GREEN/BLACK交替TextRun
    """
    if not blocks:
        return []

    # 检查是否有子短语GREEN块(需要细粒度处理)
    has_subphrase = any(
        b.children and (b.category == MatchCategory.GREEN or b.category == MatchCategory.BLUE)
        for b in blocks
    )

    if has_subphrase:
        return _blocks_to_runs_subphrase(blocks, original_text)

    # 标准处理(无子短语GREEN)
    runs = []
    run_start_idx = 0
    current_cat = blocks[0].category

    for i in range(1, len(blocks)):
        if blocks[i].category != current_cat:
            # 颜色变化 → 输出当前run
            start_b = blocks[run_start_idx]
            end_b = blocks[i - 1]

            # 将run间的空白(包括\n)归入当前run，避免丢失换行
            gap_end = blocks[i].start
            if original_text and gap_end > end_b.end:
                gap = original_text[end_b.end:gap_end]
                # 仅吸收单个换行符(及其前面的空白)，避免引入多余空行
                if gap == '\n':
                    run_text = original_text[start_b.start:gap_end]
                elif gap.endswith('\n') and gap.strip() == '':
                    # 空白+单个换行 → 吸收
                    run_text = original_text[start_b.start:gap_end]
                else:
                    # 多个换行或非纯空白 → 仅吸收一个换行
                    nl_pos = original_text.find('\n', end_b.end, gap_end)
                    if nl_pos >= 0 and nl_pos == end_b.end:
                        run_text = original_text[start_b.start:nl_pos + 1]
                    else:
                        run_text = _extract_run_text(start_b, end_b, original_text)
            else:
                run_text = _extract_run_text(start_b, end_b, original_text)

            runs.append(TextRun(text=run_text, category=current_cat))
            run_start_idx = i
            current_cat = blocks[i].category

    # 输出最后一个run
    start_b = blocks[run_start_idx]
    end_b = blocks[-1]
    run_text = _extract_run_text(start_b, end_b, original_text)
    # 如果原文在最后一个block之后还有内容(如尾部空白)，也包含进来
    if original_text and len(original_text) > end_b.end:
        trailing = original_text[end_b.end:]
        if trailing.strip() == '':  # 仅空白
            run_text += trailing

    runs.append(TextRun(text=run_text, category=current_cat))
    return runs


def _blocks_to_runs_subphrase(
    blocks: list[MicroBlock],
    original_text: str = '',
) -> list[TextRun]:
    """
    支持子短语GREEN的TextRun生成

    对于有children的GREEN块，将其分解为:
    BLACK(前缀) + GREEN(子串1) + BLACK(间隔) + GREEN(子串2) + ... + BLACK(后缀)

    安全性: 检测块位置重叠时回退到标准处理，防止文本重复
    """
    if not blocks:
        return []

    # 验证块位置: 检测重叠
    sorted_blocks = sorted(blocks, key=lambda b: b.start)
    has_overlap = False
    for idx in range(1, len(sorted_blocks)):
        if sorted_blocks[idx].start < sorted_blocks[idx - 1].end:
            has_overlap = True
            break

    if has_overlap and original_text:
        # 块位置有重叠 → 回退到标准处理(忽略子短语children)
        # 将每个块视为整体，按其category着色
        return _blocks_to_runs_standard_safe(blocks, original_text)

    # 标准子短语处理
    runs = []
    prev_end = 0

    for block in blocks:
        block_start = block.start
        block_end = block.end

        # 填充块之间的空白
        if original_text and block_start > prev_end:
            gap_text = original_text[prev_end:block_start]
            if gap_text:
                # 将空白归入前一个同色run或新建BLACK run
                if runs and runs[-1].category == MatchCategory.BLACK:
                    runs[-1] = TextRun(
                        text=runs[-1].text + gap_text,
                        category=MatchCategory.BLACK,
                    )
                else:
                    runs.append(TextRun(text=gap_text, category=MatchCategory.BLACK))

        if block.children and (block.category == MatchCategory.GREEN or block.category == MatchCategory.BLUE):
            # 子短语渲染: 生成细粒度runs
            children = sorted(block.children, key=lambda x: x[0])
            cursor = block_start  # 当前处理位置
            # 间隙使用原始类别(_orig_cat)，无_orig_cat时回退到BLACK
            gap_cat = getattr(block, '_orig_cat', None) or MatchCategory.BLACK

            for child_start, child_end, child_len in children:
                # 确保child位置在block范围内
                abs_child_start = child_start
                abs_child_end = child_end

                # GREEN子串前的间隙文本(使用原始类别着色)
                if abs_child_start > cursor:
                    pre_text = original_text[cursor:abs_child_start] if original_text else ''
                    if pre_text:
                        if runs and runs[-1].category == gap_cat:
                            runs[-1] = TextRun(
                                text=runs[-1].text + pre_text,
                                category=gap_cat,
                            )
                        else:
                            runs.append(TextRun(text=pre_text, category=gap_cat))

                # GREEN子串
                green_text = original_text[abs_child_start:abs_child_end] if original_text else ''
                if green_text:
                    runs.append(TextRun(text=green_text, category=MatchCategory.GREEN))
                cursor = max(cursor, abs_child_end)

            # GREEN子串后的间隙文本(块尾部，使用原始类别着色)
            if cursor < block_end:
                post_text = original_text[cursor:block_end] if original_text else ''
                if post_text:
                    runs.append(TextRun(text=post_text, category=gap_cat))
        else:
            # 标准块: 整体一个颜色
            run_text = original_text[block.start:block.end] if original_text else block.text
            if runs and runs[-1].category == block.category:
                # 合并同色run
                runs[-1] = TextRun(
                    text=runs[-1].text + run_text,
                    category=block.category,
                )
            else:
                runs.append(TextRun(text=run_text, category=block.category))

        prev_end = max(prev_end, block_end)

    # 处理尾部空白
    if original_text and len(original_text) > prev_end:
        trailing = original_text[prev_end:]
        if trailing.strip() == '':
            if runs:
                runs[-1] = TextRun(
                    text=runs[-1].text + trailing,
                    category=runs[-1].category,
                )

    # 关键修复（2026-08-19, DCS-SyRS005/3.2.2.2 标绿内容合并为一行问题）：
    # 子短语GREEN会把换行符拆到独立的BLACK run（如 <t>\n</t>、<t>\n-</t>），
    # WPS/Excel 渲染富文本时对"纯换行run"的换行可能失效，导致标绿的多行
    # 内容被合并成一行。此处把每个 run 开头的前导换行符剥离并合并到前一个
    # run 的末尾，使换行符附着在文本 run 内部（与整段单run时的行为一致）。
    merged_runs = []
    for run in runs:
        if merged_runs and run.text.startswith('\n'):
            n = 0
            while n < len(run.text) and run.text[n] == '\n':
                n += 1
            leading = '\n' * n
            rest = run.text[n:]
            merged_runs[-1] = TextRun(
                text=merged_runs[-1].text + leading,
                category=merged_runs[-1].category,
            )
            if rest:
                merged_runs.append(TextRun(text=rest, category=run.category))
        else:
            merged_runs.append(run)
    return merged_runs


def _blocks_to_runs_standard_safe(
    blocks: list[MicroBlock],
    original_text: str,
) -> list[TextRun]:
    """
    安全的标准TextRun生成(忽略子短语children)

    当检测到块位置重叠时使用此函数，按块在原文中的位置顺序
    逐个输出，跳过重叠部分以防止文本重复。
    """
    if not blocks:
        return []

    # 按位置排序
    sorted_blocks = sorted(blocks, key=lambda b: (b.start, b.end))

    runs = []
    prev_end = 0

    for block in sorted_blocks:
        # 跳过完全重叠的块
        if block.start < prev_end:
            # 部分重叠: 仅输出非重叠部分
            actual_start = prev_end
            if actual_start >= block.end:
                continue  # 完全被前一个块覆盖
        else:
            actual_start = block.start
            # 填充空白
            if actual_start > prev_end:
                gap = original_text[prev_end:actual_start]
                if gap:
                    if runs and runs[-1].category == MatchCategory.BLACK:
                        runs[-1] = TextRun(
                            text=runs[-1].text + gap,
                            category=MatchCategory.BLACK,
                        )
                    else:
                        runs.append(TextRun(text=gap, category=MatchCategory.BLACK))

        run_text = original_text[actual_start:block.end]
        if runs and runs[-1].category == block.category:
            runs[-1] = TextRun(
                text=runs[-1].text + run_text,
                category=block.category,
            )
        else:
            runs.append(TextRun(text=run_text, category=block.category))

        prev_end = max(prev_end, block.end)

    # 尾部空白
    if len(original_text) > prev_end:
        trailing = original_text[prev_end:]
        if trailing.strip() == '':
            if runs:
                runs[-1] = TextRun(
                    text=runs[-1].text + trailing,
                    category=runs[-1].category,
                )

    return runs


def _extract_run_text(start_block: MicroBlock, end_block: MicroBlock, original_text: str) -> str:
    """从原文中切片提取run的真实文本"""
    if original_text and end_block.end > start_block.start and end_block.end <= len(original_text):
        return original_text[start_block.start:end_block.end]
    # 兜底: 用微块文本直接拼接
    return start_block.text if start_block is end_block else start_block.text + end_block.text


def _warmup_models(pairs: list[tuple[str, str]]) -> None:
    """
    在逐行匹配之前, 对整个矩阵做一次全局批量推理来预热模型缓存。

    单行内的批量只有几个块, 前向传播批次小、并行度低; 把所有行的块合并成一批,
    能显著提升 CPU/GPU 的吞吐。预热只是把结果写进缓存, 逐行匹配时全部命中,
    判定逻辑与取值不变。任何异常都直接忽略, 退回原本的逐行推理。
    """
    try:
        from models.embedding_model import encode, cosine_similarity_matrix
        from models.nli_model import predict_batch
    except Exception:
        return

    prepared = []
    all_texts: list[str] = []

    for ds_text, up_text in pairs:
        if not ds_text or not up_text:
            continue
        ds_clean, _ = _strip_ascii_noise_for_matching(ds_text)
        up_clean, _ = _strip_ascii_noise_for_matching(up_text)
        ds_blocks = decompose_text(ds_clean)
        up_blocks = decompose_text(up_clean)
        if not ds_blocks or not up_blocks:
            continue
        ds_texts = [b.clean_text if b.clean_text else b.text for b in ds_blocks]
        up_texts = [b.clean_text if b.clean_text else b.text for b in up_blocks]
        all_texts.extend(ds_texts)
        all_texts.extend(up_texts)
        prepared.append((ds_blocks, up_blocks, ds_texts, up_texts))

    if not all_texts:
        return

    # 1) 全矩阵一次性编码(内部会自动去重, 重复文本只算一次)
    encode(all_texts)

    # 2) 用已缓存的向量算出各行的匹配, 汇总所有可能触发 NLI 的文本对
    nli_pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ds_blocks, up_blocks, ds_texts, up_texts in prepared:
        sim_matrix = cosine_similarity_matrix(encode(ds_texts), encode(up_texts))
        ds_matches, up_matches = _optimal_matching(sim_matrix)

        for i, ds_block in enumerate(ds_blocks):
            j = ds_matches.get(i, int(sim_matrix[i].argmax()))
            if float(sim_matrix[i][j]) >= EMBEDDING_SEMANTIC_THRESHOLD:
                key = (ds_block.text, up_blocks[j].text)
                if key not in seen:
                    seen.add(key)
                    nli_pairs.append(key)

        for j, up_block in enumerate(up_blocks):
            i = up_matches.get(j, int(sim_matrix[:, j].argmax()))
            if float(sim_matrix[i][j]) >= EMBEDDING_SEMANTIC_THRESHOLD:
                key = (up_block.text, ds_blocks[i].text)
                if key not in seen:
                    seen.add(key)
                    nli_pairs.append(key)

    # 3) 全矩阵一次性 NLI 推理
    if nli_pairs:
        predict_batch(nli_pairs)


def verify_matrix(matrix, progress_cb=None) -> None:
    """
    对整个追踪矩阵执行文本匹配验证
    结果直接写入每行的 match_result 字段

    单行匹配异常不中断整体(该行 match_result 置 None, 下游渲染按未匹配处理)。

    Args:
        matrix: TraceabilityMatrix 对象
        progress_cb: 可选进度回调 fn(done, total), 每完成一行调用一次
    """
    rows = matrix.rows
    total = len(rows)

    # 先做一次全矩阵批量推理预热缓存, 再逐行匹配(结果不变, 只为提速)
    try:
        _warmup_models([(r.downstream_content, r.upstream_content) for r in rows])
    except Exception as e:  # noqa: BLE001 — 预热失败退回逐行推理
        print(f'    [match] 模型预热失败(退回逐行推理): {type(e).__name__}: {e}')

    for idx, row in enumerate(rows):
        try:
            row.match_result = match_text_pair(
                row.downstream_content,
                row.upstream_content,
            )
        except Exception as e:  # noqa: BLE001 — 单行失败不拖垮整体验证
            print(f'    [match] 行{row.seq_number}({row.downstream_id})匹配失败: '
                  f'{type(e).__name__}: {e}')
            row.match_result = None
        if progress_cb is not None:
            try:
                progress_cb(idx + 1, total)
            except Exception:  # noqa: BLE001 — 回调异常不影响匹配
                pass
