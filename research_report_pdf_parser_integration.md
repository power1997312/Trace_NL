# TRACE_NL 原 PDF 解析算法与新版 pdfparser 模块差异分析及集成方案

## 摘要

TRACE_NL 现有追踪匹配功能建立在"扁平纯文本流"之上：`core/pdf_parser.py` 用 PyMuPDF 逐页抽文本并清洗，`core/requirement_extractor.py` 用正则从纯文本中猜测条目与章节，表格仅用 pdfplumber 在最后 30 页定位追踪矩阵表。这套机制丢弃了 PDF 的结构信息——章节层级、列表、正文表格、图片全部被"负向剔除"，是导致段落、章节、表格、图片提取不完整的根本原因。新开发的 `PDF 识别/src/pdfparser` 是一个五层管道（L0 预处理 → L1 原子提取 → L2 版面分析 → L3 结构重建 → L4 输出），正向重建出标题层级树、列表树、二维表格网格、图片资产与阅读顺序，恰好补齐上述缺口。

两者不是替代关系，而是"结构化上游 + 纯文本下游"的接口失配。集成应遵循适配器模式：新增一层适配器，把 pdfparser 的结构化输出降维成现有函数签名所要求的纯文本与表格形态，并用开关控制、异常回退与基准回归三重手段保证原有匹配与需求识别功能零回归。本文给出可直接落地的三阶段集成路径与关键映射代码。

## 背景

TRACE_NL 的端到端链路为：从下游 PDF 的附录追踪表解析追踪关系（`find_traceability_table` / `find_traceability_table_design`），再分别对下游、上游 PDF 提取全文与条目内容（`extract_full_text` + `extract_requirement_items` / `extract_sections` / `build_content_map`），最后做微块级文本匹配（`text_matcher.match_text_pair`）并生成 Excel。整个下游对 PDF 的消费集中在少数几个接口：`extract_full_text`、`extract_body_text`、`extract_tables`、`find_traceability_table`、`find_traceability_table_design`、`parse_traceability_table`、`parse_traceability_table_design` 以及 `TraceRelation` / `TableData` 两个数据结构。因此"不影响原有功能"的约束可以精确锁定在这几个符号上。

## 原解析算法的实现与缺陷

`core/pdf_parser.py` 的核心是 `extract_body_text`。它用 `fitz.open` 打开 PDF，对每页先调 `_detect_page_regions` 检测"非正文区域"（位图、矢量图、表格），再用 `_extract_text_excluding_regions` 把这些区域内的文字剔除，最后依次执行页眉页脚正则过滤、私有区字符清理、句内换行合并、CJK-Latin 伪空格消除、孤立 bullet 清理。表格用 `extract_tables(pdf_path, last_n_pages)` 仅扫描最后 N 页（追踪表定位时为 30 页），依赖 pdfplumber 的 `extract_tables`。

这套设计的缺陷集中在三点。其一是"负向剔除"而非"正向重建"：图、表、页眉页脚内的文字被检测出来然后丢弃，正文里的表格（非附录追踪表）整体被剔除，图内文字也全部丢失，等于主动放弃了这些内容。其二是结构完全扁平化：章节层级、列表嵌套、图文关系在纯文本中荡然无存，`requirement_extractor.extract_sections` 只能靠 `_SECTION_HEADING_RE` 这类正则去"猜"章节号，遇到"3供货要求""3 供货要求"这种无点号编号、或标题与正文同字号的情况就会漏判、误判。其三是阅读顺序缺失：`extract_body_text` 按 PyMuPDF 的内容流顺序逐页取文本，多栏文档、乱序内容流下文字顺序会错乱，只能靠事后一堆启发式清洗（`_remove_arch_diagram_blocks`、`_remove_ascii_diagrams`、`_strip_trailing_headings` 等）补救，而这些清洗本身就是针对扁平文本噪声的补丁。

## 新版 pdfparser 模块的实现

