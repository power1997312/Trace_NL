# 核仪控工程文档追踪验证系统 — 设计说明

> 本文档供后续开发/AI会话快速理解系统全貌，最后更新：2026-07-03 (第二轮优化)

---

## 1. 系统概述

本系统面向核电仪控（I&C）工程领域，用于自动化验证上下游文档之间的追踪关系。典型场景：下游的"DCS系统需求说明书"中每条需求，需要追踪到上游"用户需求"或"设备技术规格书"中的对应条目，并以**微块级文本匹配**方式在Excel中标注匹配程度（绿色=完全一致、蓝色=语义匹配、黑色=未匹配）。

### 核心能力
- 自动解析PDF文档（双引擎：PyMuPDF提取正文 + pdfplumber提取表格）
- 自动检测文档类型（ID型 vs 章节型），提取需求条目与章节内容
- 构建逆向/正向追踪矩阵
- 微块级文本匹配验证（嵌入相似度 + 字符级精确验证 + NLI语义验证）
- 子短语级GREEN扫描（在BLACK微块内部查找完全一致的子串并标绿）
- PDF ASCII图表噪声自动过滤
- 短语提纯（去编号前缀和功能后缀，提升嵌入编码质量）
- 匈牙利算法最优匹配（替代贪心，避免多对一冲突）
- 双向标色一致性校验
- 生成带 CellRichText 着色的 Excel 文件
- 正向追踪矩阵独立生成（PDF完整条目覆盖 + union组合着色 + NA填充）

---

## 2. 项目结构

```
E:\Trace_NL\
├── config.py                      # 全局配置（阈值、路径、常量、枚举）
├── app.py                         # Streamlit Web界面入口
├── run_e2e_test.py                # 端到端测试脚本（命令行）
├── compare_detail.py              # 基线对比分析工具（逐单元格逐run）
├── compare_row_detail.py          # 基线对比分析工具（逐追踪关系）
├── generate_forward_matrix.py     # 正向追踪矩阵生成（PDF完整条目+union着色+NA填充）
├── requirements.txt               # Python依赖声明
├── DESIGN.md                      # 本文档
│
├── core/                          # 核心业务模块
│   ├── __init__.py
│   ├── pdf_parser.py              # PDF双引擎解析（正文+表格）
│   ├── requirement_extractor.py   # 需求条目/章节内容提取
│   ├── traceability_matrix.py     # 追踪矩阵构建
│   ├── text_matcher.py            # 微块级文本匹配引擎（核心算法）
│   └── excel_generator.py         # 格式化Excel生成（CellRichText着色）
│
├── models/                        # ML模型封装
│   ├── __init__.py
│   ├── embedding_model.py         # bge-small-zh 嵌入模型
│   └── nli_model.py               # Erlangshen-Roberta-110M-NLI 推理模型
│
├── model/                         # 模型权重与库（内嵌，无需联网下载）
│   ├── embedder/                  # bge-small-zh (~96MB, 24M参数, 4层BERT)
│   ├── nli/                       # NLI模型 (~409MB, 110M参数, 12层BERT)
│   └── sentence_transformers/     # 内嵌sentence_transformers库
│
├── output/                        # 验证结果输出目录
│   └── 追踪验证结果_v*.xlsx
│
├── 基准数据/
│   └── Trace_Base.xlsx            # 人工标注的基准数据（用于对比评估）
│
├── 系统需求/                      # 下游文档PDF
│   └── DCS需求说明书.pdf
│
├── 用户需求/                      # 上游文档PDF
│   ├── DCS设备技术规格书.pdf
│   └── RPS系统需求规范书.pdf
│
└── 验证报告/                      # 验证问题记录
    ├── 问题清单.doc
    ├── 问题清单详细分析.md
    └── images/
```

---

## 3. 模块说明

### 3.1 config.py — 全局配置

定义所有可调参数、路径常量和枚举类型。

**关键阈值（已调优）：**
| 参数 | 当前值 | 含义 |
|---|---|---|
| `EXACT_CHAR_THRESHOLD` | 0.92 | bigram Jaccard 精确匹配阈值 |
| `EMBEDDING_EXACT_THRESHOLD` | 0.95 | 嵌入余弦相似度→进入GREEN验证路径 |
| `EMBEDDING_SEMANTIC_THRESHOLD` | 0.70 | 嵌入余弦相似度→进入BLUE验证路径 |
| `EMBEDDING_UNMATCHED_THRESHOLD` | 0.50 | 嵌入余弦相似度最低门槛，低于此直接BLACK |
| `NLI_ENTAILMENT_THRESHOLD` | 0.85 | NLI蕴含概率→BLUE |
| `NLI_NEUTRAL_EMBEDDING_THRESHOLD` | 0.80 | NLI中立+嵌入联合判定→BLUE |
| `MIN_PHRASE_LENGTH` | 4 | 短语最小字符数 |

