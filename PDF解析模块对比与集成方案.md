# Trace_NL 项目 PDF 解析模块对比与集成方案

## 执行摘要

本报告深入分析了 Trace_NL 项目中两套 PDF 解析模块的架构差异：原始的 `core/pdf_parser.py`（面向追踪矩阵提取的专用工具）和新开发的 `PDF 识别/src/pdfparser/`（工程级五层管道结构化提取框架）。原始解析器采用 PyMuPDF+pdfplumber 双引擎，以"页"为单位提取纯文本和表格，通过正则清洗去除噪声；新解析器采用 L0-L4 五层管道架构，具备章节层级树、列表树、阅读顺序排序、paint_order、图片资产导出、合并格/跨页表格重建等原始模块不具备的结构化能力。两套模块的下游接口依赖清晰——`requirement_extractor.py` 仅依赖 `extract_full_text()` 返回的纯文本字符串，`traceability_matrix.py` 依赖 `extract_full_text()`、`find_traceability_table()`、`parse_traceability_table()` 等函数及 `TableData`、`TraceRelation` 两个 dataclass。集成方案采用适配器模式，在新模块的 `DocumentResult` 与原始接口之间建立转换层，保留原始解析器作为降级回退，实现零影响切换。

---

## 一、原始 PDF 解析算法分析（core/pdf_parser.py）

### 1.1 架构概述

原始解析器是一个约 1243 行的单文件模块，采用"双引擎"策略：PyMuPDF（fitz）负责正文文本提取，pdfplumber 负责表格提取。核心设计目标是服务于追踪矩阵生成场景，而非通用文档结构化提取。

### 1.2 数据模型

```python
@dataclass
class PageText:
    page_number: int
    text: str

@dataclass
class TableData:
    page_number: int
    headers: list[str]
    rows: list[list[str]]

@dataclass
class TraceRelation:
    downstream_id: str
    upstream_ref: str
    upstream_doc: str
```

数据模型非常简洁——`PageText` 仅含页码和文本，`TableData` 仅含页码、表头和行数据（均为纯字符串），没有坐标、字体、层级等结构信息。

### 1.3 核心能力

**正文提取（`extract_body_text`）**：
- 逐页调用 PyMuPDF 的 `get_text()` / `get_text("blocks")` / `get_text("words")`
- 通过 `_detect_page_regions` 检测非正文区域（位图/矢量图/表格），提取时跳过这些区域内的文字
- 矢量图检测采用"绘图覆盖率聚类"算法（24x24 网格，覆盖率阈值 0.10，连通域分组）
- 同页位图 ≥5 时自动合并为一张图（处理 Visio 图被拆成多块位图的情况）
- 区域外扩边距 15pt 纳入图标签

**文本清洗管道**（按顺序执行）：
1. `_filter_edge_doc_name_lines`：位置过滤，检测并移除页首/页尾的多行页眉页脚块
2. 7 种正则过滤：页眉页脚、纯页码行、文档名+页码行、密级标记、纯版本行、页码标签行、文档编号行
3. `_clean_pua_chars`：去除 PDF 私有区 Unicode 字符（U+F000-U+F8FF）
4. `_merge_intra_sentence_breaks`：合并句内换行（CJK/Latin/Digit 之间的硬换行），保留段落分隔、章节标题、列表项、条目 ID 行的换行
5. `_clean_cjk_spaces`：消除 CJK-Latin、Latin-Digit、列表编号后的 PDF 伪空格
6. `_clean_lonely_bullets`：清理孤立 bullet 碎片

**表格提取（`extract_tables`）**：
- 使用 pdfplumber 的 `extract_tables()` 方法
- 仅扫描最后 N 页（默认 5 页，追踪矩阵扫描 30 页）
- 跨页表格合并：表头归一化比较、续页表头降级为数据行、行数最多表优先

**追踪矩阵表查找与解析**：
- `find_traceability_table`：需求侧，表头含"条目号/编号"+"追踪/上游"关键词
- `find_traceability_table_design`：设计侧，表头含"设计条目号"+"需求条目号"
- `parse_traceability_table` / `parse_traceability_table_design`：智能匹配列索引，处理多值单元格（换行分隔），支持尖括号 ID 提取

**缓存系统**：
- 三级 LRU 缓存：文档级正文缓存（24 文档）、页级区域缓存（4000 页）、表格缓存
- 以 `(绝对路径, mtime_ns, 文件大小)` 为键，文件变更自动失效
- 线程安全（`threading.Lock`）
- `prefetch_pdfs` 支持并行预解析

### 1.4 局限性