`PDF 识别/src/pdfparser` 采用五层单向依赖管道。L0（`preprocess.py`）判别文本型/扫描型并检测加密。L1（`extract.py`）用 `page.get_text("rawdict")` 提取 `TextLine` / `ImageBlock` / `VectorLine`，并保留字体名、字号、加粗、颜色、CID 标记和 paint_order（内容流绘制序号）。L2（`layout.py`）做行→段→块合并、块分类、页眉页脚识别（行级跨页重复）。L3 依次做表格重建（`tables.py`：camelot 优先、pdfplumber 兜底，含合并格恢复与跨页拼接）、标题层级（`headings.py`：字体签名聚类 + 层级栈归属）、列表识别（`lists.py`）、阅读顺序恢复（`order.py`：块重叠 DAG 拓扑排序 + 列分组）、图文锚定与图片导出（`media.py`）。L4（`output.py`）渲染 JSON / Markdown。

它的输出 `DocumentResult` 包含 `meta`（页数、表格引擎、资产数）、`outline`（`[(level, text, page)]` 大纲）和 `body`（顶层 `Block` 列表，标题块下挂 children 子树）。每个 `Block` 带 type（text/heading/list/table/image/caption/figure/header_footer/consumed）、level、bbox、page、paint_order、order，表格块持有二维 `TableData` 网格（单元格含 colspan/rowspan）。这套结构正是原解析器所缺失的全部信息。

## 差异对比

| 维度 | 原解析器 | pdfparser | 对匹配的影响 |
|------|---------|-----------|-------------|
| 数据形态 | 扁平纯文本（每页一个字符串） | 结构化块树（标题/列表/表格/图片独立块） | 新引擎更完整 |
| 章节/标题 | 正则猜章节号，易漏易误 | 字体聚类 + 层级栈，输出层级树 | 新引擎更准 |
| 正文表格 | 仅最后 N 页找追踪表，其余表被剔除丢弃 | 全页双引擎检测 + 合并格 + 跨页 | 新引擎更全 |
| 图片 | 只定位不提取 | 按 xref 导出 + figure 区域渲染 PNG | 新引擎补齐图片 |
| 阅读顺序 | 内容流顺序，多栏/乱序会错 | paint_order + 拓扑排序 + 列分组 | 新引擎更正确 |
| 列表 | 无 | 标记模式 + 缩进 + 连续性校验 | 新引擎新增能力 |
| 页眉页脚 | 正则 + 位置启发式 | 行级跨页重复识别 | 相当，新引擎更稳 |
| 依赖 | PyMuPDF + pdfplumber | PyMuPDF + pdfplumber（camelot/opencv 可选降级） | 无新增硬依赖 |

关键在于：下游 `requirement_extractor` 和 `text_matcher` 都是**纯文本消费者**。它们只读取字符串内容（`ITEM_ID_REGEX` 尖括号 ID、章节号正则、短语切分），不感知文本的来源结构。这意味着只要新引擎能把结构化块按阅读顺序还原成"形态一致"的纯文本，现有的条目提取、章节提取、内容映射、微块匹配逻辑可以一字不改地复用。

## 集成方案

集成目标是：在保持 `extract_full_text`、`find_traceability_table`、`parse_traceability_table` 等既有符号签名不变的前提下，让解析底层可切换到 pdfparser，从而提升段落、章节、表格、图片的提取完整度。

### 总体原则：适配器模式 + 开关 + 回退

第一步，让 pdfparser 可被导入。推荐在 `config.py` 或适配模块内把 `PDF 识别/src` 加入 `sys.path`，然后 `from pdfparser import DocumentParser`，保持 `PDF 识别` 目录原样不复制；若希望与 Trace_NL 同仓管理，也可将 `src/pdfparser` 拷贝为项目根下的子包 `pdfparser/`。两种方式的核心路径均只依赖 PyMuPDF 与 pdfplumber，与 Trace_NL 现有环境完全一致，无新增依赖（camelot 与 opencv 仅在可选表格引擎启用时按需加载，缺失时自动降级，不影响功能）。

第二步，在 `config.py` 增加开关 `USE_STRUCTURED_PARSER = False`（默认关闭），并在适配层对 pdfparser 的解析失败与"扫描型 PDF"（`meta.has_text_layer == False`）两类情况自动回退到原始 `extract_body_text` 逻辑。默认关闭保证原有行为 100% 不变，灰度验证通过后再打开。

### 阶段一：低风险替换正文文本流

