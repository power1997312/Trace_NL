"""
最小回归测试：_merge_cross_page_paragraphs 跨页段合并 bug

Bug 描述（2026-08-18）：
  pdfparser/layout.py 的 _merge_cross_page_paragraphs 存在两个缺陷：
  1. 合并跨页段后未更新 a.page 字段（仍为前一页，但 bbox 混入后一页坐标）；
  2. 循环入口未检查 j/id(ordered[j]) 是否已被合并，导致后一页同一个
     first_text 块被反复合并到前一页的多个 text 块中。

  现象：DCS设备技术规格书.pdf 第1页的 "3.2 系统和设备分级" / "3.2.1 系统和设备分级"
  后多出 "质保等级:QA1。" 与 "DCS 供货商可以采用同一平台..." 两段（实际属于第2页顶部），
  且在第 1 页 5 个不同位置重复出现。

断言：
  1. 第 1 页文本不包含 "质保等级:QA1"（它应只属于第 2 页）；
  2. 同一文本片段在同一页内不得重复出现（重复注入检测）；
  3. 第 2 页顶部保留 "质保等级:QA1。"（原文真实内容）。
"""
import sys
import os
import glob

# 项目根入路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _find_pdf() -> str:
    pdfs = glob.glob(os.path.join(_ROOT, "**", "DCS设备技术规格书.pdf"), recursive=True)
    assert len(pdfs) == 1, f"期望唯一 PDF, 实际: {pdfs}"
    return pdfs[0]


def _render_pages():
    """使用新模块解析, 返回 {1-based页码: 文本}。"""
    from pdfparser import DocumentParser
    pdf = _find_pdf()
    parser = DocumentParser(pdf, prefer_camelot=False, asset_dir=None)
    result = parser.parse()
    assert result.meta.get("has_text_layer", True)
    pages = {}
    for blk in result.body:
        _collect(blk, pages)
    return pages


def _collect(blk, pages):
    if blk.type in ("consumed", "header_footer"):
        return
    page = blk.page + 1  # 0-based → 1-based
    pages.setdefault(page, [])
    if blk.type == "heading":
        t = blk.text.strip()
        if t:
            pages[page].append(t)
    elif blk.type in ("text", "paragraph"):
        t = blk.text.strip()
        if t:
            pages[page].append(t)
    elif blk.type == "list":
        for region in (blk.list_items or []):
            if region.get("type") == "intro":
                t = region.get("text", "").strip()
                if t:
                    pages[page].append(t)
            else:
                _render_items(region.get("items", []), pages[page])
    elif blk.type == "caption":
        t = blk.text.strip()
        if t:
            pages[page].append(t)
    for child in blk.children:
        _collect(child, pages)


def _render_items(items, out, depth=0):
    for item in items:
        marker = item.get("marker", "")
        t = item.get("text", "")
        out.append(f"{'  ' * depth}{marker}{t}")
        if item.get("children"):
            _render_items(item["children"], out, depth + 1)


def test_no_cross_page_pollution():
    pages = _render_pages()
    p1_text = "\n".join(pages.get(1, []))
    p2_text = "\n".join(pages.get(2, []))

    # 断言1: 第1页不得出现 "质保等级:QA1"（第2页顶部内容被错误注入）
    assert "质保等级:QA1" not in p1_text, \
        f"第1页被第2页顶部内容污染:\n{p1_text}"

    # 断言2: 第2页应保留原文 "质保等级:QA1。"
    assert "质保等级:QA1" in p2_text, \
        f"第2页原始内容丢失:\n{p2_text}"

    # 断言3: 第1页 3.2 章节下不应出现重复注入的 "DCS 供货商可以采用同一平台"
    count = p1_text.count("DCS 供货商可以采用同一平台")
    assert count <= 1, f"第1页重复注入 {count} 次:\n{p1_text}"

    # 断言4: 第1页真实标题 "3.2 系统和设备分级" 与 "3.2.1 系统和设备分级" 均应存在
    assert "3.2 系统和设备分级" in p1_text, f"真实章节标题缺失:\n{p1_text}"
    assert "3.2.1 系统和设备分级" in p1_text, f"真实章节标题缺失:\n{p1_text}"


if __name__ == "__main__":
    test_no_cross_page_pollution()
    print("PASS: test_no_cross_page_pollution")