**路径常量：** `PROJECT_ROOT`, `MODEL_DIR`, `EMBEDDER_MODEL_PATH`, `NLI_MODEL_PATH`, `OUTPUT_DIR`

**文档层级映射 `DOCUMENT_LEVEL`：** 定义用户需求(上游)、系统需求(中游)、系统设计/软件需求/硬件需求(下游)的角色和层级。

**枚举类型 `MatchCategory`：** GREEN(exact)/BLUE(semantic)/BLACK(unmatched)，附带 `.color`（Excel颜色码）和 `.priority`（优先级）属性。

### 3.2 core/pdf_parser.py — PDF双引擎解析

**职责：** 从PDF提取正文文本和追踪矩阵附录表。

**双引擎策略：**
- **PyMuPDF (fitz)：** 正文文本提取。优势是段落结构好，速度快。
- **pdfplumber：** 表格提取。仅在最后8页扫描追踪矩阵附录表时使用。

**文本清洗流水线（5步）：**
1. 清除页眉页脚和密级标记（正则匹配 `"xxx 版本：x 页码：x/x"` 格式）
2. 去除PDF私有区Unicode字符（U+F000-U+F8FF，通常是Wingdings bullet）
3. 合并句内换行（CJK→CJK或Latin→CJK跨行合并，保留段落边界、章节标题、列表项的换行）
4. 消除CJK-Latin伪空格（PyMuPDF根据字形间距自动插入的空格）
5. 清理孤立bullet碎片

**关键数据结构：**
- `PageText(page_number, text)` — 单页文本
- `TableData(page_number, headers, rows)` — 表格数据
- `TraceRelation(downstream_id, upstream_ref, upstream_doc)` — 单条追踪关系

**关键函数：**
- `extract_body_text(pdf_path)` → `list[PageText]`
- `find_traceability_table(pdf_path)` → `TableData | None`（扫描最后8页）
- `parse_traceability_table(table)` → `list[TraceRelation]`（处理多值单元格）

### 3.3 core/requirement_extractor.py — 需求条目/章节提取

**职责：** 自动检测文档类型并提取结构化内容映射。

**文档类型检测：** 全文中正则匹配 `<xxx>` 格式ID，去重后≥3个 → ID型文档，否则 → 章节型文档。

**ID型提取 `extract_requirement_items`：**
- 用 `ITEM_ID_REGEX` 匹配所有尖括号ID
- 每个ID的内容 = 从该ID结束到下一个ID开始之间的文本
- 同一ID多次出现时保留最长版本（处理缩略语表 vs 正文）
- 过滤附录引用：内容长度 < 中位数×15% 的条目被过滤

**章节型提取 `extract_sections`：**
- 匹配 `"3.2.2.1 F-SC1级要求"` 等格式的章节号+标题
- 每个章节的内容 = 从标题结束到下一个章节标题前

**ContentMap 多策略引用解析（6级回退）：**
1. 精确ID匹配
2. 归一化ID匹配（去空格、统一全半角标点）
3. 精确章节号匹配
4. 精确full_key匹配（章节号+标题）
5. 子串匹配 + 章节号前缀匹配
6. 模糊匹配（difflib SequenceMatcher ≥ 0.7）

### 3.4 core/traceability_matrix.py — 追踪矩阵构建

**职责：** 整合PDF解析和内容映射，构建追踪矩阵。

**构建流程 `build_backward_matrix`：**
1. 解析下游PDF的追踪矩阵附录表 → `list[TraceRelation]`
2. 提取下游文档内容映射 → `{item_id: content}`
3. 为每个上游文档构建内容映射 → `ContentMap`
4. 按下游条目分组，分配序号，逐条填充上下游内容
5. 跨文档回退：指定文档无法解析时，尝试所有其他上游文档

**数据结构：**
- `TraceabilityRow` — 矩阵单行：序号、下游ID/内容、上游文档/引用/内容、匹配结果
- `TraceabilityMatrix` — 矩阵容器：行列表 + 逆向/正向合并组（一对多关系）
- `to_forward_format()` — 按(upstream_doc, upstream_ref)重新分组，生成正向矩阵

### 3.5 core/text_matcher.py — 微块级文本匹配引擎（核心）

**这是系统的核心算法模块，详见第4节。**

### 3.6 core/excel_generator.py — Excel生成

