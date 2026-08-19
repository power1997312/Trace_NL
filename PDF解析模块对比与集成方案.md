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

## 八、优化执行记录（2026-08-18 ~ 08-19）

本节记录在后续验证与优化中发现的问题、根因与修复经验。所有修复均已提交（commit `fc7de55`）并推送至 GitHub，回归测试 `tests/test_cross_page_merge_regression.py`、`tests/test_list_structure_regression.py` 全部通过。

### 8.1 跨页段落合并 bug（2026-08-18）

**现象**：`DCS设备技术规格书.pdf` 第 1 页"3.2 系统和设备分级"、"3.2.1 系统和设备分级"、"3.2.2.1"等章节标题下被重复注入"质保等级:QA1"、"DCS 供货商可以采用同一平台..."两段内容（第 2 页顶部内容），5 个不同位置重复出现；且 3.2.2.1/3.2.2.2 缺少"在役检查/失去厂外电/环境抗震"等数据。

**根因**（`pdfparser/layout.py` `_merge_cross_page_paragraphs`，两个叠加 bug）：
1. 循环入口只检查 `a`（前一页块）是否已合并，**未检查 `b`（下一页首块）**是否已被合并 → 第 2 页顶部块被前一页**每一个** text 块重复合并（注入 5 次）。
2. 合并条件只看字体/文本连续性，**不看页面几何位置** → 页面中部标题（3.2/3.2.1）与下一页首块误合。
3. 合并后未更新 `a.page` 字段（仍是前一页，bbox 却跨页），适配器按 `block.page` 分组导致内容错误归页。

**修复**：
- 循环入口增加 `if j in merged or id(ordered[j]) in merged: continue`（防重复注入）。
- 增加页面几何边界约束：仅当 `a` 位于本页底部（y1 距本页正文最大 y1 ≤ 60pt）且 `b` 位于下一页顶部（y0 距下一页最小 y0 ≤ 60pt）时才合并。
- 只允许**每页最后一个正文块**作为合并源（`last_text_by_page.get(a.page) != i` 则跳过），避免非页末块抢走续行。

**连带修复**（`_hf_lines` 跨页重复误杀列表项）：`_hf_lines` 原按"出现次数 ≥ max(3, 35%)"判定页眉页脚，跨页重复的列表项（如"在役检查和定期试验；"在 2 页出现 3 次）被误杀。增加**位置一致性判定**：跨页出现的 y0 差异 ≤ 20pt 才判为页眉页脚（真页眉页脚每页位置固定，列表项则分散）。

**验证**：修复后 3.2/3.2.1 标题下无污染，"在役检查/失去厂外电/环境抗震"恢复，质保等级:QA1 正确留在第 2 页。流水线 A GREEN 9/14 → 15/16 (94%)。

### 8.2 bullet 列表结构丢失 + 正文误判为列表（2026-08-18）

**现象 1**：`DCS设备技术规格书` 3.2.2.1/3.2.2.2 的 7 个 bullet 列表被合并成单段，bullet 标记消失。

**根因**：`_cluster_paragraphs` 把 Wingdings 私有字符 bullet 行（`\uf06c`）与下一行 SimSun 文本合并成同一段，ListDetector 提取不出独立 bullet 结构。

**修复**：`_cluster_paragraphs` 检测非主流字体（非 SimSun/TimesNewRomanPSMT）的单字符行（`len(text.strip()) <= 3`），强制 flush 前段并独立成段（symbol 行与内容文本行分属两个 block）。

**现象 2**：`RPS系统需求规范书` RQ-010/012/013 句子折半（"（A、\nB）"被拆行）。

**根因**：`_split_regions` 把正文行内"B）"（括号内单字母+右括号）误识别为列表 marker（alpha 模式 `^([a-zA-Z])[.、)）]\s*`），整段连续正文被识别为 list。

**修复**：`ListDetector.identify` 增加两道门槛：
1. 至少一个 list region 包含 ≥2 个有效 item；
2. 所有 list region 的 marker type 必须是常见列表标记（numeric/circled/cn_num/bullet/dash），排除 alpha（单字母+右括号易与正文括号混淆）。

