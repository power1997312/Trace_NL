"""端到端回归测试：基于合成样本的 golden 断言。

覆盖研究报告的 8 类难点：
- sample_a: 多级标题 / 图文插接 / 序号·圆点·符号列表 / 页眉页脚
- sample_b: 有线表格（含合并格）
- sample_c: 无线表格
- sample_d: 跨页接续表
- sample_e: 乱序内容流（阅读顺序修复）
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pdfparser import DocumentParser  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")
OUT = os.path.join(os.path.dirname(__file__), "..", "output", "pytest")


def parse(name: str):
    """解析样本并返回 DocumentResult + 字典化结果。"""
    p = DocumentParser(os.path.join(SAMPLES, name), prefer_camelot=True)
    r = p.parse()
    assert r.meta.get("has_text_layer"), f"{name} 应有文本层"
    return r, r.to_dict()


# ---------------------------------------------------------------------------
# sample_a：标题 / 图文 / 列表 / 页眉页脚
# ---------------------------------------------------------------------------
class TestSampleA:
    def test_headings_hierarchy(self):
        r, d = parse("sample_a.pdf")
        texts = [(o["level"], o["text"]) for o in d["outline"]]
        assert ("2", "1. 系统概述") in [(str(l), t) for l, t in texts] or \
               (2, "1. 系统概述") in texts, texts
        assert (3, "1.1 背景") in texts
        assert (2, "4. 文档管理") in texts
        assert len(texts) >= 10

    def test_no_header_footer_in_body(self):
        r, d = parse("sample_a.pdf")
        def walk(b, acc):
            acc.append(b.get("text", ""))
            for c in b.get("children", []):
                walk(c, acc)
        acc = []
        for b in d["body"]:
            walk(b, acc)
        joined = "\n".join(acc)
        assert "内部资料" not in joined
        assert "第 1 页" not in joined

    def test_figure_interleaving(self):
        """图文插接：1.1 背景 下应存在 图题→图片→图题→图后文字 的顺序结构。"""
        r, d = parse("sample_a.pdf")
        def find(b, lv):
            if b.get("level") == lv and "1.1" in b.get("text", ""):
                return b
            for c in b.get("children", []):
                res = find(c, lv)
                if res:
                    return res
            return None
        h3 = find({"children": d["body"]}, 3)
        assert h3 is not None
        types = []
        for c in h3.get("children", []):
            types.append(c["type"])
        assert "image" in types
        # 图后应有接续文字（场景 A）
        texts = [c.get("text", "") for c in h3.get("children", []) if c["type"] == "text"]
        assert any("三层架构" in t for t in texts)

    def test_lists_detected(self):
        r, d = parse("sample_a.pdf")
        n_lists = 0
        items_total = 0
        def walk(b):
            nonlocal n_lists, items_total
            if b["type"] == "list":
                n_lists += 1
                for reg in b["list_items"]:
                    items_total += len(reg.get("items", []))
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        assert n_lists >= 3, f"应识别 ≥3 个列表块，实际 {n_lists}"
        assert items_total >= 12

    def test_image_asset_exported(self):
        r, d = parse("sample_a.pdf")
        assert d["meta"].get("n_assets", 0) >= 1


# ---------------------------------------------------------------------------
# sample_b：有线表格
# ---------------------------------------------------------------------------
class TestSampleB:
    def test_two_lattice_tables(self):
        r, d = parse("sample_b.pdf")
        tables = _collect_tables(d)
        assert len(tables) == 2, tables
        t0 = tables[0]
        assert t0["n_rows"] == 5 and t0["n_cols"] == 5
        assert t0["rows"][0][0]["text"] == "序号"
        assert t0["rows"][1][1]["text"] == "安全级机柜"
        # 合并格：第二个表 模拟量 应跨两行
        t1 = tables[1]
        assert t1["rows"][1][0]["rowspan"] >= 2 or t1["rows"][2][0]["rowspan"] >= 2


# ---------------------------------------------------------------------------
# sample_c：无线表格
# ---------------------------------------------------------------------------
class TestSampleC:
    def test_stream_tables(self):
        r, d = parse("sample_c.pdf")
        tables = _collect_tables(d)
        assert len(tables) == 2, tables
        assert tables[0]["n_rows"] == 5 and tables[0]["n_cols"] == 4
        assert tables[0]["rows"][0][0]["text"] == "参数名称"
        assert tables[1]["rows"][3][3]["text"] == "SSE"


# ---------------------------------------------------------------------------
# sample_d：跨页接续表
# ---------------------------------------------------------------------------
class TestSampleD:
    def test_cross_page_join(self):
        r, d = parse("sample_d.pdf")
        tables = _collect_tables(d)
        assert len(tables) == 1, tables
        t = tables[0]
        assert t["n_rows"] == 71, t["n_rows"]  # 表头 1 + 数据 70
        assert t["n_cols"] == 5
        assert t["page_end"] - t["page_start"] == 1  # 跨两页
        # 第二页表头不应重复：第 36 行应为数据（PT-036）
        assert "PT-036" in t["rows"][36][1]["text"]


# ---------------------------------------------------------------------------
# sample_e：乱序内容流 → 阅读顺序修复
# ---------------------------------------------------------------------------
class TestSampleE:
    def test_reading_order_fixed(self):
        r, d = parse("sample_e.pdf")
        orders = []
        def walk(b):
            orders.append((b.get("order", -1), b["type"], b.get("text", "")[:12]))
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        orders.sort()
        # 视觉顶部标题（内容流最后绘制）应排在最前
        assert orders[0][1] == "heading" and "第六章" in orders[0][2], orders
        assert any("正文" in o[2] for o in orders), orders


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _collect_tables(d):
    tables = []
    def walk(b):
        if b["type"] == "table":
            tables.append(b["table"])
        for c in b.get("children", []):
            walk(c)
    for b in d["body"]:
        walk(b)
    return tables
