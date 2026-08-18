"""合成测试样本生成器（golden 样本集）。

覆盖 8 类解析难点：
- sample_a: 多级标题 + 图文插接（图后接续文字）+ 序号/圆点/短横线列表 + 页眉页脚
- sample_b: 有线表格（含合并单元格 rowspan/colspan、表头、多表）
- sample_c: 无线表格（空格对齐、无框线）
- sample_d: 跨页接续表（第二页重复表头）+ 两栏页
- sample_e: 乱序内容流（canvas 先画底部正文、后画顶部标题）→ 验证 paint_order

运行：python tests/samplegen.py [输出目录，默认 samples/]
依赖：reportlab、pymupdf
"""
from __future__ import annotations

import os
import sys

import fitz  # PyMuPDF（生成图片资产）

# ---------------------------------------------------------------------------
# 图片资产（供图文插接样本）
# ---------------------------------------------------------------------------
def _make_logo_png(path: str, w: int = 160, h: int = 120) -> str:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    pix.clear_with(230)
    for y in range(h):
        for x in range(w):
            if ((x // 40) + (y // 40)) % 2 == 0:
                pix.set_pixel(x, y, (70, 110, 190))
            else:
                pix.set_pixel(x, y, (220, 230, 245))
    pix.save(path)
    return path


# ---------------------------------------------------------------------------
# sample_a：标题树 + 图文插接 + 列表 + 页眉页脚
# ---------------------------------------------------------------------------
def gen_sample_a(out: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Image as RLImage, PageBreak)
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    path = os.path.join(out, "sample_a.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=25 * mm, rightMargin=25 * mm,
                            topMargin=22 * mm, bottomMargin=22 * mm)

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=10.5, leading=15)
    h1 = ParagraphStyle("h1", parent=body, fontSize=18, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=body, fontSize=14, spaceBefore=12, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=body, fontSize=12, spaceBefore=8, spaceAfter=4)

    logo = _make_logo_png(os.path.join(out, "logo_a.png"))

    story = [
        Paragraph("核电仪控系统验证与确认实施方案", h1),
        Spacer(1, 6),
        Paragraph("1. 系统概述", h2),
        Paragraph("1.1 背景", h3),
        Paragraph("本方案面向核电仪控系统（DCS/PLC）的验证与确认（V&V）工作，"
                  "覆盖需求分析、设计审查、代码走查与动态测试各环节。"
                  "文档依据 NBT 20448 系列标准编制。", body),
        Spacer(1, 6),
        Paragraph("图1-1 系统总体架构如下所示。", body),
        RLImage(logo, width=80 * mm, height=60 * mm),
        Paragraph("图1-1 系统总体架构", body),
        Spacer(1, 6),
        Paragraph("如上图所示，系统采用三层架构：现场层、控制层与监控层。"
                  "现场层负责信号采集与执行机构驱动，控制层完成逻辑运算与联锁保护，"
                  "监控层提供人机交互与历史数据存储。三层之间通过冗余总线互联，"
                  "任一单点故障均不影响系统整体功能。", body),
        Paragraph("1.2 适用范围", h2),
        Paragraph("本方案适用于以下对象：", body),
        Paragraph("1) 反应堆保护系统（RPS）；", body),
        Paragraph("2) 专设安全设施驱动系统（ESFAS）；", body),
        Paragraph("3) 核仪表系统（RPN）；", body),
        Paragraph("4) 过程控制系统（DCS）。", body),
        Paragraph("主要工作内容包括：", body),
        Paragraph("■ 需求可追溯性分析；", body),
        Paragraph("■ 设计验证与评审；", body),
        Paragraph("■ 代码静态分析与走查。", body),
        Paragraph("注意事项：", body),
        Paragraph("- 所有验证活动需形成书面记录；", body),
        Paragraph("- 变更需经配置管理委员会评审；", body),
        Paragraph("- 验证结论应支持独立复核。", body),
        Paragraph("2. 验证方法", h2),
        Paragraph("2.1 静态验证", h3),
        Paragraph("静态验证包括代码走查、规则检查与文档审查，"
                  "不依赖目标机运行环境，可在开发阶段早期发现缺陷。", body),
        PageBreak(),
        Paragraph("2.2 动态测试", h3),
        Paragraph("动态测试在专用测试平台上执行，覆盖正常工况、边界条件与故障注入三类场景。"
                  "测试用例应具备可追溯性，并与需求条目建立双向链接。", body),
        Paragraph("3. 组织与职责", h2),
        Paragraph("3.1 验证组职责", h3),
        Paragraph("验证组独立于开发组，负责制定验证计划、执行验证活动并输出验证报告。", body),
        Paragraph("3.2 里程碑", h3),
        Paragraph("项目设置以下里程碑节点：", body),
        Paragraph("① 需求冻结评审；", body),
        Paragraph("② 设计完成评审；", body),
        Paragraph("③ 出厂验收测试；", body),
        PageBreak(),
        Paragraph("4. 文档管理", h2),
        Paragraph("所有验证文档纳入配置管理，按版本控制，变更记录需完整归档。"
                  "文档编号遵循项目配置管理规程。", body),
        Paragraph("4.1 文档清单", h3),
        Paragraph("— 验证与确认计划；", body),
        Paragraph("— 需求追溯矩阵；", body),
        Paragraph("— 测试报告与缺陷记录。", body),
    ]
    doc.build(story, onFirstPage=_draw_header_footer_a,
              onLaterPages=_draw_header_footer_a)
    return path


def _draw_header_footer_a(canvas, doc):
    from reportlab.lib.units import mm
    canvas.saveState()
    canvas.setFont("STSong-Light", 9)
    canvas.drawCentredString(doc.pagesize[0] / 2, doc.pagesize[1] - 14 * mm,
                             "内部资料 注意保存")
    canvas.drawCentredString(doc.pagesize[0] / 2, 12 * mm,
                             f"第 {doc.page} 页")
    canvas.restoreState()


# ---------------------------------------------------------------------------
# sample_b：有线表格（合并格、表头）
# ---------------------------------------------------------------------------
def gen_sample_b(out: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, PageBreak)
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = os.path.join(out, "sample_b.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=25 * mm, rightMargin=25 * mm,
                            topMargin=22 * mm, bottomMargin=22 * mm)
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=10.5, leading=15)
    h2 = ParagraphStyle("h2", parent=body, fontSize=14, spaceBefore=12, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=9, leading=12)

    def mk_cell(s): return Paragraph(s, cell)

    story = [
        Paragraph("设备清册", h2),
        Spacer(1, 6),
    ]
    # 表1：含合并单元格（colspan 表头 + rowspan 分类）
    t1 = Table([
        [mk_cell("序号"), mk_cell("设备名称"), mk_cell("规格型号"), mk_cell("数量"), mk_cell("备注")],
        [mk_cell("1"), mk_cell("安全级机柜"), mk_cell("QNA-2"), mk_cell("4"), mk_cell("冗余配置")],
        [mk_cell("2"), mk_cell("驱动单元"), mk_cell("DRV-1"), mk_cell("8"), mk_cell("")],
        [mk_cell("3"), mk_cell("人机界面"), mk_cell("HMI-9000"), mk_cell("2"), mk_cell("含工作站")],
        [mk_cell("4"), mk_cell("网络交换机"), mk_cell("SW-48"), mk_cell("6"), mk_cell("")],
    ], colWidths=[20 * mm, 40 * mm, 40 * mm, 20 * mm, 36 * mm])
    t1.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F5")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t1)
    story.append(Spacer(1, 10))
    # 表2：表头 colspan + 数据 rowspan（合并单元格）
    t2 = Table([
        [mk_cell("信号类型"), mk_cell("通道分配"), mk_cell("冗余方式"), mk_cell("备注")],
        [mk_cell("模拟量"), mk_cell("AI-01~16"), mk_cell("2oo4"), mk_cell("双重化")],
        [mk_cell(""), mk_cell("AI-17~32"), mk_cell("2oo4"), mk_cell("")],
        [mk_cell("开关量"), mk_cell("DI-01~64"), mk_cell("1oo2"), mk_cell("带自检")],
        [mk_cell(""), mk_cell("DO-01~32"), mk_cell("1oo2"), mk_cell("")],
    ], colWidths=[24 * mm, 44 * mm, 30 * mm, 38 * mm])
    t2.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F5")),
        ("SPAN", (0, 1), (0, 2)),     # 模拟量 跨两行
        ("SPAN", (0, 3), (0, 4)),     # 开关量 跨两行
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t2)
    doc.build(story)
    return path


