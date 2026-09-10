# -*- coding: utf-8 -*-
"""
生成《候选追踪关系发现——算法与流程总览》Visio 图 (.vsdx, Visio2013/2016 格式)
图中仅使用中文术语，不出现函数名与参数。
同时输出同版式 SVG 预览图。
"""
import os
import zipfile
import html

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
VSDX_PATH = os.path.join(OUT_DIR, "候选追踪关系发现流程图.vsdx")
SVG_PATH = os.path.join(OUT_DIR, "候选追踪关系发现流程图预览.svg")

FONT = "微软雅黑"
PAGE_W = 11.6929
PAGE_H = 8.2677


def num(v):
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def esc(s):
    return html.escape(s, quote=False)


# ------------------------------------------------------------
# Visio 形状构建
# ------------------------------------------------------------

def _char_section(color, size_pt, bold):
    return (
        "<Section N='Character'>"
        "<Row IX='0'>"
        "<Cell N='Font' V='1'/>"
        "<Cell N='ComplexScriptFont' V='1'/>"
        "<Cell N='AsianFont' V='1'/>"
        f"<Cell N='Color' V='{color}'/>"
        f"<Cell N='Style' V='{1 if bold else 0}'/>"
        f"<Cell N='Size' V='{num(size_pt / 72)}'/>"
        "</Row></Section>"
    )


def _para_section(align):
    return (
        "<Section N='Paragraph'>"
        "<Row IX='0'>"
        f"<Cell N='HorzAlign' V='{align}'/>"
        "<Cell N='IndFirst' V='0'/><Cell N='IndLeft' V='0'/><Cell N='IndRight' V='0'/>"
        "<Cell N='SpLine' V='-1.25'/><Cell N='SpBefore' V='0'/><Cell N='SpAfter' V='0'/>"
        "</Row></Section>"
    )


def rect_shape(sid, cx, cy, w, h, text, fill, line, tcolor, size_pt,
               bold=False, rounding=0.05, align=1, valign=1, weight_pt=1.0,
               fill_pattern=1, line_pattern=1):
    return f"""<Shape ID='{sid}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>
<Cell N='Rounding' V='{num(rounding)}'/>
<Cell N='LineWeight' V='{num(weight_pt / 72)}'/>
<Cell N='LineColor' V='{line}'/>
<Cell N='LinePattern' V='{line_pattern}'/>
<Cell N='LineCap' V='0'/>
<Cell N='BeginArrow' V='0'/><Cell N='EndArrow' V='0'/>
<Cell N='FillForegnd' V='{fill}'/>
<Cell N='FillPattern' V='{fill_pattern}'/>
<Cell N='VerticalAlign' V='{valign}'/>
<XForm>
<Cell N='PinX' V='{num(cx)}'/><Cell N='PinY' V='{num(cy)}'/>
<Cell N='Width' V='{num(w)}'/><Cell N='Height' V='{num(h)}'/>
<Cell N='LocPinX' V='{num(w / 2)}'/><Cell N='LocPinY' V='{num(h / 2)}'/>
<Cell N='Angle' V='0'/><Cell N='FlipX' V='0'/><Cell N='FlipY' V='0'/>
<Cell N='ResizeMode' V='0'/>
</XForm>
<Section N='Geometry' IX='0'>
<Cell N='NoFill' V='0' U='BOOL'/><Cell N='NoLine' V='0' U='BOOL'/>
<Cell N='NoShow' V='0' U='BOOL'/><Cell N='NoSnap' V='0' U='BOOL'/>
<Cell N='NoQuickDrag' V='0' U='BOOL'/>
<Row T='RelMoveTo' IX='1'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row>
<Row T='RelLineTo' IX='2'><Cell N='X' V='1'/><Cell N='Y' V='0'/></Row>
<Row T='RelLineTo' IX='3'><Cell N='X' V='1'/><Cell N='Y' V='1'/></Row>
<Row T='RelLineTo' IX='4'><Cell N='X' V='0'/><Cell N='Y' V='1'/></Row>
<Row T='RelLineTo' IX='5'><Cell N='X' V='0'/><Cell N='Y' V='0'/></Row>
</Section>
{_char_section(tcolor, size_pt, bold)}
{_para_section(align)}
<Text>{esc(text)}</Text>
</Shape>"""