| 局限 | 具体表现 |
|------|----------|
| 无章节层级树 | 无法输出文档大纲，章节信息仅通过正则从纯文本中后验提取 |
| 无列表结构识别 | 列表项仅作为普通文本行处理，无法识别嵌套列表 |
| 无阅读顺序排序 | 依赖 PyMuPDF 的 `get_text()` 默认顺序，遇到乱序内容流无法修复 |
| 无 paint_order | 无法区分"内容流顺序"与"视觉顺序"，图文插接场景文本可能错乱 |
| 无图片资产导出 | 仅检测图片区域位置用于排除文字，不提取图片本身 |
| 表格能力有限 | 仅支持 pdfplumber 的 `extract_tables()`，不支持合并格恢复、无线表检测、图表区域判别 |
| 矢量图检测粗糙 | 基于"绘图覆盖率"启发式，对复杂矢量图（如 CAD 导出的 SAMA/FD 图）检测精度不足 |
| 文本清洗与下游耦合 | `_merge_intra_sentence_breaks` 的换行合并规则被 `requirement_extractor` 深度依赖，改动风险高 |

---

## 二、新 PDF 解析模块分析（PDF 识别/src/pdfparser/）

### 2.1 架构概述

新模块是一个完整的工程级 PDF 结构化提取框架，采用五层管道架构（L0-L4），共 13 个 Python 文件，不依赖 OCR 与大模型，仅消费 PDF 对象层/内容流层/几何层信息。

### 2.2 五层管道

```
L4 输出层      JSON（type/level/bbox/page/order）| Markdown | 图片资产
L3 结构重建    阅读顺序 · 标题层级树 · 列表树 · 表格网格 · 图文锚定
L2 版面分析    行→段→块合并 · 块分类 · 列检测 · 页眉页脚
L1 原子提取    TextLine/ImageBlock/VectorLine + 字体·坐标·paint_order
L0 预处理      文本型 vs 扫描型判别 · 加密检测
```

### 2.3 数据模型

新模块的数据模型丰富得多，所有元素都保留 `(bbox, page, paint_order)` 三维定位信息：

```python
@dataclass
class TextLine:
    text, bbox, font_name, font_size, bold, italic, color,
    cid_font, paint_order, page, block_idx, line_idx

@dataclass
class Block:
    type  # text | heading | list | table | image | caption | header_footer | figure | consumed
    level, text, bbox, page, paint_order, order
    lines: list[TextLine]
    media: list[dict]           # 段落内嵌媒体锚点
    table: Optional[TableData]  # type==table 时
    children: list[Block]       # 层级栈归属的子块
    list_items: Optional[list[dict]]  # type==list 时的列表树
    image_info: Optional[dict]  # type==image/figure 时
    font_sig: Optional[tuple]   # (name, size, bold)

@dataclass
class TableCell:
    text, rowspan, colspan, bbox

@dataclass
class TableData:
    rows: list[list[TableCell]]
    header_rows, page_start, page_end
    flavor  # lattice | stream
    engine  # camelot | pdfplumber | custom
    bbox, warnings, columns

@dataclass
class DocumentResult:
    meta: dict
    outline: list[OutlineItem]  # 平铺大纲
    body: list[Block]           # 层级树顶层节点
    warnings: list[str]
```

### 2.4 核心能力（对比原始模块的增量）

| 能力 | 新模块实现 | 原始模块 |
|------|-----------|----------|
| **章节层级树** | `headings.py`：字体签名聚类确定正文基线 → 5 重条件 AND 判定 → 层级栈归属算法 → `build_tree()` 构建 Block 树 | 无（仅正则后验提取） |
| **大纲输出** | `OutlineItem` 列表，含 level/text/page | 无 |
| **列表识别** | `lists.py`：标记模式库（数字/字母/圈号/符号）+ 缩进聚类 + 序号连续性校验 + 嵌套树 | 无（作为普通文本） |
| **阅读顺序** | `order.py`：paint_order 基线 + 块重叠 DAG 拓扑排序 + 列分组 | 无（依赖 get_text 默认顺序） |
| **paint_order** | `extract.py`：通过 rawdict block 全局顺序近似内容流绘制序号 | 无 |
| **图片资产导出** | `media.py`：按 xref 提取原始图片数据 + 矢量图区域渲染为 PNG | 无（仅检测位置） |
| **图题关联** | `media.py`：最近邻图题配对 + 段落 media 锚点 | 无 |
| **表格双引擎** | `tables.py`：camelot（lattice/stream）优先 → pdfplumber 兜底 | 仅 pdfplumber `extract_tables()` |
| **合并格恢复** | `TableCell.rowspan/colspan` + 跨格证据推断 | 不支持 |
| **无线表检测** | `_stream_detect_page`：多字段行聚类 + 空白带分段 + 列对齐一致性校验 | 不支持 |
| **图表区域判别** | `_is_chart_area`：旋转/竖排文本占比 > 30% 判为图表坐标轴 | 不支持 |
| **跨页表格** | `_join_cross_page`：表头文本重复 + 列边界一致性（±3pt） | 表头归一化比较（无列边界校验） |
| **扫描型判别** | `preprocess.py`：字符密度检测，扫描件明确报告需 OCR | 无 |
| **结构校验** | `_validate`：行列一致性、空单元格占比、列边界对齐 warnings | 无 |
| **JSON/Markdown 输出** | `output.py`：结构化 JSON + Markdown 渲染 | 无（仅返回 dataclass） |

### 2.5 管道编排流程