# ---------------------------------------------------------------------------
# sample_c：无线表格（无框线、空格对齐）
# ---------------------------------------------------------------------------
def gen_sample_c(out: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = os.path.join(out, "sample_c.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=25 * mm, rightMargin=25 * mm,
                            topMargin=22 * mm, bottomMargin=22 * mm)
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=10.5, leading=15)
    h2 = ParagraphStyle("h2", parent=body, fontSize=14, spaceBefore=12, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=10, leading=13)

    def mk(s): return Paragraph(s, cell)

    story = [
        Paragraph("3. 参数汇总", h2),
        Paragraph("表3-1 主要设计参数（无框线排版）", body),
        Spacer(1, 4),
    ]
    t1 = Table([
        [mk("参数名称"), mk("单位"), mk("设计值"), mk("允许偏差")],
        [mk("反应堆功率"), mk("MWt"), mk("3000"), mk("±2%")],
        [mk("冷却剂温度"), mk("°C"), mk("310"), mk("±5")],
        [mk("稳压器压力"), mk("MPa"), mk("15.5"), mk("±0.2")],
        [mk("主给水流量"), mk("t/h"), mk("6800"), mk("±50")],
    ], colWidths=[52 * mm, 30 * mm, 30 * mm, 30 * mm])
    # 无框线表格：只有文本对齐，无任何线条
    t1.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t1)
    story.append(Spacer(1, 12))
    story.append(Paragraph("4. 环境条件", h2))
    story.append(Paragraph("表4-1 环境参数", body))
    Spacer(1, 4)
    t2 = Table([
        [mk("项目"), mk("正常工况"), mk("事故工况"), mk("基准地震")],
        [mk("环境温度"), mk("25°C"), mk("40°C"), mk("—")],
        [mk("相对湿度"), mk("50%"), mk("95%"), mk("—")],
        [mk("地震烈度"), mk("—"), mk("—"), mk("SSE")],
    ], colWidths=[42 * mm, 30 * mm, 30 * mm, 30 * mm])
    t2.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t2)
    doc.build(story)
    return path