def line_shape(sid, x1, y1, x2, y2, color="#4472C4", weight_pt=1.5,
               arrow_end=13, pattern=1):
    bx, by = min(x1, x2), min(y1, y2)
    w = max(abs(x2 - x1), 0.02)
    h = max(abs(y2 - y1), 0.02)
    lx1, ly1 = x1 - bx, y1 - by
    lx2, ly2 = x2 - bx, y2 - by
    return f"""<Shape ID='{sid}' Type='Shape' LineStyle='0' FillStyle='0' TextStyle='0'>
<Cell N='LineWeight' V='{num(weight_pt / 72)}'/>
<Cell N='LineColor' V='{color}'/>
<Cell N='LinePattern' V='{pattern}'/>
<Cell N='LineCap' V='0'/>
<Cell N='BeginArrow' V='0'/><Cell N='EndArrow' V='{arrow_end}'/>
<Cell N='EndArrowSize' V='2'/>
<XForm>
<Cell N='PinX' V='{num(bx)}'/><Cell N='PinY' V='{num(by)}'/>
<Cell N='Width' V='{num(w)}'/><Cell N='Height' V='{num(h)}'/>
<Cell N='LocPinX' V='0'/><Cell N='LocPinY' V='0'/>
<Cell N='Angle' V='0'/><Cell N='FlipX' V='0'/><Cell N='FlipY' V='0'/>
<Cell N='ResizeMode' V='0'/>
</XForm>
<Section N='Geometry' IX='0'>
<Cell N='NoFill' V='1' U='BOOL'/><Cell N='NoLine' V='0' U='BOOL'/>
<Cell N='NoShow' V='0' U='BOOL'/><Cell N='NoSnap' V='0' U='BOOL'/>
<Cell N='NoQuickDrag' V='0' U='BOOL'/>
<Row T='MoveTo' IX='1'><Cell N='X' V='{num(lx1)}'/><Cell N='Y' V='{num(ly1)}'/></Row>
<Row T='LineTo' IX='2'><Cell N='X' V='{num(lx2)}'/><Cell N='Y' V='{num(ly2)}'/></Row>
</Section>
</Shape>"""


# ------------------------------------------------------------
# 图内容定义（全中文术语）
# ------------------------------------------------------------

STAGES = [
    {
        "chip": "第一步 · 文档单元化",
        "body": "把上下游文档切成条目/章节单元，记录正文与位置区间，剔除目录与伪标题",
        "card": [
            "• 候选池＝条目＋章节混装，兼容没有编号的上游文档",
            "• 每个单元记下正文和在全文中的位置区间",
            "• 按序号骨架剔除目录页与伪章节标题",
        ],
        "chip_c": "#4472C4", "body_c": "#DEEBF7", "card_h": 0.86,
    },
    {
        "chip": "第二步 · 章节骨架对齐",
        "body": "章节标题与正文双重相似，先对齐文档骨架，产出章节关系与位置加分",
        "card": [
            "• 标题相似且正文语义相近才对齐（互为最优）",
            "• 对齐只给同区候选加分，绝不拦截其他候选",
            "• 章节级对齐关系本身也是交付成果",
        ],
        "chip_c": "#4472C4", "body_c": "#DEEBF7", "card_h": 0.86,
    },
    {
        "chip": "第三步 · 整体粗筛",
        "body": "整条语义与词汇初筛，每条下游需求保留前十名候选",
        "card": [
            "• 整条语义相似度＋词汇重合度合成初筛分",
            "• 超长条目分段编码取平均，防止截断失真",
            "• 宁多勿漏，把精确排序留给下一阶段",
        ],
        "chip_c": "#4472C4", "body_c": "#DEEBF7", "card_h": 0.86,
    },
    {
        "chip": "第四步 · 精细比对",
        "body": "逐句找落点算覆盖率，稀有词双向命中，两路精细证据",
        "card": [
            "〖句级语义聚合〗治“长内容稀释”：",
            "• 下游每句话到候选中找最相近的句子，统计有着落的比例",
            "• 平均相似度与最佳句对，共同构成精细语义分",
            "〖稀有词证据〗治“同模板章节混淆”：",
            "• 越罕见的词权重越高，常见词自动忽略",
            "• 双向命中稀有词，一词之差分辨“孪生”章节",
        ],
        "chip_c": "#4472C4", "body_c": "#DEEBF7", "card_h": 1.06,
    },
    {
        "chip": "第五步 · 综合裁决",
        "body": "三路证据加权合分，过线收录、接近存疑、一对多追加",
        "card": [
            "• 语义为主、词汇次之、章节结构加分，加权合分",
            "• 达及格线才收录；候选是章内更具体的条目时登记条目",
            "• 前两名分差过小：标记“存疑”，同时输出多个候选",
            "• 一条上游可被多条下游继承（最多三个来源）",
            "• 全部不过线：判“来历不明”，附参考候选提请人工",
        ],
        "chip_c": "#ED7D31", "body_c": "#FCE4D6", "card_h": 1.02,
    },
    {
        "chip": "第六步 · 登记输出",
        "body": "以编号或章节号登记关系，交由原有引擎逐句着色",
        "card": [
            "• 关系确定后才用编号或章节号“登记门牌”",
            "• 交原有引擎逐句着色：绿＝完全一致　蓝＝语义相近　黑＝对不上",
            "• 混合模式：与文档自带追踪表互相校对，发现漏报与错报",
        ],
        "chip_c": "#70AD47", "body_c": "#E2EFDA", "card_h": 0.90,
    },
]