**职责：** 将追踪矩阵和匹配结果生成格式化Excel。

**关键特性：**
- 使用 openpyxl 的 `CellRichText` + `TextBlock` + `InlineFont` 实现微块级着色
- 每个 `TextRun` 对应一个 `<r>` XML元素，拥有独立的颜色字体
- 正确处理 `\n` 换行（在 inline string `<t>` 元素中保留）
- 逆向矩阵Sheet（6列）；正向矩阵已移至独立模块 `generate_forward_matrix.py` 生成
- 一对多关系通过合并单元格处理

**颜色定义：**
- GREEN: `#00B050` — 完全一致
- BLUE: `#0070C0` — 语义匹配
- BLACK: `#000000` — 未匹配
- 表头: `#4472C4` 蓝底白字

### 3.7 models/embedding_model.py — 嵌入模型

- 模型：bge-small-zh（BAAI），4层BERT，hidden_size=512，24M参数，~96MB
- 使用 transformers `AutoModel` 直接加载（非sentence-transformers），避免版本兼容问题
- 懒加载（首次调用时初始化），自动检测CUDA/回退CPU
- Mean pooling（带attention mask）+ L2归一化
- `encode(texts, batch_size=32)` → `(N, 512)` 归一化嵌入向量
- `cosine_similarity_matrix(a, b)` → `(M, N)` 余弦相似度矩阵（因向量已归一化，直接 `a @ b.T`）

### 3.8 models/nli_model.py — NLI推理模型

- 模型：Erlangshen-Roberta-110M-NLI，12层BERT，hidden_size=768，110M参数，~409MB
- 3分类：CONTRADICTION(0), NEUTRAL(1), ENTAILMENT(2)
- 使用 `AutoModelForSequenceClassification` 加载
- `predict_batch(pairs, batch_size=16)` → `list[dict]`，每个 `{CONTRADICTION, NEUTRAL, ENTAILMENT}` 概率值

### 3.9 generate_forward_matrix.py — 正向追踪矩阵生成

**职责：** 从已完成的逆向追踪矩阵Excel中提取匹配数据，结合用户需求PDF的完整章节结构，生成独立的正向追踪矩阵Excel（用户需求 → 系统需求）。

**与逆向矩阵的区别：**
- 逆向矩阵：一条系统需求 → 对应的用户需求（多对一）
- 正向矩阵：一条用户需求 → 对应的多条系统需求（一对多，去重转置）

**核心流程（5步）：**

1. **XML级RichText读取** `_read_reverse_matrix_data`：直接解析逆向矩阵Excel的XML，提取每个单元格的富文本runs（颜色+文本）。绕过openpyxl的data_only模式（可能丢失inline string颜色）。同时处理下游侧合并单元格（B/C列为空时继承前一行的ds_id）。

2. **PDF完整条目提取** `_extract_pdf_items`：
   - ID型文档：仅提取需求条目（`<RPS-SYS-RQ-001>`等），不再重复提取章节
   - 章节型文档：提取章节，标题截断至20字符防止正文泄漏
   - 确保正向矩阵覆盖PDF中所有章节/条目，而非仅逆向矩阵中已有的追踪关系

3. **引用匹配** `_match_ref_to_item`：将逆向矩阵中的`up_ref`匹配到PDF条目。4级回退策略：
   - 精确匹配（归一化后完全相等）
   - 子串匹配（双方长度≥5，且非纯章节号，防止父节抢匹配）
   - 章节号前缀匹配（精确→子节→父节）
   - 模糊匹配（difflib ≥ 0.7）

4. **Union Coloring组合合并** `_compute_union_coloring`：当同一用户需求对应多条系统需求时，对每个字符位置取所有匹配中优先级最高的颜色：GREEN > BLUE > BLACK。实现"需求覆盖完成度"的可视化——绿色/蓝色部分表示被至少一条系统需求覆盖。

5. **Excel生成** `generate_forward_excel`：每份用户需求文档一个独立sheet。上游内容（C列）合并单元格并使用union着色；下游内容（F列）按每条追踪关系独立着色；无追踪关系的条目下游填"NA"。

**关键数据结构：**
```python
# 正向矩阵条目
{
    'up_text': str,           # 用户需求内容
    'up_runs': list[(rgb, text)],  # union着色runs
    'up_category': MatchCategory,  # 整体类别
    'sys_reqs': [{            # 系统需求列表
        'ds_id': str, 'ds_text': str,
        'ds_runs': list, 'ds_category': MatchCategory,
    }],
    'has_trace': bool,        # 是否有追踪关系
}
```