```
L0 preprocess.analyze()           → 判别文本型/扫描型
L1 extract.extract()              → TextLine[] / ImageBlock[] / VectorLine[]
L2 layout.build_blocks()          → Block[]
L3c tables.extract_all()          → TableData[]
    tables.carve_blocks()         → 表格区域从正文流"挖出"
    figure-table 去重             → 与结构图重叠的表标记 consumed
L3a headings.assign_levels()      → Block.level 标注
L3b lists.identify()              → Block.type = "list" + list_items
L3  order.assign()                → Block.order 阅读顺序编号
L3  headings.build_tree()         → 层级树 (roots: list[Block])
L3  media.anchor()                → 图题配对 + 段落 media 锚点
L4  ImageExporter.export()        → 按 xref 提取图片资产
    ImageExporter.export_figures()→ 矢量图区域渲染为 PNG
L4  DocumentResult 组装           → meta + outline + body(roots) + warnings
```

关键编排决策：表格区域先于标题/列表/阅读顺序处理（"carve"挖出），保证表格不污染正文结构，同时保留"表格打断段落"的信息。

### 2.6 验证结果

新模块在合成样本和真实文档上均通过测试（30/30 通过），包括：多级标题层级、图文插接续接文字、序号/圆点/短横线列表、页眉页脚剔除、有线表格（含合并格）、无线表格、跨页接续表、乱序内容流、图片资产导出、结构图识别、矢量图识别、图表与表格区分、段落连贯性等。

---

## 三、下游模块接口依赖分析

### 3.1 requirement_extractor.py 的依赖

该模块不直接导入 `pdf_parser`，而是通过 `traceability_matrix.py` 间接获得 `extract_full_text()` 的结果（全文字符串 `str`）。

**对文本格式的关键依赖**：

1. `detect_document_type(text)`：使用 `re.findall(ITEM_ID_REGEX, text)` 检测文档类型。依赖全文中尖括号 ID `<...>` 格式被正确保留。

2. `extract_requirement_items(text)`：
   - 用 `ITEM_ID_REGEX` 的 `finditer` 定位每个条目 ID
   - **关键依赖**：条目 ID 所在行与后续正文之间的换行必须保留（由 `_merge_intra_sentence_breaks` 中的 `re.search(ITEM_ID_REGEX, prev)` 守卫保证）
   - 内容截取从 ID 结束位置到下一个 ID 开始位置

3. `extract_sections(text)`：
   - 使用正则 `^(\d+(?:\.\d+)*\.?)\s*([\u4e00-\u9fffA-Z].*?)(?:\n|$)` 匹配章节标题
   - **关键依赖**：章节标题必须独占一行（MULTILINE 模式），且标题行不被合并到前一行

4. `_drop_entry_title_line(content)`：依赖条目号同行尾随标题与正文之间的 `\n` 分隔

**格式依赖总结**：
- `\n` 作为段落/条目/章节的边界标记
- 尖括号 ID `<...>` 结构完整
- 章节标题行保持独立（不被句内换行合并）
- CJK-Latin 伪空格已清除

### 3.2 traceability_matrix.py 的依赖

该模块直接导入 `pdf_parser` 的以下接口：

```python
from core.pdf_parser import (
    extract_full_text, find_traceability_table, parse_traceability_table,
    find_traceability_table_design, parse_traceability_table_design,
    prefetch_pdfs, TraceRelation,
)
```

| 调用点 | 函数 | 期望返回 | 格式依赖 |
|--------|------|----------|----------|
| `build_backward_matrix` | `prefetch_pdfs({ds: pdf, **upstream})` | None | 仅预热缓存 |
| | `find_traceability_table(downstream_pdf)` | `TableData \| None` | `headers: list[str]`, `rows: list[list[str]]` |
| | `parse_traceability_table(table)` | `list[TraceRelation]` | `.downstream_id`, `.upstream_ref`, `.upstream_doc` |
| | `extract_full_text(downstream_pdf)` | `str` | 传给 `extract_requirement_items()` |
| | `extract_full_text(pdf_path)` (每个上游) | `str` | 传给 `build_content_map()` |
| `build_backward_matrix_from_design` | `prefetch_pdfs({'系统需求': pdf, **design})` | None | 仅预热缓存 |
| | `find_traceability_table_design(pdf_path)` | `TableData \| None` | 同上 |
| | `parse_traceability_table_design(table)` | `list[TraceRelation]` | 同上 |
| | `extract_full_text(sys_req_pdf)` | `str` | 传给 `extract_requirement_items()` + `build_content_map()` |
| | `extract_full_text(pdf_path)` (每个设计) | `str` | 传给 `extract_requirement_items()` |

### 3.3 必须保持兼容的接口

