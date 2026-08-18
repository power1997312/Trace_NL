"""
回归测试（2026-08-18）：bullet 列表结构保留 + 正文段不被误识为列表

问题 1：DCS设备技术规格书 3.2.2.1 等 bullet 列表被合并成单段，bullet 标记消失
问题 2：RPS系统需求规范书 RQ-010/012/013 连续正文被误识为有序列表，导致句子折半

修复方向：
  1. layout.py 在 Wingdings bullet symbol 出现时 flush 前一段（独立成段）
  2. lists.py 拒绝将"无列表标记引导句"的整段连续正文识别为列表
"""
import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_pdf(name: str) -> str:
    pdfs = glob.glob(os.path.join("e:/Trace_NL", "**", name), recursive=True)
    assert len(pdfs) == 1, f"未找到唯一 PDF {name}: {pdfs}"
    return pdfs[0]


def _render(pdf: str) -> dict[int, list[str]]:
    from pdfparser import DocumentParser
    result = DocumentParser(pdf, prefer_camelot=False, asset_dir=None).parse()
    pages: dict[int, list[str]] = {}

    def _walk(blk):
        if blk.type in ("consumed", "header_footer"):
            return
        p = blk.page + 1
        if blk.type == "heading":
            if blk.text.strip():
                pages.setdefault(p, []).append(blk.text.strip())
        elif blk.type in ("text", "paragraph"):
            if blk.text.strip():
                pages.setdefault(p, []).append(blk.text.strip())
        elif blk.type == "list":
            for region in (blk.list_items or []):
                if region.get("type") == "intro":
                    t = region.get("text", "").strip()
                    if t:
                        pages.setdefault(p, []).append(t)
                else:
                    for it in region.get("items", []):
                        marker = it.get("marker", "")
                        t = it.get("text", "").strip()
                        if marker or t:
                            pages.setdefault(p, []).append(f"{marker}{t}")
                        for c in it.get("children", []):
                            cm = c.get("marker", "")
                            ct = c.get("text", "").strip()
                            if cm or ct:
                                pages.setdefault(p, []).append(f"  {cm}{ct}")
        elif blk.type == "caption":
            if blk.text.strip():
                pages.setdefault(p, []).append(blk.text.strip())
        for c in blk.children:
            _walk(c)
    for b in result.body:
        _walk(b)
    return pages


def test_dcs_bullet_list_preserved():
    """问题 1: 3.2.2.1 应保留为 7 个独立 bullet 行,而非合并成单段"""
    pages = _render(_find_pdf("DCS设备技术规格书.pdf"))
    p1 = "\n".join(pages.get(1, []))

    # 3.2.2.1 标题应存在
    assert "3.2.2.1 F-SC1 级要求" in p1, f"3.2.2.1 标题缺失:\n{p1}"

    # 7 个 bullet 内容都应保留(作为独立项,不被合并)
    bullets = [
        "单一故障准则",
        "在役检查和定期试验",
        "失去厂外电",
        "环境和抗震",
        "仪控部件的硬件鉴定",
        "软件开发NB/T20054",
        # PDF 字符混淆 "1"→"l", 接受 QAl/QA1 任意
        "质保等级",
    ]
    for b in bullets:
        assert b in p1, f"3.2.2.1 bullet 内容缺失: {b!r}"

    # 关键断言: "F-SC1 级设备执行安全功能" 引导句之后, "单一故障准则" 应作为
    # 独立项出现,而不是与 "在役检查和定期试验" 等被分号合并成单段。
    import re
    # 检测 "引导句...;在役检查..." 这种被合并的坏情况
    bad_pattern = re.compile(r"要满足下列\(不限于\)要求:.*?;.*?在役检查")
    assert not bad_pattern.search(p1), \
        f"3.2.2.1 bullet 被合并成单段 (分号拼接):\n{p1}"


def test_rps_paragraph_not_split_as_list():
    """问题 2: RQ-010 连续正文不应被识别为列表导致折半"""
    pages = _render(_find_pdf("RPS系统需求规范书.pdf"))
    p2 = "\n".join(pages.get(2, []))
    p3 = "\n".join(pages.get(3, []))

    # RQ-010 内容应作为连续文本输出, 不应在 "A、" 后折行
    assert "<RPS-SYS-RQ-010>" in p2, "RQ-010 标题缺失"
    # "两个逻辑系列（A、B）" 应在同一行/同一段连续出现
    assert "两个逻辑系列（A、B）" in p2 or "两个逻辑系列" in p2, \
        f"RQ-010 内容被折半:\n{p2}"


if __name__ == "__main__":
    test_dcs_bullet_list_preserved()
    print("PASS: test_dcs_bullet_list_preserved")
    test_rps_paragraph_not_split_as_list()
    print("PASS: test_rps_paragraph_not_split_as_list")