**输出格式（6列）：**
| 列 | 含义 | 合并规则 |
|---|---|---|
| A 序号 | 自增序号 | 一对多时合并 |
| B 上游条目号 | 用户需求条目号/章节号 | 一对多时合并 |
| C 上游内容 | 用户需求内容（union着色） | 一对多时合并 |
| D 下游文档 | 系统需求文档名 | 每行独立 |
| E 下游条目号 | 系统需求条目号 | 每行独立 |
| F 下游内容 | 系统需求内容（按该条追踪关系着色） | 每行独立 |

---

## 4. 文本匹配算法详解

### 4.1 match_text_pair 完整流水线

对每对（下游文本, 上游文本）执行以下完整流程：

**Pre-Stage：文本预处理**
1. **ASCII图表噪声清理** `_strip_ascii_noise_for_matching`：检测并移除连续短ASCII行（≥2行连续无CJK字符的短行），返回 `(cleaned_text, pos_map)` 位置映射表
2. **段落级前缀归一化** `_normalize_text_for_decomposition`：去除行首列表标记（`-`/`•`/`1)`等），合并因PDF换行导致的碎片化短行（<15字符非列表项行与前一行合并），返回 `(normalized_text, pos_map)`
3. **三级微块分解** `decompose_text`：段落→句子→短语

**坐标系统：**
- 保存每个微块的 `_clean_start`（cleaned-text坐标，在位置重映射之前）
- 调用 `_remap_block_positions` 将 `block.start/end` 从 cleaned-text 坐标映射回原文坐标
- 后续子短语GREEN使用 `_clean_start` + `pos_map` 进行正确的坐标转换

**Stage 0：嵌入编码**
- 对每个微块进行**短语提纯** `_purify_phrase`：去除编号前缀和功能类型词后缀（如"功能"、"系统"、"模块"），得到 `clean_text`
- 使用提纯后的文本（如有）编码为嵌入向量，提升嵌入质量
- 计算余弦相似度矩阵 `(M×N)`

**Stage 1：最优匹配** `_optimal_matching`
- 使用**匈牙利算法**（`scipy.optimize.linear_sum_assignment`）计算最优二分匹配
- 约束：相似度 < `EMBEDDING_UNMATCHED_THRESHOLD` 的匹配对设极大代价（不会被选中）
- 回退：scipy不可用时使用约束性贪心匹配
- 返回两个方向的匹配映射：`ds_matches`(下游→上游) 和 `up_matches`(上游→下游)

**Stage 2：分类判定** `_classify_block`
- 下游→上游：对每个下游微块，使用其最优匹配的上游块进行分类
- 上游→下游：对每个上游微块，使用其最优匹配的下游块进行分类
- 两个方向独立分类（详见4.3节决策树）

**Stage 3：子短语GREEN扫描** `_apply_subphrase_green`
- 双向独立执行：下游→上游 和 上游→下游
- 对BLACK微块内部查找与对侧完全一致的子串（≥8字符），标为GREEN
- 使用 `pos_map` 和 `_clean_start` 将children坐标从块文本本地坐标正确转换为原文坐标（详见4.5节）

**Stage 4：双向标色一致性校验** `_ensure_bidirectional_consistency`
- 当下游块为GREEN/BLUE时，其对应的上游块不应为BLACK（反之亦然）
- 取两者中较高的类别作为统一类别（需相似度≥阈值才升级）
- 注意：此机制仅同步**块级**类别，不同步子短语children

**Stage 5：着色重建** `_blocks_to_runs`
- 将微块列表合并为 `TextRun` 列表（相邻同色微块合并为一个run）
- 使用原文切片保真重建（从原文按 `(start, end)` 位置切片）
- 支持子短语GREEN：有children的块分解为 BLACK/BLACK/GREEN/BLACK/GREEN... 细粒度runs

### 4.2 三级微块分解

将文本逐层拆解为最细粒度的短语单元：

```
段落（按\n分割）
  └→ 句子（按句末标点分割：。；;！!？?）
       └→ 短语（按逗号/顿号/冒号/分号/句号/破折号分割）
```

**短语分割正则：** `(?<=[，、,：:；;.。\n])\s*|(?=——)`
**列表项正则：** `^\s*(\d+[)）]|[a-zA-Z][)）]|[-•●](?:\s|(?=[\u4e00-\u9fff])))`

**短短语合并：** 少于 `MIN_PHRASE_LENGTH`（当前4字符）的短语与相邻短语合并，保持位置追踪。

**位置追踪：** 每个微块记录其在原文中的 `(start, end)` 位置，用于后续保真重建（从原文切片而非拼接）。