| 接口 | 类型 | 必须兼容的原因 |
|------|------|---------------|
| `extract_full_text(pdf_path) -> str` | 函数 | `requirement_extractor` 和 `traceability_matrix` 直接依赖返回的纯文本字符串 |
| `extract_body_text(pdf_path) -> list[PageText]` | 函数 | `extract_full_text` 内部调用，部分脚本可能直接调用 |
| `PageText` (page_number, text) | dataclass | `extract_body_text` 的返回类型 |
| `TableData` (page_number, headers, rows) | dataclass | `find_traceability_table*` 的返回类型，`parse_traceability_table*` 的输入类型 |
| `TraceRelation` (downstream_id, upstream_ref, upstream_doc) | dataclass | `parse_traceability_table*` 的返回类型，`traceability_matrix` 直接访问字段 |
| `find_traceability_table(pdf_path) -> TableData \| None` | 函数 | `traceability_matrix` 直接调用 |
| `parse_traceability_table(table) -> list[TraceRelation]` | 函数 | `traceability_matrix` 直接调用 |
| `find_traceability_table_design(pdf_path, last_n_pages) -> TableData \| None` | 函数 | `traceability_matrix` 直接调用 |
| `parse_traceability_table_design(table, upstream_doc_name) -> list[TraceRelation]` | 函数 | `traceability_matrix` 直接调用 |
| `prefetch_pdfs(pdfs, max_workers) -> None` | 函数 | `traceability_matrix` 用于并行预热 |
| `clear_pdf_cache() -> None` | 函数 | 长驻服务可能调用 |

---

## 四、两套解析算法的差异对比

### 4.1 架构层面

| 维度 | 原始模块 | 新模块 |
|------|---------|--------|
| 设计目标 | 追踪矩阵提取专用 | 通用文档结构化提取 |
| 代码结构 | 单文件 1243 行 | 13 模块五层管道 |
| 数据模型 | 3 个简洁 dataclass（无坐标/字体信息） | 8+ 个 dataclass（含完整三维定位） |
| 输出格式 | dataclass 实例 | JSON + Markdown + 图片资产 |
| 可扩展性 | 低（功能内聚在单文件） | 高（层间单向依赖，每层可独立调试） |

### 4.2 文本提取能力

| 维度 | 原始模块 | 新模块 |
|------|---------|--------|
| 提取引擎 | PyMuPDF `get_text()` | PyMuPDF `get_text("dict")` + paint_order |
| 区域排除 | 位图/矢量图/表格区域检测后排除文字 | 表格"挖出"(carve) + figure 块标记 |
| 矢量图检测 | 绘图覆盖率聚类（24x24 网格） | 矢量线条分类 + figure 块 + 区域渲染 |
| 页眉页脚 | 7 种正则 + 位置过滤 | 跨页重复检测 + 行级识别 |
| 换行合并 | `_merge_intra_sentence_breaks`（CJK/Latin/Digit 规则） | `layout.py` 行→段合并（1.6×行高阈值） |
| CJK 空格 | 正则清洗（CJK-Latin/Latin-Digit/列表标记） | `_join_lines`（中文紧连，英文补空格） |
| 阅读顺序 | 依赖 get_text 默认顺序 | paint_order + DAG 拓扑排序 + 列分组 |

### 4.3 表格提取能力

| 维度 | 原始模块 | 新模块 |
|------|---------|--------|
| 引擎 | pdfplumber `extract_tables()` | camelot（优先）+ pdfplumber（兜底）+ 自研网格 |
| 有线表 | 支持 | 支持（含合并格恢复） |
| 无线表 | 不支持 | 支持（字段间隙聚类 + 空白带分段） |
| 合并格 | 不支持 | rowspan/colspan 推断 |
| 跨页合并 | 表头归一化比较 | 表头文本重复 + 列边界一致性（±3pt） |
| 图表判别 | 不支持 | 旋转/竖排文本占比检测 |
| 结构校验 | 不支持 | 行列一致性 + 空格占比 + warnings |
| 表头识别 | 关键词匹配 | 字体样式差异检测 |

### 4.4 结构化能力

| 维度 | 原始模块 | 新模块 |
|------|---------|--------|
| 章节层级 | 无（后验正则提取） | 字体签名聚类 + 层级栈归属算法 |
| 大纲输出 | 无 | `OutlineItem` 列表 |
| 列表识别 | 无 | 标记模式库 + 缩进聚类 + 连续性校验 + 嵌套树 |
| 图片资产 | 无 | xref 导出 + 矢量图渲染 + 图题关联 |
| 扫描型判别 | 无 | 字符密度检测 |
| 文档类型 | 无 | meta 信息（has_text_layer, text_chars 等） |

---

## 五、集成方案

### 5.1 集成策略：适配器模式 + 降级回退

核心思路：在新模块的 `DocumentResult` 与原始接口之间建立适配器层，使下游模块无需任何改动即可使用新解析器的结果。同时保留原始解析器作为降级回退，确保新模块解析失败时不影响现有功能。

### 5.2 集成架构