TITLE = "候选追踪关系发现——算法与流程总览"
SUBTITLE = "纯内容匹配（词汇 · 语义 · 章节结构）　｜　编号与章节号仅作登记索引　｜　不确定即标存疑、多给候选"
PRINCIPLE = ("设计三原则：① 匹配只看内容，三路证据交叉印证　"
             "② 编号与章节号仅作登记，绝不参与匹配判断　"
             "③ 宁可存疑多给候选，把精确裁决交给着色验证与人工")

# 版式常量
STAGE_W = 3.5
CX = 2.25
CHIP_H = 0.30
BODY_H = 0.55
TOPS = [7.02 - i * 1.13 for i in range(6)]
CARD_L = 4.6
CARD_W = 6.5
CARD_CX = CARD_L + CARD_W / 2


def build_shapes():
    shapes = []
    sid = 1

    # 标题横幅 + 副标题
    shapes.append(rect_shape(sid, PAGE_W / 2, 7.88, 10.9, 0.46, TITLE,
                             "#4472C4", "#4472C4", "#FFFFFF", 14, bold=True,
                             align=1, rounding=0.06, weight_pt=1.0))
    sid += 1
    shapes.append(rect_shape(sid, PAGE_W / 2, 7.55, 10.9, 0.26, SUBTITLE,
                             "#FFFFFF", "#FFFFFF", "#595959", 9, bold=False,
                             fill_pattern=0, line_pattern=0, align=1))
    sid += 1

    for i, st in enumerate(STAGES):
        top = TOPS[i]
        chip_cy = top - CHIP_H / 2
        body_cy = top - CHIP_H - BODY_H / 2
        mid = top - (CHIP_H + BODY_H) / 2

        # 标题条
        shapes.append(rect_shape(sid, CX, chip_cy, STAGE_W, CHIP_H, st["chip"],
                                 st["chip_c"], st["chip_c"], "#FFFFFF", 10,
                                 bold=True, align=1, rounding=0.03, valign=1))
        sid += 1
        # 内容框
        shapes.append(rect_shape(sid, CX, body_cy, STAGE_W, BODY_H, st["body"],
                                 st["body_c"], st["chip_c"], "#1F3864", 8.5,
                                 align=0, valign=1, rounding=0.03))
        sid += 1
        # 说明卡片
        card_cy = mid
        shapes.append(rect_shape(sid, CARD_CX, card_cy, CARD_W, st["card_h"],
                                 "\n".join(st["card"]), "#F7F7F7", "#BFBFBF",
                                 "#333333", 8, align=0, valign=1, rounding=0.03,
                                 weight_pt=0.75))
        sid += 1
        # 阶段 → 卡片 虚线
        shapes.append(line_shape(sid, CX + STAGE_W / 2, mid, CARD_L, mid,
                                 color="#999999", weight_pt=0.75,
                                 arrow_end=0, pattern=2))
        sid += 1
        # 阶段 → 下一阶段 箭头
        if i < len(STAGES) - 1:
            nxt = TOPS[i + 1]
            shapes.append(line_shape(sid, CX, top - CHIP_H - BODY_H, CX, nxt,
                                     color=st["chip_c"], weight_pt=1.75,
                                     arrow_end=13, pattern=1))
            sid += 1

    # 底部原则条
    last_bottom = TOPS[5] - CHIP_H - BODY_H
    strip_cy = last_bottom - 0.27
    shapes.append(rect_shape(sid, PAGE_W / 2, strip_cy, 10.9, 0.40, PRINCIPLE,
                             "#FFF2CC", "#BF9000", "#7F6000", 8.5, bold=False,
                             align=1, rounding=0.03, weight_pt=0.75))
    sid += 1
    return shapes, sid - 1


