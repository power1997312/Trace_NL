# 工程级 PDF 结构化提取工具 —— 设计文档 (design.md)

> 项目路径：/Users/plankg/PDF识别 ｜ 语言：Python 3.13 ｜ 形态：CLI + 核心库
> 依据：《复杂工程PDF结构化提取研究报告》（2026-08-14，五层管道方案）
> 原则：不依赖 OCR 与大模型；只消费 PDF 对象层/内容流层/几何层信息

---

## 1. 目标与范围

### 目标
对**程序化生成**的工程级 PDF（含文本层），完整提取并重建：文字、章节层级、列表（序号/短线/圆点）、各式表格（含合并格与跨页）、图片（含图题与正文锚点），输出保留人眼视觉结构的 JSON + Markdown。

### 范围界定
- ✅ 文本型 PDF（Word/WPS/LaTeX/CAD 导出等）
- ❌ 扫描件（L0 判别后明确报告"需 OCR"，转出管道）
- ❌ 图片内像素文字（仅保留图片资产与占位）
- ❌ 复杂数学公式（近似文本，不做 LaTeX 还原）

## 2. 架构：五层管道

```
L4 输出层      JSON（type/level/bbox/page/order）| Markdown | 图片资产
L3 结构重建    阅读顺序 · 标题层级树 · 列表树 · 表格网格 · 图文锚定
L2 版面分析    行→段→块合并 · 块分类 · 列检测 · 页眉页脚
L1 原子提取    TextLine/ImageBlock/VectorLine + 字体·坐标·paint_order
L0 预处理      文本型vs扫描型 · 加密/损坏检测 · 规范化
```

层间单向依赖，每层输出可 dump 为 JSON 便于调试与回归。

## 3. 模块划分（src/pdfparser/）

| 模块 | 职责 |
|------|------|
| `preprocess.py` | L0：文档类型判别、加密检测 |
| `extract.py` | L1：PyMuPDF 原子元素提取 + paint_order |
| `layout.py` | L2：行/段/块合并、块分类、列检测、页眉页脚 |
| `order.py` | L3：阅读顺序（拓扑排序） |
| `headings.py` | L3：字体签名聚类、标题判定、层级栈归属 |
| `lists.py` | L3：列表标记识别、缩进层级、连续性校验 |
| `tables.py` | L3：camelot/pdfplumber 双引擎、合并格、跨页 |
| `media.py` | L3：图片资产、图题关联、正文锚点 |
| `document.py` | 管道编排（DocumentParser 主类） |
| `output.py` | L4：JSON/Markdown 渲染 |
| `cli.py` | 命令行入口 |
| `models.py` | 数据模型（dataclass + JSON Schema） |

## 4. 数据模型（核心）

```python
TextLine    text/bbox/font_name/font_size/bold/color/cid_font/paint_order/page/block_idx
ImageBlock  bbox/xref/paint_order/page/has_caption
VectorLine  kind(hline|vline|curve)/bbox/page/paint_order
Block       type(text|heading|list|table|image|caption|header_footer)/level/bbox/page/text/
            media[]/children[]/order/paint_order/spanning
Document    meta/outline[]/body[]
```

## 5. 关键技术决策（含理由）

1. **主引擎 PyMuPDF**：C 内核性能最好，`get_text("dict")` 提供块/行/span 层级与字体信息，`get_image_info` 提供图像位置。
2. **paint_order（内容流绘制序号）**：通过解析页面内容流操作符序列获取。解决"内容流顺序≠视觉顺序"与"图文插接后续接文字"问题（研究报告场景 A 的核心）。
3. **表格双引擎**：camelot（lattice/stream）优先——准确率标杆；运行时探测 Ghostscript，缺失则自动降级 pdfplumber + 自研行列聚类。**准确性优先原则**。
4. **标题防吸入**：全文档字体签名聚类确定正文基线 → 5 重条件 AND 判定 → **层级栈归属算法**（机制性杜绝，非启发式修补）。
5. **列表识别**：行首标记模式库 + 左边界缩进聚类 + **序号连续性校验**（防误判普通行为列表）。
6. **阅读顺序**：paint_order 基线 + 块重叠 DAG 拓扑排序 + 列分组。
7. **合成样本**：reportlab 生成覆盖 8 类难点的测试 PDF（多级标题、图文插接、序号/圆点列表、有线/无线/合并格/跨页表格、页眉页脚、多栏、乱序内容流）。
8. **测试**：pytest + golden 样本集；指标：正文召回、标题准确率、列表树准确率、表格单元格 F1、图片命中率。

## 6. 验证指标（合成样本）

| 指标 | 目标 |
|------|------|
| 正文文本召回率 | ≥99% |
| 标题层级准确率 | ≥95% |
| 标题归属错误率（吸入正文） | <2% |
| 列表项识别准确率 | ≥95% |
| 有线表格单元格 F1 | ≥90% |
| 无线表格单元格 F1 | ≥80% |
| 图片定位与图题关联命中率 | ≥90% |

## 7. 已知边界

- 扫描件需 OCR（L0 明确报告）
- 旋转/竖排文字需字形方向检测（L1 预留接口，先标记）
- 异形全合并表格自动重建可能失败 → 输出结构校验报告并标记人工复核