```
┌─────────────────────────────────────────────────────────┐
│                    下游模块（不改动）                      │
│  requirement_extractor.py  │  traceability_matrix.py    │
│  text_matcher.py           │  excel_generator.py        │
└───────────────┬───────────┴────────────┬────────────────┘
                │                        │
        extract_full_text()      find_traceability_table*()
        extract_body_text()      parse_traceability_table*()
                │                        │
┌───────────────▼────────────────────────▼────────────────┐
│              适配器层（新增 core/pdf_parser_adapter.py）    │
│                                                          │
│  ┌─────────────────────────────────────────────────┐    │
│  │  PDFParserBackend（抽象接口）                     │    │
│  │  - extract_full_text(pdf_path) -> str            │    │
│  │  - extract_body_text(pdf_path) -> list[PageText] │    │
│  │  - extract_tables(pdf_path, last_n_pages) -> ... │    │
│  │  - find_traceability_table*(...) -> TableData    │    │
│  │  - parse_traceability_table*(...) -> [TraceRel]  │    │
│  │  - prefetch_pdfs(...) / clear_pdf_cache()        │    │
│  └──────────┬──────────────────────┬───────────────┘    │
│             │                      │                     │
│  ┌──────────▼──────────┐ ┌────────▼──────────────┐     │
│  │ NewParserBackend    │ │ LegacyParserBackend   │     │
│  │ (新模块适配器)        │ │ (原始模块包装)         │     │
│  │                     │ │                       │     │
│  │ DocumentParser →    │ │ 直接调用原始           │     │
│  │ DocumentResult →    │ │ core/pdf_parser.py    │     │
│  │ 适配原始接口         │ │ 的函数                 │     │
│  └─────────────────────┘ └───────────────────────┘     │
└─────────────────────────────────────────────────────────┘
                │                        │
┌───────────────▼────────┐  ┌────────────▼──────────────┐
│ PDF 识别/src/pdfparser/ │  │ core/pdf_parser.py        │
│ (新五层管道模块)         │  │ (原始双引擎模块)           │
└────────────────────────┘  └───────────────────────────┘
```

### 5.3 适配器层设计（core/pdf_parser_adapter.py）

适配器层需要实现以下转换：

**转换 1：DocumentResult → list[PageText]（正文提取）**

将新模块的 `DocumentResult.body`（Block 层级树）展平为按页组织的纯文本，保持原始模块的文本格式约定：

```python
def _document_result_to_page_texts(result: DocumentResult) -> list[PageText]:
    """将 DocumentResult 转换为 list[PageText]，保持原始文本格式约定"""
    pages_dict = {}  # page_number -> list[str]

    def _walk_block(block: Block):
        # 跳过被吸收的块
        if block.type == "consumed":
            return
        # 页眉页脚不入正文
        if block.type == "header_footer":
            return

        page = block.page
        if page not in pages_dict:
            pages_dict[page] = []

        if block.type == "heading":
            # 章节标题独占一行（保持 \n 边界，供 extract_sections 使用）
            pages_dict[page].append(block.text.strip())
        elif block.type in ("text", "paragraph"):
            # 正文段落：使用 _join_lines 合并段内行
            text = _join_lines_compat(block.text)
            if text:
                pages_dict[page].append(text)
        elif block.type == "list":
            # 列表项：每项独占一行
            for region in (block.list_items or []):
                if region.get("type") == "intro":
                    pages_dict[page].append(region.get("text", ""))
                else:
                    _render_list_items_compat(region.get("items", []), pages_dict[page], 0)
        elif block.type == "table" and block.table is not None:
            # 表格区域：跳过（与原始模块行为一致——表格内文字不入正文）
            pass
        elif block.type in ("image", "figure"):
            # 图片区域：跳过（与原始模块行为一致）
            pass
        elif block.type == "caption":
            # 图题：保留为独立行
            if block.text.strip():
                pages_dict[page].append(block.text.strip())

        # 递归子块
        for child in block.children:
            _walk_block(child)

    for block in result.body:
        _walk_block(block)

    # 组装 PageText 列表（按页码排序）
    pages = []
    for pno in sorted(pages_dict.keys()):
        text = "\n".join(pages_dict[pno])
        pages.append(PageText(page_number=pno, text=text))
    return pages
```

**关键兼容性处理**：

- 章节标题独占一行（`\n` 边界），供 `extract_sections` 正则匹配
- 列表项每项独占一行，保持 `1) xxx` / `- xxx` 格式
- 表格/图片区域文字跳过（与原始模块的"区域排除"行为一致）
- CJK-Latin 空格处理采用与原始模块一致的规则（CJK 紧连，Latin 间补空格）
- 不做 `_merge_intra_sentence_breaks`（新模块的 `layout.py` 已在段落合并阶段处理了行内换行）

**转换 2：新 TableData → 原始 TableData（表格提取）**

```python
def _convert_table_data(new_td, page_number: int) -> TableData:
    """将新模块的 TableData 转换为原始 TableData 格式"""
    if not new_td.rows:
        return TableData(page_number=page_number, headers=[], rows=[])

    # 第一行作为表头
    header_row = new_td.rows[0]
    headers = [_clean_cell_compat(c.text) for c in header_row]

    # 其余行作为数据
    rows = []
    for row in new_td.rows[1:]:
        rows.append([_clean_cell_compat(c.text) for c in row])

    return TableData(
        page_number=page_number,
        headers=headers,
        rows=rows,
    )
```

**转换 3：追踪矩阵表查找与解析**

`find_traceability_table*` 和 `parse_traceability_table*` 的工作方式不变——它们接收 `TableData`（原始格式）并返回 `list[TraceRelation]`。适配器层只需：

