"""L3 标题识别与章节层级树构建。

三步法（研究报告 3.4.2）：
1. 字体签名聚类：全文档统计 (font_name, size, bold) 频率 → 正文基线（众数）→ 标题候选层级。
2. 标题判定：多重条件 AND（签名命中 + 独占一行 + 短文本 + 不以句号结尾 + 编号模式 + 视觉缝）。
3. 层级栈归属：块流按阅读顺序遍历，用栈维护"当前打开的标题"，正文块挂到栈顶之下——
   从机制上杜绝"标题把下一节内容吸进来"（研究报告场景 C）。

编号模式（工程文档常见）：
   1 / 1.1 / 1.1.1 | 第X章 / 第X部分 / 第X节 | 附录A | A.1 | 1. 1、1) (1)
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

from pdfparser.models import Block

# 标题编号模式（行首）
_NUM_PATTERNS = [
    re.compile(r"^\d+(\.\d+){0,3}[\s、.．：:]\s*"),        # 1 / 1.1 / 1.1.1
    re.compile(r"^第[一二三四五六七八九十百\d零]+[章节部分篇讲]\s*"),  # 第X章
    re.compile(r"^附录\s*[A-Za-z\d]"),                      # 附录A
    re.compile(r"^[A-Z]\.\d+\s*"),                          # A.1
    re.compile(r"^\d+[.、)）]\s*"),                          # 1. 1、 1)
    re.compile(r"^\(\d+\)\s*"),                              # (1)
    re.compile(r"^[一二三四五六七八九十]+、"),                # 一、 二、
    re.compile(r"^[（(][一二三四五六七八九十]+[)）]"),        # （一） (一)
    re.compile(r"^[A-Z]\s*[.、)）]\s*"),                     # A. B.
]
_END_PUNCT = set("。；;，,、：:！？!?…—")


def _number_depth(text: str) -> Optional[int]:
    """从编号模式推断标题层级深度（返回 None 表示无编号无法推断）。"""
    t = text.strip()
    if re.match(r"^第[一二三四五六七八九十百\d零]+[章节部分篇讲]", t):
        return 1
    if re.match(r"^[一二三四五六七八九十]+、", t):
        return 2
    if re.match(r"^（[一二三四五六七八九十]+）", t):
        return 3
    if re.match(r"^\d+\.\d+\.\d+", t):
        return 5
    if re.match(r"^\d+\.\d+", t):
        return 4
    if re.match(r"^[A-Z]\.\d+", t):
        return 4
    # 单段编号 "1. xxx" "1、xxx" — 中文公文与"（一）"同级 L3
    if re.match(r"^\d+[.、)）]\s*\S", t):
        return 3
    if re.match(r"^附录\s*[A-Za-z\d]", t):
        return 1
    return None


def _is_body_cjk(body_sig) -> bool:
    """判断正文基线字体是否属于 CJK 字体族（中文文档特征）。"""
    if not body_sig:
        return False
    name = body_sig[0] or ""
    return any('\u4e00' <= ch <= '\u9fff' for ch in name) or any(
        k in name for k in ("GBK", "Song", "Hei", "Kai", "FZ", "ST", "Sim", "Fang", "GB"))


def _has_number_prefix(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(p.match(t) for p in _NUM_PATTERNS)


class HeadingAssigner:
    """标题识别 + 层级树构建。"""

    def __init__(self, max_heading_chars: int = 80,
                 visual_gap_ratio: float = 1.5):
        self.max_heading_chars = max_heading_chars
        self.visual_gap_ratio = visual_gap_ratio

    # ------------------------------------------------------------------
    def assign_levels(self, blocks: list[Block]) -> None:
        """对 text 块标注 level（0=正文，1..N=标题层级）。"""
        # 每页内容区（用于居中判定）：页面上全部文本块的范围
        page_areas: dict[int, tuple[float, float]] = {}
        for b in blocks:
            if b.type not in ("text", "heading"):
                continue
            x0, _, x1, _ = b.bbox
            if b.page not in page_areas:
                page_areas[b.page] = [x0, x1]
            else:
                page_areas[b.page][0] = min(page_areas[b.page][0], x0)
                page_areas[b.page][1] = max(page_areas[b.page][1], x1)

        sig_stats = self._font_signatures(blocks)
        body_sig, heading_sigs = self._body_and_heading_sigs(sig_stats)

        # 文本模式兜底：即使标题与正文同字体同字号（Office 模板导出），
        # 也依据"编号标题"文本模式识别（如 "3.3 系统架构设计要求"、"1.1.2 xxx"）。
        # 这是 Word/金山模板 PDF 的常见特征——标题样式不体现在字号/字体上。
        _numbered = re.compile(
            r"^\s*(\d+(\.\d+){1,3}|第[一二三四五六七八九十百\d零]+[章节部分篇讲]"
            r"|附录\s*[A-Za-z\d])\s*[、.．：: ]\s*\S")
        for i, b in enumerate(blocks):
            if b.type != "text":
                continue
            t = b.text.strip()
            if not heading_sigs and _numbered.match(t):
                # 无字体差异可用时，仅依据编号模式 + 短行
                if len(t) <= 60 and len(b.lines) <= 2 \
                        and t[-1] not in _END_PUNCT:
                    b.type = "heading"
                    b.level = 3
                continue
            if not heading_sigs:
                continue
            sig = b.font_sig or ("", 0.0, False)
            if sig not in heading_sigs:
                # 字体签名不命中时，用编号文本模式兜底
                if _numbered.match(t) and len(t) <= 60 and len(b.lines) <= 2 \
                        and t[-1] not in _END_PUNCT:
                    b.type = "heading"
                    b.level = 3
                else:
                    b.level = 0
                continue
            area = page_areas.get(b.page, (0, 1000))
            if not self._heading_like(b, blocks, i, body_sig, area):
                b.level = 0
                continue
            # 层级：始终以字号/字体签名 sig 为准（已对中文做了跨字体族不合并的修正）
            b.level = heading_sigs[sig]

        # 将判定为标题的块 type 改为 heading
        for b in blocks:
            if b.type == "text" and b.level > 0:
                b.type = "heading"

    # ------------------------------------------------------------------
    def _font_signatures(self, blocks: list[Block]) -> Counter:
        c: Counter = Counter()
        for b in blocks:
            if b.type in ("text", "heading"):
                sig = b.font_sig or ("", round(b.bbox[3] - b.bbox[1], 1), False)
                c[sig] += 1
        return c

    def _body_and_heading_sigs(self, stats: Counter):
        """正文基线 = 众数；标题 = 字号更大 或 加粗 或 字体族不同，
        且字号不小于正文、出现频率明显低（中文标题常用楷体/黑体而非加粗）。"""
        if not stats:
            return None, {}
        body_sig, body_cnt = stats.most_common(1)[0]
        _, body_size, body_bold = body_sig
        body_size = body_size or 0.0
        total = sum(stats.values())
        freq_limit = max(5, int(body_cnt * 0.5))  # 相对正文频率门槛
        # 中文文档：其他 CJK 标题字体（黑/楷/宋等）放宽频率门槛（条款标题可高频出现）
        _CJK_FONTS = ("GBK", "Song", "Hei", "Kai", "FZ", "ST", "Sim", "Fang", "GB")
        body_is_cjk = any('\u4e00' <= ch <= '\u9fff' for ch in body_sig[0]) \
            or any(k in body_sig[0] for k in _CJK_FONTS)
        candidates = []
        for sig, cnt in stats.items():
            name, size, bold = sig
            if sig == body_sig:
                continue
            if size < body_size - 2.0:
                continue  # 明显小于正文的排除（LaTeX 加粗标题允许比正文小 ~1pt）
            is_candidate = (size > body_size + 0.5) or (bold != body_bold) \
                or (name != body_sig[0])
            if not is_candidate:
                continue
            if size >= body_size + 1.5:
                candidates.append((sig, cnt))  # 显著大字号无条件保留
                continue
            # 近字号候选：中文文档中标题字体族放宽；否则严格频率门槛
            if body_is_cjk and name != body_sig[0] and any(
                    k in name for k in _CJK_FONTS):
                if cnt < max(10, body_cnt):
                    candidates.append((sig, cnt))
            elif cnt < freq_limit:
                candidates.append((sig, cnt))
        if not candidates:
            return body_sig, {}
        # 按显著性排序：字号降序，加粗优先；
        # 中文：罕见字体（黑体）排前 = 浅层；西文：罕见字体排后 = 深层
        if body_is_cjk:
            candidates.sort(key=lambda x: (-x[0][1], 0 if x[0][2] else 1, x[1]))
        else:
            candidates.sort(key=lambda x: (-x[0][1], 0 if x[0][2] else 1, -x[1]))
        level_map: dict[tuple, int] = {}
        levels: list[tuple] = []
        for sig, _ in candidates:
            # 西文：字号/加粗差异产生新层级；中文公文：跨字体族（黑/楷）也产生新层级
            if not levels or abs(sig[1] - levels[-1][1]) >= 1.0 or sig[2] != levels[-1][2]:
                same_level = False
            elif body_is_cjk and sig[0] != levels[-1][0]:
                same_level = False
            else:
                same_level = True
            if not same_level:
                if len(levels) >= 6:
                    break  # 层级上限
                levels.append(sig)
            level_map[sig] = len(levels)  # 同层 sig 都映射到同一级
        return body_sig, level_map

    # ------------------------------------------------------------------
    def _heading_like(self, block: Block, blocks: list[Block],
                      idx: int, body_sig, page_area=None) -> bool:
        """多重条件（含无编号标题的居中+大字号判据）。"""
        t = block.text.strip()
        # 0) 排除文档标识/URL 行（arXiv 头、DOI、链接）与作者行（arXiv 常用 ∗†‡ 上标）
        if t[:8].lower().startswith(("arxiv:", "doi:", "http")):
            return False
        if any(ch in t for ch in "∗†‡§"):
            return False
        # 1) 独占一行（允许 2 行：编号行 + 标题文字行，如 LaTeX "I.\\nINTRODUCTION"）
        if len(block.lines) not in (1, 2):
            return False
        if len(block.lines) == 2 and len(t) > self.max_heading_chars + 40:
            return False
        # 2) 短文本
        if len(t) > self.max_heading_chars:
            return False
        # 3) 不以句号/分号/连字符结尾（排除断行碎片）
        if t and (t[-1] in _END_PUNCT or t[-1] in "-–—"):
            return False
        # 4) 标题证据：编号 或 显著字号（≥正文+2，覆盖双栏左对齐标题）或 居中+更大
        numbered = _has_number_prefix(t)
        # 标题文本必须含 ≥2 个字母或汉字（过滤纯数字/纯编号行）
        alpha_n = sum(1 for ch in t if ch.isalnum() and not ch.isdigit())
        if alpha_n < 2:
            return False
        # 公式行排除：数学符号密集且非大字号（如 "1 + Nv"）
        if any(ch in t for ch in "=+×÷±√∑∫∞") and not (block.font_sig and
                block.font_sig[1] >= (body_sig[1] if body_sig else 0) + 2.0):
            return False
        centered = False
        if page_area is not None:
            x0, _, x1, _ = block.bbox
            area_x0, area_x1 = page_area
            area_w = max(area_x1 - area_x0, 1.0)
            center_dev = abs((x0 + x1) / 2 - (area_x0 + area_x1) / 2)
            centered = center_dev <= 0.08 * area_w
        cur_size = block.font_sig[1] if block.font_sig else 0
        body_size = body_sig[1] if body_sig else 0
        large_font = cur_size >= body_size + 2.0
        # LaTeX 全大写加粗标题（如 I. INTRODUCTION / THEORY OF ...）
        is_bold = bool(block.font_sig and block.font_sig[2])
        uppercase_bold = is_bold and t.isupper() and len(t) <= 60
        evidence = numbered or large_font or uppercase_bold \
            or (centered and cur_size > body_size)
        if not evidence:
            return False
        # 5) 与下一块的关系（软否决）：仅当"下一块字号更大且间距极小"时否决
        if idx + 1 < len(blocks):
            nxt = blocks[idx + 1]
            nxt_size = nxt.font_sig[1] if nxt.font_sig else 0
            if (nxt.page == block.page and len(nxt.text.strip()) >= 3
                    and nxt_size > cur_size and (nxt.y0 - block.y1) < 6.0):
                return False
        # 6) 排除"段落续行"：与上一块同字号、x0 相近、字体族相同
        #    （段落的一部分而非独立标题）。中文公文中同级标题可能紧接上级标题
        #    出现，x0 相同但字体族不同（如 FZHTK 黑体 vs FZKTK 楷体），此时应
        #    保留为独立标题。
        if idx > 0:
            prev = blocks[idx - 1]
            prev_size = prev.font_sig[1] if prev.font_sig else 0
            prev_name = (prev.font_sig[0] or "") if prev.font_sig else ""
            cur_name = (block.font_sig[0] or "") if block.font_sig else ""
            same_font_family = prev_name.split("-")[0] == cur_name.split("-")[0]
            if (prev.page == block.page
                    and prev.type in ("text", "paragraph", "heading")
                    and abs(prev_size - cur_size) <= 0.5
                    and same_font_family
                    and abs(prev.x0 - block.x0) < 12.0):
                return False
        return True

    # ------------------------------------------------------------------
    def build_tree(self, blocks: list[Block]) -> list[Block]:
        """层级栈归属：返回顶层块列表（标题下挂 children）。"""
        roots: list[Block] = []
        stack: list[Block] = []  # 打开的标题栈

        for b in blocks:
            if b.type in ("header_footer", "consumed"):
                continue  # 页眉页脚/表格吸收区不入文档树
            if b.type == "heading":
                while stack and stack[-1].level >= b.level:
                    stack.pop()
                stack.append(b)
                # 挂到父标题下
                if stack:
                    parent = stack[-2] if len(stack) >= 2 else None
                    if parent is not None:
                        parent.children.append(b)
                    else:
                        roots.append(b)
                else:
                    roots.append(b)
            else:
                parent = stack[-1] if stack else None
                if parent is not None:
                    parent.children.append(b)
                else:
                    roots.append(b)
        return roots

    # ------------------------------------------------------------------
    def outline(self, blocks: list[Block]) -> list[tuple[int, str, int]]:
        """从标题块生成大纲 [(level, text, page)]。"""
        out = []
        for b in blocks:
            if b.type == "heading":
                out.append((b.level, b.text.strip(), b.page))
        return out