def gen_sample_d(out: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, PageBreak)
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = os.path.join(out, "sample_d.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=25 * mm, rightMargin=25 * mm,
                            topMargin=22 * mm, bottomMargin=22 * mm)
    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=10.5, leading=15)
    h2 = ParagraphStyle("h2", parent=body, fontSize=14, spaceBefore=12, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontName="STSong-Light",
                          fontSize=9, leading=12)

    def mk_cell(s): return Paragraph(s, cell)

    story = [
        Paragraph("5. 信号清单", h2),
        Paragraph("表5-1 模拟量输入信号清单（跨页接续）", body),
        Spacer(1, 4),
    ]
    # 30 行数据 → 强制跨页，repeatRows=1 让第二页重复表头
    rows = [[mk_cell("序号"), mk_cell("信号名称"), mk_cell("量程"), mk_cell("通道"), mk_cell("精度")]]
    for i in range(1, 71):
        rows.append([mk_cell(str(i)), mk_cell(f"压力变送器 PT-{i:03d}"),
                     mk_cell("0~25MPa"), mk_cell(f"AI-{i:02d}"), mk_cell("0.1%")])
    t = Table(rows, colWidths=[16 * mm, 50 * mm, 28 * mm, 24 * mm, 18 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE6F5")),
    ]))
    story.append(t)
    doc.build(story)
    return path


# ---------------------------------------------------------------------------
# sample_e：乱序内容流（先画底部正文，后画顶部标题）
# ---------------------------------------------------------------------------
def gen_sample_e(out: str) -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    path = os.path.join(out, "sample_e.pdf")
    c = rl_canvas.Canvas(path, pagesize=A4)
    W, H = A4
    # ★ 故意乱序：先画底部正文（内容流在前），再画顶部标题（内容流在后）
    c.setFont("STSong-Light", 10.5)
    c.drawString(25 * mm, 60 * mm, "这是页面底部的正文内容，从内容流顺序看它最先被绘制。")
    c.drawString(25 * mm, 45 * mm, "它描述了系统维护与检修的基本要求。")
    c.setFont("STSong-Light", 12)
    c.drawString(25 * mm, 30 * mm, "6.1 检修周期：每年一次全面检修。")
    # 再画顶部标题（视觉第一，内容流最后）
    c.setFont("STSong-Light", 18)
    c.drawCentredString(W / 2, H - 30 * mm, "第六章 维护与检修")
    c.setFont("STSong-Light", 14)
    c.drawString(25 * mm, H - 50 * mm, "6. 维护与检修要求")
    c.showPage()
    c.save()
    return path


# ---------------------------------------------------------------------------
def gen_all(out: str) -> list[str]:
    os.makedirs(out, exist_ok=True)
    paths = [
        gen_sample_a(out),
        gen_sample_b(out),
        gen_sample_c(out),
        gen_sample_d(out),
        gen_sample_e(out),
    ]
    return paths


if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "samples"
    print("生成样本:", gen_all(out_dir))