1. 调用新模块的 `DocumentParser.parse()` 获取 `DocumentResult`
2. 从 `result.body` 中提取所有 `type == "table"` 的 Block
3. 将新 `TableData` 转换为原始 `TableData` 格式
4. 传入现有的 `find_traceability_table*` / `parse_traceability_table*` 函数

```python
def find_traceability_table(pdf_path: str) -> TableData | None:
    """使用新模块解析 PDF，然后用现有逻辑查找追踪矩阵表"""
    tables = _extract_all_tables_via_new_parser(pdf_path)
    # 复用原始模块的表头匹配逻辑
    return _find_trace_table_from_list(tables, is_design=False)

def find_traceability_table_design(pdf_path: str, last_n_pages: int = 30) -> TableData | None:
    tables = _extract_all_tables_via_new_parser(pdf_path)
    return _find_trace_table_from_list(tables, is_design=True)
```

**转换 4：缓存与预取**

适配器层维护自己的缓存，以 `(绝对路径, mtime_ns, 文件大小)` 为键，缓存 `DocumentResult` 解析结果。`prefetch_pdfs` 并行调用新模块的 `DocumentParser.parse()`。

### 5.4 后端选择策略

```python
class PDFParserBackend:
    """PDF 解析后端选择器"""

    def __init__(self, prefer_new: bool = True, fallback: bool = True):
        self.prefer_new = prefer_new
        self.fallback = fallback

    def _try_new_parser(self, pdf_path: str):
        """尝试使用新模块解析，失败时返回 None"""
        if not self.prefer_new:
            return None
        try:
            from pdfparser import DocumentParser
            parser = DocumentParser(pdf_path, prefer_camelot=False)
            result = parser.parse()
            if result.meta.get("has_text_layer", True):
                return result
            return None  # 扫描件，新模块无法处理
        except Exception:
            return None

    def extract_full_text(self, pdf_path: str) -> str:
        result = self._try_new_parser(pdf_path)
        if result is not None:
            pages = _document_result_to_page_texts(result)
            return "\n".join(p.text for p in pages)
        # 降级到原始模块
        if self.fallback:
            from core.pdf_parser import extract_full_text as _legacy
            return _legacy(pdf_path)
        raise RuntimeError(f"PDF 解析失败: {pdf_path}")
```

### 5.5 集成步骤

**阶段一：适配器层开发（不影响现有功能）**