### 4.3 分类决策树（`_classify_block`）

```
嵌入相似度 < 0.50 ?
  └→ 是 → BLACK（直接排除，不做任何后续计算）

嵌入相似度 ≥ 0.95 ?
  └→ GREEN路径（精确匹配验证）
       ├─ 路径1: bigram Jaccard ≥ 0.92 → 通过
       ├─ 路径2: char_set Jaccard ≥ 0.88 且 LCS比率 ≥ 0.80 → 通过
       └─ 通过后检查: 长度比 ≥ 0.70 → GREEN
                     长度比 < 0.70 → 降级到下方路径

Stage 2a-fallback: 归一化后文本完全一致 → GREEN
  └→ 条件: 嵌入≥0.85 + 归一化文本完全相等 + 长度≥8字符 + 原始长度比≥0.50
  └→ 目的: 处理嵌入模型对微小后缀差异(如"功能")过度敏感的情况

嵌入相似度 ≥ 0.70 ?
  └→ BLUE路径（语义匹配验证）
       ├─ NLI矛盾概率 ≥ 0.80 → BLACK
       ├─ NLI蕴含概率 ≥ 0.85 且 char_set Jaccard ≥ 0.35 → BLUE
       │   └→ 或: 蕴含≥0.90 且 char_set≥0.30 → BLUE（容忍短语分解粒度差异）
       ├─ NLI中立概率 ≥ 0.80 且 嵌入 ≥ 0.80 且 char_set Jaccard ≥ 0.40 → BLUE
       └→ 其余继续到下方

Stage 2c: 高嵌入回退BLUE（无需NLI）
  └→ 条件: 嵌入≥0.85 + (LCS≥0.55 或 char_set≥0.60) → BLUE

Stage 2d: 中等嵌入 + 高字符重叠 → BLUE
  └→ 条件: 嵌入≥0.75 + char_set≥0.55 + LCS≥0.45 → BLUE
  └→ 目的: 处理上下游短语分解粒度不同导致的嵌入稀释

其余 → BLACK
```

**设计要点：**
- **无兜底策略：** 取消了旧版 `if embedding >= 0.70: return BLUE` 的宽松兜底，严格控制BLUE比例
- **双路径GREEN：** 路径1（bigram）对完全一致最严格；路径2（char_set + LCS）容忍后缀差异（如"采集来自本保护组PIPS的信号" vs "采集来自本保护组PIPS"）
- **归一化GREEN回退：** 处理嵌入模型对"功能"等后缀差异过度敏感的情况，要求足够长的文本（≥8字符）和合理的长度比
- **多级BLUE回退：** Stage 2b(NLI) → 2c(高嵌入+LCS) → 2d(中嵌入+高字符重叠)，逐级放宽条件
- **BLUE文本重叠验证：** NLI蕴含即使高分，也要求char_set≥0.35，避免纯语义相近但内容完全不同的文本被标BLUE
- **PDF字符混淆归一化：** 在字符级比较前进行上下文感知的归一化（l→1: 大写字母/数字上下文中的l; O→0: 数字序列中的O），提升匹配准确性

### 4.4 短语提纯（`_purify_phrase`）

对分解后的短语进行提纯，去除不影响语义的修饰成分，提升嵌入编码质量：

1. **去编号前缀：** 去除 `"1)"`, `"2）"`, `"a)"`, `"- "` 等列表标记
2. **去尾部标点：** 去除 `；;。，,` 等尾部标点
3. **去功能类型词后缀：** 当短语长度>6字符时，去除 `"功能"`, `"系统"`, `"模块"`, `"装置"`, `"单元"`, `"组件"` 等后缀词
4. **提纯后的文本 `clean_text`** 用于嵌入编码，原始文本 `text` 用于输出着色

### 4.5 子短语GREEN扫描（`_apply_subphrase_green`）

当短语级别的匹配为BLACK时，在短语内部查找与对侧文本完全一致的子串，将其标为GREEN。

**保守策略参数：**
- `_MIN_GREEN_SUBSTR_LEN = 8`：最短GREEN子串长度（避免"个保护组"等短片段误匹配）
- `_MAX_GREEN_SUBSTRS_PER_BLOCK = 3`：每块最多3个GREEN子串
- `_MIN_BLOCK_SIM_FOR_SUBPHRASE = 0.45`：块级嵌入相似度低于此值不扫描（避免跨语义上下文匹配）

