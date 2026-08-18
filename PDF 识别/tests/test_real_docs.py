"""真实文档 golden 断言（强力验证回归）。

覆盖 complex_pdfs 全部文档的关键质量门：
- 表格误检上限（F1 回归防线）
- 标题识别（F2/F3：双栏无编号标题 + 中文编号标题）
- 文本召回范围（F4：重复计数回归防线）
- 扫描件正确判别
"""
import glob
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from pdfparser import DocumentParser  # noqa: E402

CP = os.path.join(os.path.dirname(__file__), "..", "complex_pdfs")


def find(name_part: str) -> str:
    hits = [p for p in glob.glob(os.path.join(CP, "**", "*.pdf"), recursive=True)
            if name_part in os.path.basename(p)]
    assert hits, f"未找到含 {name_part} 的文档"
    return hits[0]


def parse(path: str):
    r = DocumentParser(path).parse()
    return r, r.to_dict()


def n_tables(d: dict) -> int:
    cnt = 0
    def walk(b):
        nonlocal cnt
        if b["type"] == "table":
            cnt += 1
        for c in b.get("children", []):
            walk(c)
    for b in d["body"]:
        walk(b)
    return cnt


# ---------------------------------------------------------------------------
# F1 表格误检防线（上限根据真实文档实际表格数量设定，留余量）
# ---------------------------------------------------------------------------
class TestTableFloodGuard:
    def test_alexnet(self):
        _, d = parse(find("1208.3962"))
        assert n_tables(d) <= 8, f"AlexNet 表格数 {n_tables(d)} 应 ≤8（误检回归）"

    def test_llm_survey(self):
        _, d = parse(find("2303.18223"))
        assert n_tables(d) <= 60, f"LLM_Survey 表格数 {n_tables(d)} 应 ≤60"

    def test_rfc9110(self):
        _, d = parse(find("rfc9110"))
        assert n_tables(d) <= 30, f"RFC 表格数 {n_tables(d)} 应 ≤30"

    def test_srs_online(self):
        _, d = parse(find("SRS_online"))
        assert n_tables(d) <= 20, f"SRS 表格数 {n_tables(d)} 应 ≤20"

    def test_gov(self):
        _, d = parse(find("国家数据"))
        assert n_tables(d) <= 10, f"gov 表格数 {n_tables(d)} 应 ≤10"

    def test_jos_p2p(self):
        _, d = parse(find("jos_2007"))
        assert n_tables(d) <= 12, f"jos_P2P 表格数 {n_tables(d)} 应 ≤12"


# ---------------------------------------------------------------------------
# F2/F3 标题识别
# ---------------------------------------------------------------------------
class TestHeadings:
    def test_gov_chinese_headings(self):
        """中文编号标题：一、/（一）"""
        r, d = parse(find("国家数据"))
        texts = [o["text"] for o in d["outline"]]
        assert len(texts) >= 10, f"gov 大纲 {len(texts)} 条，应 ≥10"
        assert any("总体要求" in t for t in texts)

    def test_attention_headings(self):
        """双栏无编号标题（居中+大字号判据）"""
        r, d = parse(find("1706.03762"))
        assert len(d["outline"]) >= 5, f"Attention 大纲 {len(d['outline'])} 条，应 ≥5"

    def test_alexnet_headings(self):
        r, d = parse(find("1208.3962"))
        assert len(d["outline"]) >= 5, f"AlexNet 大纲 {len(d['outline'])} 条，应 ≥5"

    def test_resnet_title(self):
        r, d = parse(find("1512.03385"))
        texts = [o["text"] for o in d["outline"]]
        assert any("Deep Residual" in t for t in texts) or len(texts) >= 4

    def test_engineering_docs_still_ok(self):
        """工程文档（用户核心场景）不回归：SRS 大纲不下降"""
        r, d = parse(find("SRS_online"))
        assert len(d["outline"]) >= 40, f"SRS 大纲 {len(d['outline'])} 条，应 ≥40"