1. 将 `PDF 识别/src/pdfparser/` 包复制到 `e:\Trace_NL\pdfparser\`（或通过 sys.path 配置）
2. 创建 `core/pdf_parser_adapter.py`，实现适配器层
3. 适配器层默认使用新模块，降级到原始模块
4. 不修改 `core/pdf_parser.py` 的任何现有代码

**阶段二：接口切换（可灰度）**

5. 在 `core/traceability_matrix.py` 中将 `from core.pdf_parser import ...` 改为 `from core.pdf_parser_adapter import ...`
6. 添加配置开关（环境变量 `TRACE_NL_PDF_BACKEND=new|legacy|auto`），默认 `auto`
7. 运行 `run_e2e_test.py` 和 `run_e2e_sd_test.py` 验证结果一致性

**阶段三：增强利用（可选）**

8. 利用新模块的章节层级树，增强 `extract_sections` 的准确性
9. 利用新模块的图片资产导出，在追踪矩阵中嵌入图片引用
10. 利用新模块的列表树，改善需求条目的列表项识别

### 5.6 风险评估与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 新模块文本格式与原始模块不一致 | `requirement_extractor` 的正则匹配可能失效 | 适配器层的 `_join_lines_compat` 严格复刻原始模块的 CJK-Latin 空格规则；`_document_result_to_page_texts` 保持章节标题独占行 |
| 新模块解析某些 PDF 失败 | 下游功能中断 | 降级回退到原始模块（`fallback=True`） |
| 新模块表格检测结果不同 | 追踪矩阵表可能找不到或找到不同的表 | 适配器层复用原始模块的 `find_traceability_table*` 表头匹配逻辑，仅替换表格提取引擎 |
| 新模块性能差异 | 批量处理时间变化 | 适配器层维护缓存；`prefetch_pdfs` 并行预解析；新模块在 3 页复杂文档上 0.2s |
| 新模块依赖冲突 | import 失败 | 新模块依赖（PyMuPDF/pdfplumber/pdfminer.six）与原始模块完全一致，无新增依赖 |
| 换行合并规则差异 | 条目 ID 行与正文的换行边界可能丢失 | 适配器层在 `_document_result_to_page_texts` 中确保 heading 块独占行；新模块的 `layout.py` 已在段落合并阶段处理行内换行 |

### 5.7 文本格式兼容性要点

以下是适配器层必须严格保证的文本格式约定（来源于 `requirement_extractor.py` 的依赖分析）：

1. **`\n` 作为段落/条目/章节边界**：每个 `Block` 的文本以 `\n` 分隔，不跨块合并
2. **章节标题独占一行**：`heading` 块的 text 单独成行，前后有 `\n`
3. **条目 ID 行与正文换行分离**：如果 ID 出现在某行末尾，下一行不与之合并（新模块的 `layout.py` 段落合并基于行高阈值，不会跨块合并，天然满足此约束）
4. **尖括号 ID `<...>` 结构完整**：新模块的 `TextLine.text` 保留原始文本，不做破坏性清洗
5. **CJK-Latin 伪空格清除**：适配器层的 `_join_lines_compat` 对 CJK-Latin 相邻词不插空格
6. **列表项格式**：保持 `1) xxx` / `- xxx` 格式，每项独占一行
7. **表格/图片区域文字不入正文**：与原始模块的"区域排除"行为一致

---

## 六、结论

原始 `core/pdf_parser.py` 是一个面向追踪矩阵提取的高效专用工具，在文本清洗和缓存机制上设计精良，但缺乏章节层级、列表结构、阅读顺序、图片资产等结构化能力。新 `PDF 识别/src/pdfparser/` 模块通过五层管道架构提供了完整的文档结构化提取能力，在表格检测（双引擎/合并格/无线表/跨页/图表判别）、标题层级（字体签名聚类/层级栈归属）、阅读顺序（paint_order/DAG 排序）等方面显著优于原始模块。

两套模块的下游接口依赖清晰且边界明确——`requirement_extractor.py` 仅依赖纯文本字符串，`traceability_matrix.py` 依赖 `extract_full_text()`、`find_traceability_table*()`、`parse_traceability_table*()` 及 `TableData`/`TraceRelation` 两个 dataclass。采用适配器模式可以在新模块的 `DocumentResult` 与原始接口之间建立转换层，实现零影响切换，同时保留原始模块作为降级回退。适配器层的核心工作是保证文本格式约定（`\n` 边界、章节标题独占行、CJK-Latin 空格规则、表格/图片区域文字排除）与原始模块一致，确保下游模块无需任何改动。

集成应分三阶段推进：先开发适配器层（不影响现有功能），再灰度切换接口（可回退），最后增强利用新模块的结构化能力。关键风险通过降级回退、复用表头匹配逻辑、严格文本格式兼容来缓解。

---

## 七、集成执行记录（2026-08-17）

### 7.1 已交付内容

| 交付物 | 说明 |
|--------|------|
| `pdfparser/` 包 | 新模块从 `PDF 识别/src/` 复制到项目根目录，可直接 `from pdfparser import DocumentParser` |
| `core/pdf_parser_adapter.py` | 适配器层（约600行），桥接 DocumentResult 与原始接口，含双层缓存和降级回退 |
| 导入切换 | `core/traceability_matrix.py`、`generate_forward_matrix.py`、`generate_forward_sd_matrix.py` 的导入改为 `core.pdf_parser_adapter` |
| `config.py` | 新增 `PDF_BACKEND` 配置项与说明 |
| `requirements.txt` | 补充新模块依赖说明 |

### 7.2 适配器层关键实现

1. **文本转换** `_document_result_to_page_texts`：Block 树 → 按页分组文本行，heading 独占行、list 每项独占行、table/image/figure 跳过（与原始"区域排除"行为一致）。
2. **表格转换** `_extract_tables_via_new_parser`：新 TableData（TableCell 网格）→ 原始 TableData（headers+rows），复用原始 `_clean_cell`。
3. **追踪表查找**：复用原始 `find_traceability_table*` 的表头匹配和跨页合并逻辑，仅替换表格提取引擎。
4. **双层缓存**：DocumentResult 缓存（解析结果）+ PageText 缓存（转换结果），`prefetch_pdfs` 同时预热两层。
5. **降级回退**：新模块解析失败/扫描件时自动回退原始模块；`find_traceability_table*` 未找到追踪表时也回退。

### 7.3 执行中发现并修复的问题

| 问题 | 现象 | 修复 |
|------|------|------|
| 页眉页脚残留 | 新模块跨页重复检测无法覆盖"页码每页不同"的场景 | 补充原始模块的 7 种正则过滤（`_HEADER_FOOTER_RE` 等） |
| 位置过滤误删正文 | `_filter_edge_doc_name_lines` 误删 ≤30 字符的短正文行 | 移除该调用（新模块已通过 header_footer 块分类处理） |
| prefetch 缓存未填充 | 元组解包顺序错误导致 `os.path.isfile("文档名")` 而非路径 | 修正为 `(p, name) for p, name in tasks` |
| 图片资产污染源目录 | 新模块默认导出图片到 PDF 同目录 `assets/` | `asset_dir` 重定向到 `output/pdf_assets/<文档名>/`，清理已污染目录 |

### 7.4 验证结果

**冒烟测试**（`_smoke_test.py`）：正文提取 16 页、全文条目 ID 12 个、表格 8 个、需求侧追踪表第 16 页 14 行、设计侧仪控报警 11 行/总体方案 19 行、prefetch 后二次调用 0.000s。

**端到端测试**（`run_e2e_test.py`）：
- 逆向矩阵 14 条关系，上游内容 2/14 为空，匹配 GREEN 9/14 (64%)，BLACK 5/14 (36%)
- 正向矩阵生成：2 文档 24 条目，有追踪 8，未追踪 16

**端到端测试**（`run_e2e_sd_test.py`）：
- 设计侧 41 条关系（仪控报警 11 + 总体方案 30），上游内容 0/41 为空
- 生成 `正向追踪矩阵_系统设计` 和 `追踪验证结果_系统设计` xlsx

**后端切换验证**：`auto`/`legacy`/`new` 三种模式均正常，legacy 行为与集成前一致，切换可靠。

### 7.5 章节增强验证结论（重要）

对比新旧章节提取（`DCS设备技术规格书.pdf`）：

| 管线 | 结果 |
|------|------|
| 旧正则 `extract_sections` | 15 个章节（含 3.1/3.2/3.2.2.1 等子章节），但标题与正文粘连、有重复 |
| 新模块 `outline` | 仅 3 个一级标题（1概述/2.协调和集成/3.总体功能要求），子章节与正文同字体签名未被标题检测捕获 |

**结论**：新模块的标题识别在此类文档上"召回不足"（准确率高但覆盖少），不宜替换现有的 `extract_sections` 正则管线。旧正则在追踪矩阵场景的章节号引用解析（如 "3.2.2.1F-SC1级要求"）上更适用。**建议保持现有章节提取管线不变**。

### 7.6 后续可选增强

1. 安装 Ghostscript 启用 camelot 表格引擎，提升有线表准确率
2. 将新模块的图片资产（`output/pdf_assets/`）整合到追踪矩阵 Excel 输出
3. 长期方案：为 `extract_sections` 引入"字体签名辅助"——结合旧正则的高召回与新模块的字体签名做交叉校验，但需在更大样本集上验证

### 7.7 中间产物调试机制（2026-08-17 新增）

为区分"解析问题"与"提取问题"，新增中间产物 dump 模块 `core/debug_dump.py`。解析与提取各阶段产物分目录输出到 `output/debug/<时间戳>/<文档名>/`，多次运行自动创建新时间戳目录便于前后对比。

**目录结构与排查指引**：

| 子目录 | 文件 | 内容 | 排查结论 |
|--------|------|------|----------|
| parse/ | document_result.json | 新模块完整解析结果（Block树/outline/tables/图片） | 看结构是否完整 |
| parse/ | document_result.txt | 块树缩进预览（章节/列表/表格结构一目了然） | 快速看结构 |
| parse/ | page_text.txt | 每页正文文本（页眉页脚过滤后） | **若此处缺文字 → 解析问题** |
| parse/ | tables.json | 全文档表格（页码/表头/行数/前3行） | 看表格提取是否完整 |
| parse/ | summary.json | 解析统计（页数/字符/块数/警告） | 快速概览 |
| extract/ | requirement_items.txt/.json | 需求条目提取结果（ID+内容） | **若ID缺失/内容截断 → 提取问题** |
| extract/ | sections.txt | 章节提取结果 | 看章节切分 |
| extract/ | content_map.json | 内容映射（by_id/by_section/by_heading） | 看引用解析映射 |
| extract/ | trace_table.json | 追踪矩阵表查找结果（表头+全部行） | 看表头匹配是否正确 |
| extract/ | trace_relations.json | 追踪关系（downstream/upstream） | 看关系解析 |
| extract/ | summary.json | 提取统计（关系数/条目数/矩阵行数/空内容数） | 快速概览 |

**排查方法**：
- 若 `page_text.txt` 中某页缺文字 → **解析问题**（看 document_result.txt 对应块是否被错误分类/跳过）
- 若 `page_text.txt` 文字完整但 `requirement_items.txt` 条目缺失 → **提取问题**（正则/切分逻辑）
- 若 `trace_table.json` 未生成或表头不对 → **表格提取/匹配问题**
- 若 `trace_relations.json` 关系数量不对 → **追踪表解析问题**

**开关控制**：
- `TRACE_NL_DUMP=0` 关闭（默认开启）
- `TRACE_NL_DUMP_DIR=<自定义目录>` 改变输出位置
- dump 均为写文件操作，不影响解析/提取逻辑，失败静默忽略

---

## References

1. 原始解析器源码：`e:\Trace_NL\core\pdf_parser.py`
2. 新解析器源码：`e:\Trace_NL\PDF 识别\src\pdfparser\`（13 个模块）
3. 需求提取模块：`e:\Trace_NL\core\requirement_extractor.py`
4. 追踪矩阵模块：`e:\Trace_NL\core\traceability_matrix.py`
5. 全局配置：`e:\Trace_NL\config.py`
6. 新模块设计文档：`e:\Trace_NL\PDF 识别\design.md`
7. 新模块最终报告：`e:\Trace_NL\PDF 识别\final_report.md`
8. 新模块实施计划：`e:\Trace_NL\PDF 识别\plan.md`
9. 新模块 README：`e:\Trace_NL\PDF 识别\README.md`
10. 新模块依赖清单：`e:\Trace_NL\PDF 识别\requirements.txt`
11. 中间产物 dump 模块：`e:\Trace_NL\core\debug_dump.py`
12. PDF 解析适配器：`e:\Trace_NL\core\pdf_parser_adapter.py`
