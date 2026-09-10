# -*- coding: utf-8 -*-
"""
把《追踪系统白话设计全解.html》转换为排版规范的 Word 文档。

流程:
  1) 用 Chrome/Edge 无头模式把文档内每张 SVG 图渲染成 2 倍高清 PNG
  2) 用 BeautifulSoup 走一遍正文节点, 按中文排版规范映射为 python-docx 元素
  3) A4 / 页边距 2.2cm / 页脚页码 / 图片按版心宽度自适应

依赖: python-docx, beautifulsoup4, lxml(或 bs4 自带解析器)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

from bs4 import BeautifulSoup
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# ---------------------------------------------------------------
# 路径
# ---------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
SRC_HTML = os.path.join(ROOT, "追踪系统白话设计全解.html")
FIG_DIR = os.path.join(ROOT, "output", "docx_figs")
OUT_DOCX = os.path.join(ROOT, "追踪系统白话设计全解.docx")

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

BODY_FONT = "宋体"
HEAD_FONT = "微软雅黑"
MONO_FONT = "Consolas"

COLOR_MAP = {
    "sw-green": RGBColor(0x00, 0xB0, 0x50),
    "sw-blue": RGBColor(0x00, 0x70, 0xC0),
    "sw-black": RGBColor(0x00, 0x00, 0x00),
}
BOX_FILL = {
    "b-blue": "EFF6FF", "b-green": "F0FDF4", "b-amber": "FFFBEB",
    "b-red": "FEF2F2", "b-purple": "F5F3FF",
}


def find_browser() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise RuntimeError("未找到 Chrome / Edge，无法渲染 SVG 图")


# ---------------------------------------------------------------
# 一、SVG -> PNG
# ---------------------------------------------------------------
def render_svgs(soup: BeautifulSoup) -> None:
    os.makedirs(FIG_DIR, exist_ok=True)
    browser = find_browser()
    svgs = soup.find_all("svg")
    print(f"[1/3] 渲染 {len(svgs)} 张示意图 ...")
    tmpdir = tempfile.mkdtemp(prefix="svg2png_")

    for idx, svg in enumerate(svgs, 1):
        vb = (svg.get("viewBox") or svg.get("viewbox") or "0 0 900 300").split()
        w = int(float(vb[2]))
        h = int(float(vb[3]))
        page = os.path.join(tmpdir, f"fig{idx:02d}.html")
        png = os.path.join(FIG_DIR, f"fig{idx:02d}.png")
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
            'html,body{margin:0;padding:0;background:#ffffff;'
            'font-family:"Microsoft YaHei","PingFang SC",sans-serif;}'
            f'.box{{width:{w}px;height:{h}px;overflow:hidden;background:#fff;}}'
            'svg{display:block;}'
            '</style></head><body><div class="box">'
            + str(svg)
            + "</div></body></html>"
        )
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)

        cmd = [
            browser, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=2",
            f"--window-size={w},{h}", f"--screenshot={png}",
            "--virtual-time-budget=3000", "--default-background-color=FFFFFFFF",
            f"file:///{page.replace(chr(92), '/')}",
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        if not os.path.exists(png) or os.path.getsize(png) < 1000:
            print(f"    [WARN] 图 {idx} 渲染失败: {r.stderr.decode('utf-8', 'ignore')[:200]}")
        else:
            print(f"    图 {idx:02d} -> {os.path.basename(png)} "
                  f"({os.path.getsize(png)//1024} KB)")

        # 用 img 占位替换 svg, 并记录原始宽高比
        img = soup.new_tag("img")
        img["src"] = png
        img["_w"] = str(w)
        img["_h"] = str(h)
        svg.replace_with(img)


# ---------------------------------------------------------------
# 二、docx 基础工具
# ---------------------------------------------------------------
def set_run_font(run, name=BODY_FONT, size=None, bold=None, color=None, italic=None):
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), name)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def shade(el, fill: str):
    """给段落或单元格加底纹"""
    pr = el.get_or_add_tcPr() if el.tag.endswith("tc") else el.get_or_add_pPr()
    shd = pr.makeelement(qn("w:shd"), {})
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def add_inline(container, node, size=11, base_color=None, mono=False):
    """递归渲染行内内容(b / span / u / em / 纯文本)"""
    from bs4 import NavigableString

    if isinstance(node, NavigableString):
        text = str(node)
        if not text.strip():
            if text:
                container.add_run(text)
            return
        run = container.add_run(text)
        set_run_font(run, MONO_FONT if mono else BODY_FONT, size,
                     color=base_color)
        return

    if node.name in ("br",):
        container.add_run("\n")
        return

    classes = node.get("class") or []
    color = base_color
    for c in classes:
        if c in COLOR_MAP:
            color = COLOR_MAP[c]

    bold = node.name in ("b", "strong") or "t" in classes or "h" in classes
    italic = node.name in ("em", "i", "u")
    mono_child = mono or node.name in ("code",)

    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if not text.strip("\n"):
                if text:
                    container.add_run(text)
                continue
            run = container.add_run(text)
            set_run_font(run, MONO_FONT if mono_child else BODY_FONT, size,
                         bold=bold if bold else None,
                         color=color, italic=italic or None)
        else:
            add_inline(container, child, size, color, mono_child)


def add_para(doc, node, size=11, align=None, space_after=6, indent=None,
             color=None, bold_all=False, italic_all=False):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.space_after = Pt(space_after)
    pf.space_before = Pt(2)
    pf.line_spacing = 1.5
    if align:
        p.alignment = align
    if indent:
        pf.left_indent = Cm(indent)
    if bold_all or italic_all:
        for r in p.runs:
            pass
    for child in node.children:
        if getattr(child, "name", None) in ("div", "p"):
            for c2 in child.children:
                add_inline(p, c2, size, color)
            p.add_run(" ")
        else:
            add_inline(p, child, size, color)
    if bold_all:
        for r in p.runs:
            r.font.bold = True
    if italic_all:
        for r in p.runs:
            r.font.italic = True
    if not p.runs:
        p.add_run("")
    return p


def add_shaded_block(doc, node, fill="F1F5F9", size=10.5):
    """带底纹的提示块(box / term)"""
    title = node.find(class_="t")
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    shade(cell._tc, fill)
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.line_spacing = 1.4
    if title:
        r = p.add_run(title.get_text(strip=True))
        set_run_font(r, HEAD_FONT, size + 0.5, bold=True)
        p = cell.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.4
    for child in node.children:
        if child is title:
            continue
        cname = getattr(child, "name", None)
        if cname == "p":
            if p.runs:
                p = cell.add_paragraph()
                p.paragraph_format.space_after = Pt(2)
                p.paragraph_format.line_spacing = 1.4
            for c in child.children:
                add_inline(p, c, size)
        else:
            if cname is None and not str(child).strip():
                continue
            add_inline(p, child, size)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)
    return tbl


def add_card_grid(doc, node, size=10.5):
    cards = node.find_all(class_="card", recursive=False) or node.find_all(class_="card")
    if not cards:
        return
    ncol = min(len(cards), 3)
    nrow = (len(cards) + ncol - 1) // ncol
    tbl = doc.add_table(rows=nrow, cols=ncol)
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, card in enumerate(cards):
        cell = tbl.cell(i // ncol, i % ncol)
        head = card.find(class_="h")
        paras = card.find_all("p")
        first = True
        if head:
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.35
            for c in head.children:
                add_inline(p, c, size)
            for r in p.runs:
                r.font.bold = True
                r.font.name = HEAD_FONT
                r._element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
            first = False
        for pp in paras:
            if not pp.get_text(strip=True):
                continue
            p = cell.paragraphs[0] if first else cell.add_paragraph()
            first = False
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.35
            for c in pp.children:
                add_inline(p, c, size)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def _bold_override_noop():
    pass


def add_table(doc, node, size=10):
    rows = node.find_all("tr")
    if not rows:
        return
    maxcol = max(len(r.find_all(["td", "th"])) for r in rows)
    tbl = doc.add_table(rows=0, cols=maxcol)
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ri, tr in enumerate(rows):
        cells = tr.find_all(["td", "th"])
        row = tbl.add_row()
        for ci in range(maxcol):
            cell = row.cells[ci]
            if ci >= len(cells):
                continue
            src = cells[ci]
            if ri == 0:
                shade(cell._tc, "EEF4FF")
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.space_before = Pt(1)
            p.paragraph_format.line_spacing = 1.3
            for c in src.children:
                add_inline(p, c, size)
            for r in p.runs:
                if ri == 0:
                    r.font.bold = True
                r.font.name = BODY_FONT
                r._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def add_image(doc, img, max_width_cm=16.0):
    src = img.get("src")
    if not src or not os.path.exists(src):
        p = doc.add_paragraph("[图片缺失]")
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        return
    w = float(img.get("_w") or 900)
    h = float(img.get("_h") or 300)
    width = Cm(max_width_cm)
    height = Cm(max_width_cm * h / w)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(2)
    p.add_run().add_picture(src, width=width, height=height)


def add_page_number_footer(doc):
    from docx.oxml import OxmlElement
    footer = doc.sections[0].footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run()
    set_run_font(r, BODY_FONT, 9, color=RGBColor(0x64, 0x74, 0x8B))
    for instr in ("PAGE",):
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), instr)
        run_el = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        sz = OxmlElement("w:sz"); sz.set(qn("w:val"), "18"); rpr.append(sz)
        run_el.append(rpr)
        t = OxmlElement("w:t")
        t.text = "1"
        run_el.append(t)
        fld.append(run_el)
        p._p.append(fld)


def add_hr(doc):
    from docx.oxml import OxmlElement
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(8)
    pPr = p._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CBD5E1")
    borders.append(bottom)
    pPr.append(borders)


# ---------------------------------------------------------------
# 三、主流程
# ---------------------------------------------------------------
def build():
    with open(SRC_HTML, encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "lxml")

    render_svgs(soup)

    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.top_margin = Cm(2.2)
    sec.bottom_margin = Cm(2.2)
    sec.left_margin = Cm(2.4)
    sec.right_margin = Cm(2.4)

    style = doc.styles["Normal"]
    style.font.name = BODY_FONT
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    style.paragraph_format.line_spacing = 1.5
    style.paragraph_format.space_after = Pt(6)

    print("[2/3] 生成 Word 正文 ...")
    body = soup.body
    main = body.find("main") or body

    # 标题区
    header = body.find("header")
    if header:
        h1 = header.find("h1")
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(4)
        r = p.add_run(h1.get_text(strip=True))
        set_run_font(r, HEAD_FONT, 20, bold=True, color=RGBColor(0x1E, 0x3A, 0x8A))

        sub = header.find(class_="sub")
        if sub:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(8)
            r = p.add_run(" ｜ ".join(sub.get_text(" ", strip=True).split("｜")) )
            set_run_font(r, BODY_FONT, 9.5, color=RGBColor(0x64, 0x74, 0x8B))

        intro = header.find_all("p")
        for ip in intro:
            if ip.get_text(strip=True):
                pp = doc.add_paragraph()
                pp.paragraph_format.space_after = Pt(10)
                pp.paragraph_format.line_spacing = 1.5
                for c in ip.children:
                    add_inline(pp, c, 11)

    toc_skipped = False
    for node in main.children:
        name = getattr(node, "name", None)
        if name is None:
            continue
        classes = node.get("class") or []

        # 目录: 转为普通条目列表
        if "toc" in classes:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            r = p.add_run("目　录")
            set_run_font(r, HEAD_FONT, 14, bold=True)
            for li in node.find_all("li"):
                pp = doc.add_paragraph()
                pp.paragraph_format.left_indent = Cm(0.8)
                pp.paragraph_format.space_after = Pt(1)
                pp.paragraph_format.line_spacing = 1.3
                for c in li.children:
                    add_inline(pp, c, 10.5)
            add_hr(doc)
            continue

        if name == "h2":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(18)
            p.paragraph_format.space_after = Pt(8)
            r = p.add_run(node.get_text(strip=True))
            set_run_font(r, HEAD_FONT, 16, bold=True, color=RGBColor(0x1E, 0x3A, 0x8A))
        elif name == "h3":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(12)
            p.paragraph_format.space_after = Pt(4)
            r = p.add_run(node.get_text(strip=True))
            set_run_font(r, HEAD_FONT, 13.5, bold=True, color=RGBColor(0x0F, 0x17, 0x2A))
        elif name == "h4":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(3)
            r = p.add_run(node.get_text(strip=True))
            set_run_font(r, HEAD_FONT, 12, bold=True, color=RGBColor(0x33, 0x41, 0x55))
        elif name == "p":
            if node.find("img"):
                for img in node.find_all("img"):
                    add_image(doc, img)
            elif node.get_text(strip=True):
                add_para(doc, node, 11)
        elif name == "figure":
            cap = node.find("figcaption")
            imgs = node.find_all("img")
            for img in imgs:
                add_image(doc, img)
            if not imgs:
                # 无图的示例块(如着色示意): 逐行渲染, 保留绿/蓝/黑字色
                for leaf in node.find_all(["div", "p"]):
                    if leaf.find(["div", "p"]):
                        continue
                    if not leaf.get_text(strip=True):
                        continue
                    p = doc.add_paragraph()
                    p.paragraph_format.space_after = Pt(3)
                    p.paragraph_format.line_spacing = 1.6
                    for c in leaf.children:
                        cls = c.get("class") if getattr(c, "name", None) else []
                        if cls and "swatch" in cls:
                            continue
                        add_inline(p, c, 11)
            if cap and cap.get_text(strip=True):
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.space_after = Pt(12)
                p.paragraph_format.line_spacing = 1.3
                r = p.add_run(cap.get_text(strip=True))
                set_run_font(r, BODY_FONT, 9.5, color=RGBColor(0x64, 0x74, 0x8B),
                             italic=True)
        elif name in ("ul", "ol"):
            for li in node.find_all("li", recursive=False):
                p = doc.add_paragraph(style="List Bullet" if name == "ul" else "List Number")
                p.paragraph_format.space_after = Pt(2)
                p.paragraph_format.line_spacing = 1.5
                for c in li.children:
                    add_inline(p, c, 11)
        elif name == "table":
            add_table(doc, node)
        elif name == "hr":
            add_hr(doc)
        elif name == "div":
            if "box" in classes:
                fill = "F1F5F9"
                for c in classes:
                    if c in BOX_FILL:
                        fill = BOX_FILL[c]
                add_shaded_block(doc, node, fill)
            elif "cards" in classes:
                add_card_grid(doc, node)
            elif "term" in classes:
                add_shaded_block(doc, node, "F8FAFC")
            elif "q" in classes:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Cm(0.6)
                p.paragraph_format.space_after = Pt(10)
                for c in node.children:
                    add_inline(p, c, 10.5, RGBColor(0x64, 0x74, 0x8B))
                for r in p.runs:
                    r.font.italic = True
            else:
                # 其它容器: 递归处理内部块级内容
                for child in node.children:
                    cname = getattr(child, "name", None)
                    if cname == "p":
                        if child.find("img"):
                            for img in child.find_all("img"):
                                add_image(doc, img)
                        elif child.get_text(strip=True):
                            add_para(doc, child, 11)
                    elif cname == "figure":
                        cap = child.find("figcaption")
                        for img in child.find_all("img"):
                            add_image(doc, img)
                        if cap and cap.get_text(strip=True):
                            p = doc.add_paragraph()
                            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                            p.paragraph_format.space_after = Pt(12)
                            r = p.add_run(cap.get_text(strip=True))
                            set_run_font(r, BODY_FONT, 9.5,
                                         color=RGBColor(0x64, 0x74, 0x8B), italic=True)
                    elif cname == "img":
                        add_image(doc, child)

    add_page_number_footer(doc)
    print(f"[3/3] 写出: {OUT_DOCX}")
    doc.save(OUT_DOCX)
    size_kb = os.path.getsize(OUT_DOCX) // 1024
    print(f"完成，文件大小 {size_kb} KB")


if __name__ == "__main__":
    build()