**算法流程：**
1. 对每个BLACK块，检查最佳候选的嵌入相似度（≥0.45才继续）
2. 取top-2候选块，用DP算法查找最长公共子串（≥8字符，归一化后比较）
3. 验证bigram Jaccard ≥ 0.92
4. 去重：移除被更长子串完全包含的短子串
5. 贪心选择最优非重叠子串集合（按长度降序，最多3个）
6. 验证GREEN子串总长度 ≤ 块文本的90%
7. **坐标映射：** 将children从块文本本地坐标转换为原文坐标

**坐标映射细节（关键修复）：**
- `_apply_subphrase_green` 接收 `pos_map` 参数
- children中的位置 `(s, e)` 是相对于 `block.text`（cleaned text）的本地坐标
- 转换为绝对cleaned-text坐标：`abs_clean_s = block._clean_start + s`
- 通过pos_map映射到原文坐标：`new_s = pos_map[abs_clean_s]`, `new_e = pos_map[abs_clean_e - 1] + 1`
- `_clean_start` 在 `_remap_block_positions` 之前保存，记录块在cleaned-text中的起始位置

### 4.6 ASCII图表噪声清理（`_strip_ascii_noise_for_matching`）

PDF中嵌入的表格/图表常被提取为连续短ASCII行（如 `"PIPS-1"`, `"ESFAC-A1"`, `"≥1"` 等），干扰匹配。此预处理步骤将其移除。

**检测逻辑：**
- 逐行扫描，检测连续≥2行无CJK字符且长度≤8（或匹配技术标签正则）的行
- 技术标签正则 `_TECH_LABEL_RE`：纯ASCII短标签（字母/数字/运算符组成，长度≤8）

**输出：**
- `cleaned_text`：移除噪声行后的文本
- `pos_map`：`pos_map[i]` = cleaned_text第i个字符在原文中的位置（用于后续坐标重映射）
- 仅对长度≥100的文本执行（短文本不太可能包含ASCII图表）

### 4.7 字符级相似度函数

| 函数 | 算法 | 用途 |
|---|---|---|
| `_char_bigram_jaccard` | 字符二元组的集合Jaccard | GREEN路径1（最严格） |
| `_char_set_jaccard` | 去重字符集的Jaccard | GREEN路径2 + BLUE重叠验证 |
| `_lcs_ratio` | 最长公共子序列比率（O(n²) DP，200字符截断，滚动数组优化） | GREEN路径2 + BLUE回退 |
| `_normalize_for_char_compare` | 全角→半角、去PUA字符、去bullet/空白、去编号前缀、去功能后缀、PDF字符混淆归一化 | 所有字符级比较的前置归一化 |

**`_normalize_for_char_compare` 归一化步骤（按顺序）：**
1. 全角→半角标点映射（（→(, ）→), ：→:, ，→, 等）
2. 去PDF私有区字符（U+F000-U+F8FF）
3. 去列表标记和bullet符号
4. 去数字/字母编号前缀（`1)`, `2）`, `a)` 等，使GREEN匹配对编号差异免疫）
5. 去空格和换行
6. 去尾部标点
7. 去功能类型词后缀（`功能|系统|模块|装置|单元|组件`，使GREEN匹配对后缀差异免疫）
8. PDF字符混淆归一化：`l→1`（大写字母/数字上下文）、`O→0`（数字序列中）

### 4.8 着色重建（`_blocks_to_runs`）

将微块列表合并为 `TextRun` 列表（相邻同色微块合并为一个run），使用原文切片保真重建：
- 从原文中按 `(start, end)` 位置切片，而非直接拼接微块文本
- 颜色变化时，run间的空白字符（含单个换行）归入前一个run
- 避免逗号/换行拼接破坏原文完整性

**子短语GREEN模式（`_blocks_to_runs_subphrase`）：**
- 当存在有children的GREEN块时启用
- 将GREEN块分解为：BLACK(前缀) + GREEN(子串1) + BLACK(间隔) + GREEN(子串2) + ... + BLACK(后缀)
- 安全性：检测块位置重叠时回退到标准处理，防止文本重复

### 4.9 双向标色一致性（`_ensure_bidirectional_consistency`）

确保下游和上游的标色逻辑一致：
- 当下游块为GREEN/BLUE时，其最优匹配的上游块不应为BLACK（反之亦然）
- 取两者中较高的类别统一（需嵌入相似度≥`EMBEDDING_SEMANTIC_THRESHOLD`才升级）
- **已知限制：** 此机制仅同步块级类别，不同步子短语children。因此可能出现一侧有GREEN子短语而另一侧没有的情况（因为 `_apply_subphrase_green` 在两侧独立运行，处理的文本不同）

---

## 5. 数据流管线

