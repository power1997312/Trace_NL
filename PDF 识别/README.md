# pdfparser —— 工程级 PDF 结构化提取工具

**不依赖 OCR 与大模型**，仅解析 PDF 内部结构（对象层/内容流层/几何层），
从复杂工程文档（含中文）中完整提取并重建：文字、章节层级、列表（序号/短线/圆点）、
各式表格（有线/无线/合并格/跨页）、图片（图题与正文锚点），输出 JSON + Markdown。

## 快速开始

```bash
# 解析单个 PDF（默认输出 JSON + Markdown + 图片资产）
python -m pdfparser.cli samples/sample_a.pdf -o output/sample_a

# 仅输出 JSON
python -m pdfparser.cli samples/sample_b.pdf -o output/ --format json
```

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install pymupdf pdfminer.six pdfplumber pandas numpy opencv-python-headless
# 国内镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 表格引擎自动降级：本机装有 Ghostscript 时自动启用 camelot（lattice/stream 双模式，
> 准确率标杆）；未装时自动降级为 pdfplumber + 自研网格重建（本仓库全部样本
> 在降级模式下通过）。

## 作为库使用

```python
from pdfparser import DocumentParser

parser = DocumentParser("工程文档.pdf", asset_dir="assets/")
result = parser.parse()
result.to_dict()          # JSON 化结果
result.outline            # 章节大纲 [(level, text, page)]
for blk in result.body:   # 顶层块（标题下挂 children）
    print(blk.type, blk.text[:40])
```

## 五层管道架构

```
L4 输出层      JSON（type/level/bbox/page/order）| Markdown | 图片资产
L3 结构重建    阅读顺序 · 标题层级树 · 列表树 · 表格网格 · 图文锚定
L2 版面分析    行→段→块合并 · 块分类 · 列检测 · 页眉页脚
L1 原子提取    TextLine/ImageBlock/VectorLine + 字体·坐标·paint_order
L0 预处理      文本型 vs 扫描型判别 · 加密检测
```

## 覆盖能力（合成样本验证，pytest 9/9 通过）

| 难点 | 样本 | 验证结果 |
|------|------|----------|
| 多级标题层级 | sample_a | 11 条大纲，层级正确 |
| 标题吸入正文 | sample_a | 层级栈归属，正文不混入标题 |
| 图文插接续接文字 | sample_a | 图前文→图题→图→图题→图后文 结构保留 |
| 序号/圆点/短横线列表 | sample_a | 5 组列表区域，引导句保留 |
| 页眉页脚剔除 | sample_a | "内部资料/第X页" 不入正文 |
| 有线表格（合并格） | sample_b | 2 表，rowspan 识别 |
| 无线表格 | sample_c | 2 表 5x4/4x4，文本完整 |
| 跨页接续表 | sample_d | 2 页合并为 71 行，表头去重 |
| 乱序内容流 | sample_e | 阅读顺序几何修复 |

## 已知边界

- **扫描版 PDF**：L0 判别为"无文本层"，明确报告需 OCR（本工具不提供 OCR）；
- 图片内像素文字：不可提取（保留图片资产与占位）；
- 旋转/竖排文字：当前标记不做转置；
- 复杂数学公式：仅近似文本；
- 超不规则表格（全合并异形表）：自动重建可能失败，输出结构校验 warnings。

## 目录结构

```
src/pdfparser/      核心库（11 个模块）
  preprocess.py     L0
  extract.py        L1
  layout.py         L2
  order.py          L3 阅读顺序
  headings.py       L3 标题层级
  lists.py          L3 列表
  tables.py         L3 表格（双引擎+合并格+跨页）
  media.py          L3 图文锚定
  document.py       管道编排
  output.py         L4 渲染
  cli.py            命令行
tests/              pytest 回归 + 合成样本生成器（samplegen.py）
samples/            5 个合成测试样本
design.md / plan.md / review.md / final_report.md   开发过程文档
```