# ------------------------------------------------------------
# vsdx 包各部件
# ------------------------------------------------------------

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>
<Override PartName="/visio/pages/pages.xml" ContentType="application/vnd.ms-visio.pages+xml"/>
<Override PartName="/visio/pages/page1.xml" ContentType="application/vnd.ms-visio.page+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" Target="visio/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/pages" Target="pages/pages.xml"/>
</Relationships>"""

PAGES_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/page" Target="page1.xml"/>
</Relationships>"""

DOCUMENT_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<VisioDocument xmlns="http://schemas.microsoft.com/office/visio/2012/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xml:space="preserve">
<DocumentSettings TopPage="0" DefaultTextStyle="0" DefaultLineStyle="0" DefaultFillStyle="0" DefaultGuideStyle="0">
<Cell N="DocLangID" V="2052"/>
</DocumentSettings>
<FaceNames>
<Face ID="1" NameU="Microsoft YaHei" Name="Microsoft YaHei" CharSets="421885 0" Panose="020B0503020404020204" Flags="579"/>
</FaceNames>
<StyleSheets>
<StyleSheet ID="0" NameU="No Style" Name="No Style">
<Cell N="EnableLineProps" V="1"/><Cell N="EnableFillProps" V="1"/><Cell N="EnableTextProps" V="1"/>
<Section N="Character">
<Row IX="0">
<Cell N="Font" V="1"/><Cell N="AsianFont" V="1"/><Cell N="ComplexScriptFont" V="1"/><Cell N="Color" V="#000000"/><Cell N="Style" V="0"/><Cell N="Size" V="0.125"/>
</Row>
</Section>
<Section N="Paragraph">
<Row IX="0"><Cell N="HorzAlign" V="1"/></Row>
</Section>
<Section N="Fill">
<Row IX="0"><Cell N="FillForegnd" V="#FFFFFF"/><Cell N="FillPattern" V="1"/></Row>
</Section>
<Section N="Line">
<Row IX="0"><Cell N="LineWeight" V="0.01041666666666667"/><Cell N="LineColor" V="#000000"/><Cell N="LinePattern" V="1"/></Row>
</Section>
</StyleSheet>
</StyleSheets>
</VisioDocument>"""

PAGES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Pages xmlns="http://schemas.microsoft.com/office/visio/2012/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xml:space="preserve">
<Page ID="0" NameU="流程图" Name="流程图" ViewScale="0.9" ViewCenterX="{num(PAGE_W / 2)}" ViewCenterY="{num(PAGE_H / 2)}">
<PageSheet LineStyle="0" FillStyle="0" TextStyle="0">
<Cell N="PageWidth" V="{num(PAGE_W)}"/>
<Cell N="PageHeight" V="{num(PAGE_H)}"/>
<Cell N="ShdwOffsetX" V="0.125"/><Cell N="ShdwOffsetY" V="-0.125"/>
<Cell N="PageScale" V="1"/><Cell N="DrawingScale" V="1"/>
<Cell N="DrawingSizeType" V="3"/><Cell N="DrawingScaleType" V="0"/>
<Cell N="InhibitSnap" V="0" U="BOOL"/><Cell N="UIVisibility" V="0"/>
<Cell N="ShdwType" V="0"/><Cell N="ShdwScaleFactor" V="1"/>
<Cell N="DrawingResizeType" V="2"/>
</PageSheet>
<Rel r:id="rId1"/>
</Page>
</Pages>"""


def build_vsdx():
    shapes, n = build_shapes()
    page1 = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xml:space="preserve">
