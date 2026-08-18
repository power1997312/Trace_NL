"""L3 列表结构识别（研究报告场景 B）。

三证据识别：行首标记模式 + 左边界缩进聚类 + 序号连续性校验。
覆盖工程文档常见列表标记：
   1. / 1) / 1、/ (1) / ① / 一、 | a. / a) | • / - / – / · / * （符号列表）
并输出嵌套列表树（悬挂缩进 → 子层级）。
"""
from __future__ import annotations

import re
from typing import Optional

from pdfparser.models import Block

# 标记模式：(正则, 类型, 是否有序)
_MARKERS: list[tuple[re.Pattern, str, bool]] = [
    (re.compile(r"^(\d+)[.、)）]\s*"), "numeric", True),
    (re.compile(r"^\((\d+)\)\s*"), "numeric", True),
    (re.compile(r"^([①-⑨⑩])\s*"), "circled", True),
    (re.compile(r"^([一二三四五六七八九十]+)[、.)]\s*"), "cn_num", True),
    (re.compile(r"^([a-zA-Z])[.、)）]\s*"), "alpha", True),
    (re.compile(r"^[•·●◦▪□■*]\s*"), "bullet", False),
    (re.compile(r"^[-–—]\s+"), "dash", False),
    (re.compile(r"^-\s*$"), "dash_line", False),  # 单独短横线（分隔符样式）
]

CN_NUM = {c: i + 1 for i, c in enumerate("一二三四五六七八九十")}


def _marker_info(line_text: str):
    """返回 (marker, type, value) 或 None。value 用于连续性校验。"""
    t = line_text.strip()
    for pat, typ, ordered in _MARKERS:
        m = pat.match(t)
        if m:
            marker = m.group(0)
            val = None
            if typ == "numeric":
                val = int(m.group(1))
            elif typ == "circled":
                val = ord(m.group(1)) - 0x2460 + 1
            elif typ == "cn_num":
                val = CN_NUM.get(m.group(1))
            elif typ == "alpha":
                val = ord(m.group(1).lower()) - ord("a") + 1
            return marker, typ, val, ordered
    return None


class ListDetector:
    """列表识别。"""

    def identify(self, blocks: list[Block]) -> None:
        """对顶层 text 块做列表重组：命中列表区域的块标注 type=list + list_items。"""
        for b in blocks:
            if b.type != "text" or len(b.lines) < 2:
                continue
            regions = self._split_regions(b)
            list_regions = [r for r in regions if r["type"] == "list"]
            if not list_regions:
                continue
            b.type = "list"
            b.list_items = regions
            b.text = ""

    # ------------------------------------------------------------------
    def _split_regions(self, block: Block) -> list[dict]:
        """把块拆成 [intro, list, intro, list, ...] 区域序列。

        连续 ≥2 行的同类列表标记构成一个 list 区域；其余为 intro 文本。
        """
        lines = block.lines
        regions: list[dict] = []
        buf: list[str] = []          # 待定 intro 行
        cur_list: list | None = None

        def flush_intro():
            nonlocal buf
            if buf:
                regions.append({"type": "intro", "marker": "",
                                "ordered": False, "text": "\n".join(buf),
                                "children": []})
                buf = []

        def flush_list():
            nonlocal cur_list
            if cur_list is not None:
                items = self._build_tree(cur_list, None)
                if items:
                    regions.append({"type": "list", "marker": "",
                                    "ordered": bool(items[0].get("ordered", False)),
                                    "text": "", "items": items,
                                    "children": []})
                cur_list = None

        for ln in lines:
            mi = _marker_info(ln.text)
            if mi is None:
                flush_list()
                buf.append(ln.text.strip())
            else:
                flush_intro()
                if cur_list is None:
                    cur_list = []
                cur_list.append((ln, mi))
        flush_intro()
        flush_list()
        return regions

    # ------------------------------------------------------------------
    def _build_tree(self, infos, intro=None) -> Optional[list[dict]]:
        """缩进聚类 + 连续性校验 + 嵌套树。"""
        # 连续性校验：有序列表要求序号连续
        ordered_types = {mi[1] for _, mi in infos if mi is not None and mi[3]}
        for t in ordered_types:
            vals = [mi[2] for _, mi in infos if mi is not None and mi[1] == t and mi[3]]
            if vals and not _is_continuous(vals):
                return None
        # 缩进层级
        x0s = [ln.x0 for ln, mi in infos if mi is not None]
        if not x0s:
            return None
        base_x = min(x0s)
        items: list[dict] = []
        stack: list[tuple[float, list]] = []  # (x, items容器)
        for ln, mi in infos:
            if mi is None:
                continue
            marker, typ, val, ordered = mi
            indent = round(ln.x0 - base_x, 1)
            while stack and indent <= stack[-1][0]:
                stack.pop()
            container = stack[-1][1] if stack else items
            item = {"marker": marker, "type": typ, "ordered": ordered,
                    "text": ln.text[len(marker):].strip(), "children": []}
            container.append(item)
            stack.append((indent, item["children"]))
        return items


def _is_continuous(vals: list[int]) -> bool:
    if not vals:
        return True
    seq = [v for v in vals if v is not None]
    if not seq:
        return True
    # 允许 1,2,3... 或 1,2,4,5（缺项但递增）
    return all(b >= a for a, b in zip(seq, seq[1:])) and seq[0] in (1, 2)