**验证**：流水线 B GREEN 34/41 → 36/41 (88%)。

### 8.3 Excel 行高未随内容展开（2026-08-19）

**现象**：Excel 中内容与 PDF 解析段落结构不一致——bullet 列表显示为单段，行内容被垂直挤压。

**根因**：**不在解析/提取/匹配层**（全链路核验 `\n` 完整保留、`wrapText="1"` 正确），而在 `excel_generator.py` 等生成 Excel 时**未设置数据行行高**——默认 15pt 单行高，wrap_text 产生的多行被视觉挤压。

**修复**（3 个文件新增 3 个辅助函数）：
- `_estimate_visible_lines(text, col_width)`：按显式换行分段 + 列宽折行（CJK 全角按 2 半角宽计）估算可见行数。
- `_set_row_height(ws, row, texts_with_widths)`：数据行按内容行数动态设行高（`max(15, 行数×17+4)` pt）。
- `_set_merged_row_heights(ws, first_row, last_row, ...)`：合并单元格区域按行数平均分摊总高度。
- 应用点：`_write_backward_sheet`、`_write_forward_sheet`、`generate_forward_matrix.py`、`generate_forward_sd_matrix.py` 各分支。

### 8.4 富文本换行符独立 run 导致标绿内容合并为一行（2026-08-19）

**现象**：同样内容，段落被标绿（GREEN）时 Excel 中合并为一行；标黑（BLACK）时正常。

**根因**（用户洞察的关键）：`text_matcher.py` 的 `_blocks_to_runs_subphrase` 生成子短语 GREEN 时，把换行符 `\n` 拆到**独立的 BLACK run**（XML 中 `<t>\n</t>`），GREEN run 只含纯文本。WPS/Excel 渲染富文本时，夹在 GREEN 文本之间的"纯换行 run"的换行失效 → 标绿的多行合并成一行。对比：整段单 run 时换行在 run 内部，正常。

**证据**：修复前 F10 22 个 run、换行符全在独立 BLACK run；修复后 11 个 run、换行符全部附着在文本 run 内部。

**修复**：`_blocks_to_runs_subphrase` 返回前增加后处理——把每个 run **开头的换行符剥离并合并到前一个 run 的末尾**，使换行符附着在文本 run 内部。文本拼接完整性（字符级相等）已验证。

### 8.5 带目录文档的目录条目污染章节提取（2026-08-19）

**现象**：带目录的文档，目录页每行（如 "3.2 系统和设备分级 .... 15"）被当章节提取进 Excel 作为匹配对象，与正文同名章节形成重复/覆盖。

**根因**（两层）：
- 提取层：`extract_sections` 正则按 MULTILINE 匹配所有行首编号标题，目录条目恰好命中。
- 新模块：`headings.py` 无目录页识别，目录条目按编号模式甚至被标为 heading。

**修复**（首选 + 可选）：
- `extract_sections`：TOC 行特征检测（行尾页码 + 点线引导符 `.` 或连续空格）剔除目录条目。修复一个边界 bug：`section_pattern` 的 `(?:\n|$)` 消耗换行符导致 `m.end()` 指向换行符后，`text[:m.end()].rsplit('\n', 1)[-1]` 取到空行，改用 `text[m.start():m.end()]`。
- `headings.py`：`_mark_toc_consumed` 检测"目录/CONTENTS"标题块，把点线引导符 + 右对齐页码的目录条目标记为 `consumed`（不进标题树、不进正文），从源头消除。

### 8.6 追踪关系表内容污染需求条目（2026-08-19）

**现象**：文档末尾的需求追踪关系表行（如 `<RPS-SYS-RQ-010> <UR-SYS-005> 本条目追踪用户需求`）被 ID 正则误识别，正文条目被表格行噪声覆盖/污染。

**根因**：`extract_requirement_items` 用 `ITEM_ID_REGEX` 全文匹配，不区分正文与追踪表；同 ID 多出现时合并逻辑把表格行内容并入正文。

