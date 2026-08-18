# 实施计划 (plan.md)

> 按 Superpowers 方法论分解。每项任务含文件路径与验证方式。
> 开发顺序遵循层间依赖：骨架 → 样本 → L0/L1 → L2 → L3(a→d) → L4/CLI → 全量验证。

## P1 项目骨架与依赖（已完成）
- [x] 目录：src/pdfparser、tests、samples、output
- [x] venv：~/.workbuddy/binaries/python/envs/default（Python 3.13.12）
- [ ] 依赖安装完成确认（pymupdf/pdfminer.six/pdfplumber/pandas/numpy/opencv/reportlab/pytest；camelot-py 视 Ghostscript 决定）
- [ ] `models.py`：数据模型 dataclass
- [ ] `__init__.py` 包导出

## P2 合成样本生成器
- [ ] `tests/samplegen.py`：reportlab 生成 8 类难点 PDF（samples/）
  - 样本 A：多级标题 + 图文插接 + 序号/圆点列表 + 页眉页脚
  - 样本 B：有线表格（含合并格、表头）
  - 样本 C：无线表格（空格对齐）
  - 样本 D：跨页接续表 + 多栏页
  - 样本 E：乱序内容流（人为打乱绘制顺序）
- [ ] 验证：生成后人工/程序抽查关键特征存在

## P3 L0 + L1
- [ ] `preprocess.py`：文本型/扫描型判别（字符密度）、加密/损坏检测
- [ ] `extract.py`：TextLine/ImageBlock/VectorLine 提取 + paint_order（内容流操作符计数）
- [ ] 测试：样本 E 验证 paint_order 与 get_text 顺序差异可捕获

## P4 L2 版面分析
- [ ] `layout.py`：行合并、段合并（1.6×行高阈值）、块分类（heading/body/table/image/caption/header_footer）、列检测（X 投影聚类）、页眉页脚（跨页重复）
- [ ] 测试：页眉页脚正确剔除；表格区域不吸入正文

## P5 L3a 阅读顺序 + 标题树
- [ ] `order.py`：块重叠 DAG 拓扑排序，tiebreak=paint_order；列内排序后按列拼接
- [ ] `headings.py`：字体签名聚类（正文基线=众数）、标题 5 重条件判定、层级栈归属算法
- [ ] 测试：标题不吸入正文；多级标题层级正确

## P6 L3b 列表识别
- [ ] `lists.py`：标记模式库（数字/字母/圈号/符号）、缩进聚类、连续性校验、悬挂缩进、嵌套树
- [ ] 测试：列表层级正确；普通行不误判

## P7 L3c 表格重建
- [ ] `tables.py`：camelot 探测与优先 → pdfplumber 兜底（lattice: 线检测网格；stream: X 直方图聚类）
- [ ] 合并单元格恢复（spanning：线缺失+文本延伸证据）
- [ ] 跨页接续表拼接（表头重复检测 + 列对齐校验）、表头识别
- [ ] 结构校验报告（行列一致、空单元格占比、悬空文本）
- [ ] 测试：有线/无线/合并格/跨页四类表格单元格级断言

## P8 L3d 媒体 + L4 输出 + CLI
- [ ] `media.py`：图片资产导出（xref 原始数据）、图题关联（图X 最近邻）、正文锚点
- [ ] `output.py`：JSON Schema 输出、Markdown 渲染（#/表格/![图]/列表）
- [ ] `document.py`：DocumentParser 管道编排（L0→L4）
- [ ] `cli.py`：`python -m pdfparser.cli input.pdf -o output/`（可选 --format json/md/both）
- [ ] 测试：端到端跑通 5 个样本

## P9 全量验证与收尾
- [ ] pytest 全量回归通过
- [ ] 合成样本指标统计（召回/准确率/F1）
- [ ] 代码审查 → review.md
- [ ] final_report.md + README
- [ ] 记忆沉淀（可复用工作流）