```
PDF文档
  │
  ├─→ pdf_parser.extract_body_text()     → 正文文本（清洗后）
  ├─→ pdf_parser.find_traceability_table() → 追踪矩阵附录表
  │     └→ parse_traceability_table()      → list[TraceRelation]
  │
  ├─→ requirement_extractor.extract_requirement_items() → {ID: content}
  ├─→ requirement_extractor.build_content_map()         → ContentMap
  │
  └─→ traceability_matrix.build_backward_matrix()
        │  整合追踪关系 + 上下游内容
        └→ TraceabilityMatrix
              │
              └─→ text_matcher.verify_matrix()
                    │  逐行调用 match_text_pair()
                    │    ├─ _strip_ascii_noise_for_matching() → cleaned_text + pos_map
                    │    ├─ _normalize_text_for_decomposition() → 前缀归一化 + pos_map
                    │    ├─ decompose_text()                    → 微块列表
                    │    ├─ 保存 _clean_start + _remap_block_positions()
                    │    ├─ _purify_phrase() → clean_text (提纯后用于嵌入编码)
                    │    ├─ embedding_model.encode()            → 嵌入向量
                    │    ├─ 余弦相似度矩阵
                    │    ├─ _optimal_matching()                 → 匈牙利最优匹配
                    │    ├─ _classify_block()                   → GREEN/BLUE/BLACK
                    │    ├─ _apply_subphrase_green() × 2方向    → 子短语GREEN
                    │    ├─ _ensure_bidirectional_consistency() → 双向一致性
                    │    └─ _blocks_to_runs()                   → TextRun列表
                    └→ match_result 写入每行
                          │
                          └─→ excel_generator.generate_excel()
                                └─→ 带 CellRichText 着色的逆向追踪矩阵 Excel
                                      │
                                      └─→ generate_forward_matrix.generate_forward_matrix()
                                            │  从逆向矩阵XML读取着色数据
                                            │  从用户需求PDF提取完整条目
                                            │  匹配引用 + union着色
                                            → 正向追踪矩阵 Excel（独立文件，每份用户文档一个sheet）
```

---

## 6. 启动方式

### 6.1 Streamlit Web界面（推荐）
```bash
cd E:\Trace_NL
streamlit run app.py
# 浏览器自动打开 http://localhost:8501
```
操作流程：侧边栏配置目录 → 扫描文档 → 选择操作（逆向矩阵/正向矩阵/追踪验证/一键全流程）→ 预览结果 → 下载Excel

### 6.2 命令行端到端测试
```bash
cd E:\Trace_NL
python run_e2e_test.py
# 输出: output/追踪验证结果_v5.xlsx + output/正向追踪矩阵.xlsx
```

### 6.3 独立生成正向追踪矩阵
```bash
cd E:\Trace_NL
python generate_forward_matrix.py
# 从已有逆向矩阵Excel生成正向矩阵，输出: output/正向追踪矩阵.xlsx
```

### 6.4 基线对比分析
```bash
cd E:\Trace_NL
python compare_detail.py       # 逐单元格逐run对比 → compare_detail.txt
python compare_row_detail.py   # 逐追踪关系对比 → 逐行精细对比报告.txt
```

---

## 7. 依赖与环境

**Python版本：** 3.10+

**核心依赖：**
- streamlit >= 1.30.0（Web界面）
- torch（推理框架）
- transformers（模型加载）
- pdfplumber（表格提取）
- PyMuPDF / fitz（正文提取）
- openpyxl（Excel生成，需支持CellRichText）
- scipy（匈牙利算法 `linear_sum_assignment`）
- pandas, numpy, scikit-learn

**模型权重：** 内嵌在 `model/` 目录，无需联网下载。
**sentence_transformers库：** 内嵌在 `model/sentence_transformers/`，但当前代码实际使用 `transformers.AutoModel` 直接加载，不依赖此库。


---

## 8. 当前算法状态与优化方向

### 8.1 已实施的优化（6项）

1. **子短语GREEN扫描：** 在BLACK微块内部查找完全一致的子串（≥8字符）标为GREEN，解决"欠GREEN"问题
2. **PDF ASCII噪声清除：** 自动检测并移除连续短ASCII行（图表噪声），避免干扰匹配
3. **匈牙利最优匹配：** 替代argmax贪心，避免多对一匹配冲突，提升匹配质量
4. **短语提纯：** 去编号前缀和功能后缀，提升嵌入编码质量
5. **字符归一化（l/1, O/0）：** 上下文感知的PDF字符混淆归一化
6. **双向一致性校验：** 确保上下游标色逻辑一致（块级）