新增适配函数，把 `DocumentResult.body` 递归扁平化为纯文本。遍历时按阅读顺序（`body` 已按 page/paint_order 排序，`order` 字段亦已赋值），对 `heading` 块输出其文本（保留章节号前缀），对 `text`/`paragraph`/`caption` 块输出文本，对 `list` 块按 `list_items` 树展开为带编号的文本行，对 `table` 块可选地渲染为文本行或跳过，对 `image`/`figure`/`header_footer`/`consumed` 块跳过。产出的文本形态与原始 `extract_full_text` 一致，因此 `extract_requirement_items`、`extract_sections`、`build_content_map` 的正则仍能命中尖括号 ID 与章节号。

这一阶段立刻解决两类核心问题：阅读顺序恢复（多栏、乱序内容流不再错乱），以及段落完整性（pdfparser 的段合并 x0 容差 32pt 与跨页段落合并，避免了原逻辑把连贯段落拆成碎片、把跨页段落拆成两段）。用基准数据 `基准数据/Trace_Base.xlsx` 跑 `run_e2e_test.py` 即可验证匹配统计（GREEN/BLUE/BLACK 比例）是否退化。

### 阶段二：用结构化表格替换追踪表定位

原始 `find_traceability_table` / `find_traceability_table_design` 只在最后 30 页用 pdfplumber 找表，且 `parse_traceability_table` / `parse_traceability_table_design` 是纯逻辑（`TableData → list[TraceRelation]`），可完全复用。适配器只需做两件事：用 pdfparser 提取全文档表格；把 pdfparser 的二维 `TableData`（单元格含 colspan/rowspan）转换成原始 `TableData`（`headers: list[str]` + `rows: list[list[str]]`），转换时对合并格做展开（将合并单元格文本复制到其覆盖的所有基础格）。随后复用现有的 `_is_trace_header` / `_is_design_trace_header` 关键词判定即可。这一阶段比原始实现更全（覆盖全页表格而非仅末页），且 pdfparser 已内置跨页拼接与表头去重，简化了原逻辑中的三种跨页合并策略。

### 阶段三：直接消费标题树与图片资产（终极增强）

前两阶段已在不改动下游的前提下补齐大部分缺口。若需进一步根治"章节提取不完整"，可让 `requirement_extractor` 直接消费 `result.outline`（层级大纲）与 `body` 中的 heading 块，用其 children 子树精确划分每个章节/条目的正文范围，取代正则猜测。同时 `media.py` 导出的图片资产可用于补充"图片提取不完整"的诉求。这一阶段改动面最大，需要重点回归：`requirement_extractor` 中 `_remove_arch_diagram_blocks`、`_remove_ascii_diagrams`、`_strip_trailing_headings` 等清洗逻辑是建立在"扁平文本含噪声"假设上的，一旦上游改为结构化输入，这些补丁应逐步移除或降级为防御性空操作，否则可能误删正文。

## 结论

原解析器与 pdfparser 的根本差异在于"负向剔除的扁平文本"对"正向重建的结构化块树"。集成无需重写匹配与需求识别，只需在两者之间插入一个适配层，把结构化输出降维成既有接口所要求的形态。建议按三阶段推进：阶段一以开关 + 回退方式替换正文文本流，用基准数据回归锁定匹配质量；阶段二用全页结构化表格替换追踪表定位并复用既有解析函数；阶段三按需直消费标题树与图片资产。每阶段都保持既有函数签名不变、默认行为可回退，从而在补齐段落、章节、表格、图片提取的同时，确保匹配与需求识别功能零回归。

## 参考

- [core/pdf_parser.py](e:/Trace_NL/core/pdf_parser.py) — 原解析器（正文文本流 + 表格定位 + 追踪表解析）
- [core/requirement_extractor.py](e:/Trace_NL/core/requirement_extractor.py) — 条目/章节提取与内容映射
- [core/traceability_matrix.py](e:/Trace_NL/core/traceability_matrix.py) — 逆向追踪矩阵构建（PDF 消费主入口）
- [core/text_matcher.py](e:/Trace_NL/core/text_matcher.py) — 微块级文本匹配引擎
- [PDF 识别/src/pdfparser/document.py](e:/Trace_NL/PDF 识别/src/pdfparser/document.py) — 新版五层管道编排
- [PDF 识别/src/pdfparser/models.py](e:/Trace_NL/PDF 识别/src/pdfparser/models.py) — 新版结构化数据模型
- [PDF 识别/README.md](e:/Trace_NL/PDF 识别/README.md) — 新版模块说明与能力边界