**修复**（`requirement_extractor.py`）：
1. 追踪表行过滤：行内嵌另一 ID（`<xxx>`）、极短无句末标点摘要（≤30 字符，含跨行断开）、以"附表/表N/序号/本文档中的设计标志号"等表头词开头的出现 → `continue` 跳过。
2. 追踪表表头截断：正文条目内容夹带"附表N/序号/本文档中的设计标志号"等表头时，在表头位置截断（`_TOC_TABLE_HEADER_RE`）。
3. `tables.py`：无线表检测增加表头关键词启发式（条目/编号/序号/上游/追踪/来源/对应/关联/说明/备注/文件），命中时放宽阈值（`min_rows=3→2`），提高追踪表识别率，避免表内容流入正文。

### 8.7 回归修复：条目标题误删导致匹配退化（2026-08-19）

**现象**：实施 8.5/8.6 修复后，流水线 B 从 GREEN 36/41 降至 35/41（`ICADS009/011/012` 相关）。

**定位方法**（关键经验）：逐一恢复文件到 HEAD 版本跑流水线 B，二分定位出 `requirement_extractor.py` 引入回归；再对比新旧 `extract_requirement_items` 输出 diff，发现 `ICADS009` 内容从 83 字符降至 76 字符——**追踪表过滤（8.6）让 ICADS009 从"2 次出现合并"变为"仅正文 1 次出现"，从而触发 `_drop_entry_title_line` 误删正文首行"机柜门开状态"**。

**根因**：`_drop_entry_title_line` 原本删除所有 ≤15 字无标点短首行，把条目**专有名词标题**（"机柜门开状态"、"I&C故障与机柜门开报警"，与上游 `<DCS-SyRS005>` 的"-机柜门开;"匹配关键）误当冗余标题删除。

**修复**：仅丢弃**通用章节类标题**——首行含章节号（`^\d+(\.\d+)*[\.、]?\s*`）或通用词（概述/要求/功能/描述/简介/综述/规则/准则/说明/意义/目的/范围/组成/结构/原理/故障诊断/模块类/系统级/总体/通用）才删除；条目专有名词保留。

**验证**：流水线 B 恢复 GREEN 36/41 (88%)。ICADS009/011/012 标题保留、正文完整、追踪表噪声清除。

### 8.8 本轮经验总结

1. **跨页/跨块合并必须做几何边界约束**：仅凭字体+文本连续性合并，会把页面中部块与下一页首块误合。合并前必须检查"页末→页顶"的位置关系，且只允许每页最后一个块作为合并源。
2. **集合去重要检查消费端**：`merged` 标记后，循环入口必须同时检查 `b` 是否已被消费，否则同一资源被重复消费。
3. **富文本换行符必须附着在文本 run 内**：openpyxl CellRichText 中，换行符独立成 run 会被 WPS/Excel 渲染忽略（标绿合并为一行）。所有 run 构造后应做"前导换行符合并到前一个 run"后处理。
4. **Excel 行高是"内容结构可读性"的一部分**：wrap_text + 默认行高会把多行内容视觉挤压成单段。按内容换行数/列宽动态设行高是必需步骤（合并区域按行数分摊）。
5. **过滤噪声要防误伤正文**：追踪表/目录过滤规则应"窄而准"（用多特征 AND 而非单宽特征），且要结合下游匹配回归验证。本例 `_drop_entry_title_line` 的通用词白名单是经过回归验证的平衡方案。
6. **回归定位用"逐文件恢复 HEAD"二分法**：多个文件同时改动时，逐个恢复某文件到 git HEAD 跑 E2E，能快速锁定引入回归的文件；再对新旧提取输出做 diff 定位具体逻辑差异。
7. **PowerShell 中文/编码坑**：中文路径传参、UTF-8 输出重定向在 PowerShell 5.1 下易乱码。用 `$env:PYTHONIOENCODING='utf-8'` + Python 脚本内 `sys.stdout.reconfigure(encoding='utf-8')` 最稳妥；文件写入用 UTF-8-sig 便于 read_file 读取。

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
13. 回归测试：`e:\Trace_NL\tests\test_cross_page_merge_regression.py`
14. 回归测试：`e:\Trace_NL\tests\test_list_structure_regression.py`
15. 本次修复提交：`fc7de55`（2026-08-19）