<Shapes>
{''.join(shapes)}
</Shapes>
</PageContents>"""
    os.makedirs(OUT_DIR, exist_ok=True)
    with zipfile.ZipFile(VSDX_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("visio/document.xml", DOCUMENT_XML)
        z.writestr("visio/_rels/document.xml.rels", DOC_RELS)
        z.writestr("visio/pages/pages.xml", PAGES_XML)
        z.writestr("visio/pages/_rels/pages.xml.rels", PAGES_RELS)
        z.writestr("visio/pages/page1.xml", page1)
    return n


# ------------------------------------------------------------
# SVG 预览（同版式）
# ------------------------------------------------------------

def build_svg():
    S = 96  # 每英寸像素
    Wpx, Hpx = int(PAGE_W * S), int(PAGE_H * S)

    def X(x): return x * S

    def Y(y): return (PAGE_H - y) * S  # 原点翻转

    parts = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{Wpx}" height="{Hpx}" '
        f'viewBox="0 0 {Wpx} {Hpx}" font-family="微软雅黑, Microsoft YaHei, sans-serif">'
        '<defs><marker id="arr" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
        '<path d="M0,0 L10,4 L0,8 z" fill="#4472C4"/></marker></defs>'
        f'<rect width="{Wpx}" height="{Hpx}" fill="white"/>'
    )

    def text_block(cx_in, cy_in, w_in, lines, size_pt, color, bold=False,
                   align="middle"):
        fs = size_pt * S / 72
        lh = fs * 1.35
        x = X(cx_in)
        if align == "start":
            x = X(cx_in - w_in / 2) + 6
        y0 = Y(cy_in) - (len(lines) - 1) * lh / 2 + fs * 0.35
        w = 'bold' if bold else 'normal'
        for i, ln in enumerate(lines):
            parts.append(
                f'<text x="{x:.0f}" y="{y0 + i * lh:.0f}" font-size="{fs:.0f}" '
                f'fill="{color}" font-weight="{w}" text-anchor="{align}">{esc(ln)}</text>'
            )

    # 标题/副标题
    parts.append(f'<rect x="{X(PAGE_W/2-5.45):.0f}" y="{Y(7.88+0.23):.0f}" '
                 f'width="{10.9*S:.0f}" height="{0.46*S:.0f}" rx="6" '
                 f'fill="#4472C4"/>')
    text_block(PAGE_W / 2, 7.88, 10.9, [TITLE], 14, "#FFFFFF", bold=True)
    text_block(PAGE_W / 2, 7.55, 10.9, [SUBTITLE], 9, "#595959")

    for i, st in enumerate(STAGES):
        top = TOPS[i]
        chip_cy, body_cy = top - CHIP_H / 2, top - CHIP_H - BODY_H / 2
        mid = top - (CHIP_H + BODY_H) / 2
        l = CX - STAGE_W / 2
        # chip
        parts.append(f'<rect x="{X(l):.0f}" y="{Y(chip_cy+CHIP_H/2):.0f}" '
                     f'width="{STAGE_W*S:.0f}" height="{CHIP_H*S:.0f}" rx="3" '
                     f'fill="{st["chip_c"]}"/>')
        text_block(CX, chip_cy, STAGE_W, [st["chip"]], 10, "#FFFFFF", bold=True)
        # body
        parts.append(f'<rect x="{X(l):.0f}" y="{Y(body_cy+BODY_H/2):.0f}" '
                     f'width="{STAGE_W*S:.0f}" height="{BODY_H*S:.0f}" rx="3" '
                     f'fill="{st["body_c"]}" stroke="{st["chip_c"]}"/>')
        text_block(CX, body_cy, STAGE_W - 0.12, st["body"].split("，"), 8.5, "#1F3864")
        # card
        ch = st["card_h"]
        parts.append(f'<rect x="{X(CARD_L):.0f}" y="{Y(mid+ch/2):.0f}" '
                     f'width="{CARD_W*S:.0f}" height="{ch*S:.0f}" rx="3" '
                     f'fill="#F7F7F7" stroke="#BFBFBF"/>')
        text_block(CARD_CX, mid, CARD_W, st["card"], 8, "#333333", align="start")
        # 虚线
        parts.append(f'<line x1="{X(CX+STAGE_W/2):.0f}" y1="{Y(mid):.0f}" '
                     f'x2="{X(CARD_L):.0f}" y2="{Y(mid):.0f}" stroke="#999999" '
                     f'stroke-dasharray="4 3"/>')
        # 箭头
        if i < len(STAGES) - 1:
            nxt = TOPS[i + 1]
            parts.append(f'<line x1="{X(CX):.0f}" y1="{Y(top-CHIP_H-BODY_H):.0f}" '
                         f'x2="{X(CX):.0f}" y2="{Y(nxt):.0f}" stroke="{st["chip_c"]}" '
                         f'stroke-width="2.2" marker-end="url(#arr)"/>')

    last_bottom = TOPS[5] - CHIP_H - BODY_H
    strip_cy = last_bottom - 0.27
    parts.append(f'<rect x="{X(PAGE_W/2-5.45):.0f}" y="{Y(strip_cy+0.20):.0f}" '
                 f'width="{10.9*S:.0f}" height="{0.40*S:.0f}" rx="3" '
                 f'fill="#FFF2CC" stroke="#BF9000"/>')
    text_block(PAGE_W / 2, strip_cy, 10.9, [PRINCIPLE], 8.5, "#7F6000")

    parts.append("</svg>")
    with open(SVG_PATH, "w", encoding="utf-8") as f:
        f.write("".join(parts))


if __name__ == "__main__":
    n_shapes = build_vsdx()
    build_svg()
    print(f"OK: {VSDX_PATH}")
    print(f"OK: {SVG_PATH}")
    print(f"形状总数: {n_shapes}")