### 8.2 当前性能（v5 第二轮优化后）
基于16条追踪关系的E2E测试：
- **GREEN(完全一致): 15/16 (94%)**
- **BLUE(语义匹配): 0/16 (0%)**
- **BLACK(未匹配): 1/16 (6%)**
- 子短语GREEN对称性问题已大幅改善
- 合并单元格 Union 着色已实现

### 8.3 第二轮优化已修复问题

已修复5类问题，涉及 cross-document fallback、short text GREEN、sub-phrase symmetry、merged cell coloring、section extraction 等，详见验证评估笔记。

### 8.4 已知残留问题
1. 1/16 BLACK（seq=9）：结构保留改善导致的轻微回归
2. _build_norm_to_orig_map 仍需完整重写
3. 正向矩阵部分标题仍有正文泄漏

### 8.4 潜在优化方向

1. **NLI批处理优化：** 当前 `verify_matrix` 逐行逐块调用 `predict_batch([(text_a, text_b)])`，每次只传一对。可以收集所有需要NLI推理的文本对后统一批处理，减少GPU kernel launch开销，预期提速2-3倍。

2. **双向子短语同步（待重新设计）：** 需要一种不会导致内容缺失的机制来同步两侧的子短语GREEN标记。可能的方向：在 `_ensure_bidirectional_consistency` 中同步children，但需要仔细处理坐标映射和去重。

3. **PDF解析增强：** 修复已知的内容提取差异，可能需要针对特定PDF格式做特殊处理。


---

## 9. 关键设计决策记录

| 决策 | 选择 | 理由 |
|---|---|---|
| 嵌入模型 | bge-small-zh (24M) | 轻量、中文优化好、推理快 |
| NLI模型 | Erlangshen-Roberta-110M | 中文NLI效果好、110M参数在CPU上可接受 |
| 模型加载方式 | transformers AutoModel直接加载 | 避免sentence-transformers版本兼容问题 |
| 表格提取 | pdfplumber（仅最后8页） | 追踪矩阵附录固定在文档末尾 |
| 正文提取 | PyMuPDF | 段落结构好、速度快 |
| Excel着色 | CellRichText + InlineFont | 实现微块级细粒度着色，每个TextRun独立颜色 |
| 匹配粒度 | 短语级微块 | 在匹配精度和计算开销之间取平衡 |
| 分类策略 | 严格无兜底 + 多级BLUE回退 | 消除旧版BLUE过度标记，同时通过2c/2d回退捕获分解粒度差异 |
| GREEN双路径 | bigram + char_set+LCS | 兼顾严格匹配和后缀容忍 |
| 归一化GREEN回退 | 归一化后完全一致→GREEN | 处理嵌入模型对"功能"等后缀差异过度敏感 |
| 最优匹配 | 匈牙利算法 | 替代贪心，避免多对一冲突 |
| 短语提纯 | 去编号前缀+功能后缀 | 提升嵌入编码质量，使匹配更聚焦核心语义 |
| 双向独立匹配 | 下游→上游 和 上游→下游 结果独立 | C列和F列各自着色，不互相干扰 |
| 子短语GREEN坐标 | _clean_start + pos_map转换 | 修复ASCII噪声清理后cleaned-text坐标与原文坐标不一致的问题 |
| PDF字符归一化 | 上下文感知 l→1, O→0 | 处理PDF中常见的字符混淆 |
| 子短语GREEN最小长度 | 8字符 | 避免"个保护组"等短片段误匹配 |
| 正向矩阵数据源 | 逆向矩阵XML + 用户需求PDF | 不重新执行匹配算法，复用已着色数据；PDF提取保证完整条目覆盖 |
| 正向矩阵着色 | Union Coloring（GREEN>BLUE>BLACK） | 同一用户需求对应多条系统需求时，每字符取最高优先级颜色，直观体现需求覆盖完成度 |
| ID型文档条目提取 | 仅取需求条目，不取章节 | 避免ID型文档中条目与章节重复（如<RPS-SYS-RQ-001>与"1概述"指向同一内容） |
| 章节标题截断 | 20字符上限 | 防止PDF正文内容泄漏到章节标题（如"3.2.1系统和设备分级应进行分类..."） |
| 子串匹配过滤 | 跳过纯章节号键 | 防止短章节号（如"3.2.2"）通过子串匹配抢走子节（如"3.2.2.2F-SC-2级要求"）的匹配 |
| 正向矩阵列结构 | 6列（移除追踪匹配说明） | 后续需人工审核，自动化标色说明列暂不生成 |
| 未追踪条目处理 | 下游填NA | 无追踪关系的用户需求条目，下游文档/条目号/内容统一填"NA"，匹配说明列移除 |