# ---------------------------------------------------------------------------
# F4 文本召回范围（>115% 说明重复计数，<60% 说明内容丢失）
# ---------------------------------------------------------------------------
class TestTextRecall:
    @pytest.mark.parametrize("name_part,lo,hi", [
        ("1208.3962", 0.6, 1.15),
        ("2303.18223", 0.6, 1.15),
        ("rfc9110", 0.6, 1.15),
        ("SRS_online", 0.6, 1.15),
        ("国家数据", 0.6, 1.15),
    ])
    def test_recall_bounded(self, name_part, lo, hi):
        from evaluate import count_chars_in_result  # 复用工具函数
        import fitz
        path = find(name_part)
        r, d = parse(path)
        raw = sum(len(fitz.open(path)[i].get_text("text"))
                  for i in range(fitz.open(path).page_count))
        out = count_chars_in_result(d)
        recall = out / raw if raw else 0
        assert lo <= recall <= hi, f"{name_part} 召回 {recall:.2%} 超出 [{lo:.0%},{hi:.0%}]"


# ---------------------------------------------------------------------------
# 扫描件判别
# ---------------------------------------------------------------------------
class TestScanned:
    def test_gbt8567_scanned(self):
        r, d = parse(find("GBT8567"))
        assert d["meta"].get("scanned") is True or not d["meta"].get("has_text_layer")


# ---------------------------------------------------------------------------
# 段落连贯性与图/矢量图识别（2026-08-15 增强）
# ---------------------------------------------------------------------------
class TestParagraphAndFigures:
    def test_gov_paragraph_joined(self):
        """中文公文段落应连贯（首行缩进不拆段）。"""
        r, d = parse(find("国家数据"))
        texts = []
        def walk(b):
            if b["type"] in ("text", "paragraph"):
                texts.append(b["text"])
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        joined = "".join(t.replace("\n", "") for t in texts)
        assert "以习近平新时代中国特色社会主义思想为指导，深入学习贯彻党的二十大" in joined

    def test_gov_figure_text_extracted(self):
        """gov 结构图内文字应归入 figure 块（不入正文段落）。"""
        r, d = parse(find("国家数据"))
        n_fig = 0
        fig_has_label = False
        def walk(b):
            nonlocal n_fig, fig_has_label
            if b["type"] == "figure":
                n_fig += 1
                if "AA术语" in b["text"] or "数据基础设施安全" in b["text"]:
                    fig_has_label = True
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        assert n_fig >= 1, "gov 应识别出结构图"
        assert fig_has_label, "结构图内文字标注应被提取"

    def test_resnet_vector_figures(self):
        """ResNet 矢量图（tikz）应按 Figure 图题识别为 figure 块。"""
        r, d = parse(find("1512.03385"))
        n_fig = 0
        def walk(b):
            nonlocal n_fig
            if b["type"] == "figure":
                n_fig += 1
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        assert n_fig >= 3, f"ResNet 应识别 ≥3 个矢量图，实际 {n_fig}"

    def test_resnet_no_chart_as_table(self):
        """ResNet 折线图（轴标签）不应被误判为表格。"""
        r, d = parse(find("1512.03385"))
        tables = []
        def walk(b):
            if b["type"] == "table":
                tables.append(b["table"])
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        for t in tables:
            for row in t["rows"]:
                for c in row:
                    assert "rorre" not in (c["text"] or ""), "折线图被误判为表格"

    def test_gov_figure_image_rendered(self):
        """gov 结构图应被区域渲染为 PNG（完整保留图，不只文字标注）。"""
        import os
        r, d = parse(find("国家数据"))
        figs = []
        def walk(b):
            if b["type"] == "figure":
                figs.append(b)
            for c in b.get("children", []):
                walk(c)
        for b in d["body"]:
            walk(b)
        # 至少应导出到 assets 目录
        import tempfile, subprocess
        # 走 CLI 触发导出更可靠；这里改为检查 figure 块带 asset 或通过临时跑 CLI
        with tempfile.TemporaryDirectory() as td:
            from pdfparser import DocumentParser
            DocumentParser(find("国家数据"), asset_dir=td).parse()
            rendered = [f for f in os.listdir(td) if f.endswith(".png") and f.startswith("fig_")]
            assert len(rendered) >= 5, f"应渲染 ≥5 张结构图，实际 {len(rendered)}"
