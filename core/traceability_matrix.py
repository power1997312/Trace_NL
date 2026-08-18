from __future__ import annotations
"""
追踪矩阵构建模块
- 逆向追踪矩阵: 下游条目 -> 上游条目
- 正向追踪矩阵: 上游条目 -> 下游条目
- 支持一对多合并组计算
"""
import os
import re
from dataclasses import dataclass, field
from collections import defaultdict

from core.pdf_parser_adapter import (
    extract_full_text, find_traceability_table, parse_traceability_table,
    find_traceability_table_design, parse_traceability_table_design,
    prefetch_pdfs, TraceRelation,
)
from core.requirement_extractor import (
    build_content_map, extract_requirement_items, extract_sections,
    detect_document_type, ContentMap,
)
# 中间产物 dump(便于排查解析/提取问题)
from core.debug_dump import (
    dump_requirement_items, dump_sections, dump_relations,
    dump_content_map, dump_extract_summary,
)


@dataclass
class TraceabilityRow:
    """追踪矩阵中的一行"""
    seq_number: int
    downstream_id: str
    downstream_content: str
    upstream_doc: str
    upstream_ref: str
    upstream_content: str
    downstream_doc: str = ''  # 该行所属下游文档名(用于按文档分sheet, 问题2)
    match_result: object = None  # MatchResult, 验证阶段填充


class TraceabilityMatrix:
    """追踪矩阵数据容器"""

    def __init__(self):
        self.rows: list[TraceabilityRow] = []
        self.backward_merge_groups: dict[int, list[int]] = {}  # seq -> [row_indices]
        self.forward_merge_groups: dict[int, list[int]] = {}

    def add_row(self, row: TraceabilityRow):
        self.rows.append(row)

    def compute_backward_merge_groups(self):
        """
        计算逆向矩阵合并组
        当同一下游条目对应多个上游条目时，下游侧(A/B/C列)需要合并
        """
        groups = defaultdict(list)
        for i, row in enumerate(self.rows):
            groups[row.seq_number].append(i)
        # 只保留需要合并的组(行数>1)
        self.backward_merge_groups = {
            seq: indices for seq, indices in groups.items() if len(indices) > 1
        }

    def compute_forward_merge_groups(self, forward_rows: list):
        """
        计算正向矩阵合并组
        当同一上游条目对应多个下游条目时，上游侧(A/B/C列)需要合并
        """
        groups = defaultdict(list)
        for i, row in enumerate(forward_rows):
            groups[row['seq_number']].append(i)
        self.forward_merge_groups = {
            seq: indices for seq, indices in groups.items() if len(indices) > 1
        }

    def to_forward_format(self) -> list[dict]:
        """
        将逆向矩阵转换为正向矩阵格式

        Returns:
            list[dict]: 正向矩阵行列表, 每行包含:
                seq_number, upstream_ref, upstream_content, upstream_doc,
                downstream_id, downstream_content
        """
        # 按(upstream_doc, upstream_ref)分组
        groups = defaultdict(list)
        for row in self.rows:
            key = (row.upstream_doc, row.upstream_ref)
            groups[key].append(row)

        forward_rows = []
        seq = 1
        for (upstream_doc, upstream_ref), source_rows in groups.items():
            for sr in source_rows:
                forward_rows.append({
                    'seq_number': seq,
                    'upstream_ref': upstream_ref,
                    'upstream_content': sr.upstream_content,
                    'upstream_doc': upstream_doc,
                    'downstream_id': sr.downstream_id,
                    'downstream_content': sr.downstream_content,
                    'match_result': sr.match_result,
                })
            seq += 1

        return forward_rows


def build_backward_matrix(
    downstream_pdf: str,
    upstream_pdfs: dict[str, str],
) -> TraceabilityMatrix:
    """
    从下游PDF的附录追踪矩阵表构建逆向追踪矩阵

    Args:
        downstream_pdf: 下游文档PDF路径
        upstream_pdfs: 上游文档映射 {文档名(不含.pdf): PDF路径}
            如 {"DCS设备技术规格书": "path/to/file.pdf", "RPS系统需求规范书": "..."}

    Returns:
        TraceabilityMatrix: 构建完成的逆向追踪矩阵
    """
    matrix = TraceabilityMatrix()

    # 0. 并行预解析所有PDF, 后续各步骤直接命中缓存(不改变任何解析结果)
    ds_doc_name = os.path.splitext(os.path.basename(downstream_pdf))[0]
    prefetch_pdfs({ds_doc_name: downstream_pdf, **upstream_pdfs})

    # 1. 解析下游PDF中的追踪矩阵附录表
    table = find_traceability_table(downstream_pdf)
    if not table:
        raise ValueError(f"未在 {downstream_pdf} 中找到追踪矩阵附录表")

    relations = parse_traceability_table(table)
    if not relations:
        raise ValueError("追踪矩阵表解析结果为空")
    dump_relations(downstream_pdf, relations)  # 中间产物: 提取阶段

    # 2. 提取下游文档的内容映射
    downstream_text = extract_full_text(downstream_pdf)
    downstream_doc_type = detect_document_type(downstream_text)
    downstream_items = extract_requirement_items(downstream_text)
    dump_requirement_items(downstream_pdf, downstream_items, downstream_doc_type)
    dump_sections(downstream_pdf, extract_sections(downstream_text), downstream_doc_type)
    downstream_map = {item.item_id: item.content for item in downstream_items}

    # 3. 为每个上游文档构建内容映射
    upstream_maps: dict[str, ContentMap] = {}
    for doc_name, pdf_path in upstream_pdfs.items():
        text = extract_full_text(pdf_path)
        up_doc_type = detect_document_type(text)
        upstream_maps[doc_name] = build_content_map(text, doc_name)
        dump_requirement_items(pdf_path, extract_requirement_items(text), up_doc_type)
        dump_sections(pdf_path, extract_sections(text), up_doc_type)
        dump_content_map(pdf_path, upstream_maps[doc_name])

    # 4. 构建矩阵行
    # 按下游条目分组，分配序号
    ds_groups = defaultdict(list)
    for rel in relations:
        ds_id = _normalize_ds_id(rel.downstream_id)
        ds_groups[ds_id].append(rel)

    seq = 1
    for ds_id, rels in ds_groups.items():
        # 获取下游内容
        ds_content = downstream_map.get(ds_id, '')
        if not ds_content:
            # 尝试模糊匹配: 追踪表中的ds_id可能包含标题(如"<ID> 标题"),
            # 而downstream_map的key仅为ID(如"<ID>"), 需检查key是否为ds_id的子串
            for key, val in downstream_map.items():
                if key.replace(' ', '') in ds_id.replace(' ', '') or ds_id.replace(' ', '') in key.replace(' ', ''):
                    ds_content = val
                    break

        for rel in rels:
            # 查找上游内容
            doc_key = _match_doc_name(rel.upstream_doc, upstream_maps)
            up_content = ''
            if doc_key and doc_key in upstream_maps:
                up_content = upstream_maps[doc_key].resolve(rel.upstream_ref) or ''

            # 不在其他文档中回退查找(防止跨文档内容串稿)

            matrix.add_row(TraceabilityRow(
                seq_number=seq,
                downstream_id=ds_id,
                downstream_content=ds_content,
                upstream_doc=rel.upstream_doc,
                upstream_ref=rel.upstream_ref,
                upstream_content=up_content,
            ))

        seq += 1

    matrix.compute_backward_merge_groups()
    dump_extract_summary(downstream_pdf, {  # 中间产物: 提取阶段
        "role": "系统需求(下游)",
        "n_relations": len(relations),
        "n_downstream_items": len(downstream_items),
        "n_upstream_docs": len(upstream_maps),
        "n_matrix_rows": len(matrix.rows),
        "n_empty_upstream_content": sum(1 for r in matrix.rows if not r.upstream_content.strip()),
    })
    return matrix


def _normalize_ds_id(raw_id: str) -> str:
    """归一化下游条目ID"""
    raw_id = raw_id.strip()
    if raw_id.startswith('<') and raw_id.endswith('>'):
        inner = raw_id[1:-1].strip()
        return f"<{inner}>"
    return raw_id


def _id_core(raw_id: str) -> str:
    """
    提取ID的核心可比较标识，用于跨文档前缀/零填充不一致时的匹配。

    例如:
      <FZSDCS34-SyRS005>  -> "syrs5"
      <DCS-SyRS005>       -> "syrs5"
      <FZSDCS34-SyRS0011> -> "syrs11"   (零填充/笔误归一)
      <DCS-SyRS011>       -> "syrs11"
    规则: 取ID中最后一组 (字母+数字)，字母转小写、数字按整数归一。
    """
    s = raw_id.strip().strip('<>').replace(' ', '').replace('\n', '')
    groups = re.findall(r'([A-Za-z]+?)(\d+)', s)
    if not groups:
        return s.lower()
    alpha, num = groups[-1]
    try:
        return f"{alpha.lower()}{int(num)}"
    except ValueError:
        return f"{alpha.lower()}{num}"


def _match_doc_name(ref_name: str, upstream_maps: dict[str, ContentMap]) -> str | None:
    """
    匹配文档名称（处理空格和格式差异）
    如 "DCS 设备技术规格书" 匹配 "DCS设备技术规格书"
    """
    ref_normalized = ref_name.replace(' ', '').strip()

    for doc_name in upstream_maps:
        if doc_name.replace(' ', '') == ref_normalized:
            return doc_name

    # 子串匹配
    for doc_name in upstream_maps:
        if ref_normalized in doc_name.replace(' ', '') or doc_name.replace(' ', '') in ref_normalized:
            return doc_name

    return None


def build_backward_matrix_from_design(
    design_pdfs: dict[str, str],
    sys_req_pdf: str,
) -> TraceabilityMatrix:
    """
    从多份系统设计文档的附录追踪矩阵表构建逆向追踪矩阵（系统设计→系统需求）

    与 build_backward_matrix 不同之处:
    - 多份下游文档（系统设计），每份有自己的追踪关系表
    - 只有一个上游文档（系统需求）
    - 追踪关系表有两种可能结构（Type A/B）

    Args:
        design_pdfs: 系统设计文档映射 {文档名(不含.pdf): PDF路径}
        sys_req_pdf: 系统需求文档PDF路径

    Returns:
        TraceabilityMatrix: 构建完成的逆向追踪矩阵
    """
    matrix = TraceabilityMatrix()

    # 0. 并行预解析所有PDF, 后续各步骤直接命中缓存(不改变任何解析结果)
    prefetch_pdfs({'系统需求': sys_req_pdf, **design_pdfs})

    # 1. 构建系统需求文档的内容映射（上游）
    sys_req_text = extract_full_text(sys_req_pdf)
    sys_req_doc_type = detect_document_type(sys_req_text)
    sys_req_items = extract_requirement_items(sys_req_text)
    dump_requirement_items(sys_req_pdf, sys_req_items, sys_req_doc_type)
    dump_sections(sys_req_pdf, extract_sections(sys_req_text), sys_req_doc_type)
    sys_req_map = {item.item_id: item.content for item in sys_req_items}
    print(f"    系统需求条目数: {len(sys_req_map)}")

    # 内容映射(含章节, 支持章节号追踪, 问题3)
    sys_req_content_map = build_content_map(sys_req_text)
    dump_content_map(sys_req_pdf, sys_req_content_map)

    # 核心ID索引（前缀/零填充归一），用于跨文档ID匹配
    sys_req_core = {_id_core(k): v for k, v in sys_req_map.items()}

    # 2. 遍历每份系统设计文档
    all_relations = []  # (design_doc_name, TraceRelation)

    for doc_name, pdf_path in design_pdfs.items():
        print(f"    处理设计文档: {doc_name}")

        # 2a. 查找追踪关系表
        table = find_traceability_table_design(pdf_path)
        if not table:
            print(f"    [WARN] 未在 {doc_name} 中找到追踪矩阵附录表，跳过")
            continue

        # 2b. 解析追踪关系
        relations = parse_traceability_table_design(table)
        if not relations:
            print(f"    [WARN] {doc_name} 追踪矩阵表解析结果为空，跳过")
            continue

        dump_relations(pdf_path, relations)  # 中间产物: 提取阶段
        print(f"        解析到 {len(relations)} 条追踪关系")

        for rel in relations:
            all_relations.append((doc_name, rel))

    if not all_relations:
        raise ValueError("所有系统设计文档的追踪关系表解析结果均为空")

    # 3. 构建每个设计文档的内容映射（下游）
    design_maps: dict[str, dict[str, str]] = {}
    for doc_name, pdf_path in design_pdfs.items():
        text = extract_full_text(pdf_path)
        design_doc_type = detect_document_type(text)
        design_items = extract_requirement_items(text)
        dump_requirement_items(pdf_path, design_items, design_doc_type)
        dump_sections(pdf_path, extract_sections(text), design_doc_type)
        design_map = {item.item_id: item.content for item in design_items}
        design_maps[doc_name] = design_map

    # 4. 构建矩阵行
    # 按(design_doc, design_id)分组，分配序号
    ds_groups = defaultdict(list)
    for doc_name, rel in all_relations:
        ds_id = _normalize_ds_id(rel.downstream_id)
        ds_groups[(doc_name, ds_id)].append((doc_name, rel))

    seq = 1
    for (doc_name, ds_id), items in ds_groups.items():
        # 获取下游（系统设计条目）内容
        design_map = design_maps.get(doc_name, {})
        ds_content = design_map.get(ds_id, '')
        if not ds_content:
            # 尝试模糊匹配
            for key, val in design_map.items():
                if ds_id.replace(' ', '') in key.replace(' ', ''):
                    ds_content = val
                    break

        for _, rel in items:
            # 查找上游（系统需求条目）内容
            up_ref = rel.upstream_ref
            # 归一化引用（可能是条目ID如 <DCS-SyRS001> 或章节号如 3、系统架构设计要求）
            up_ref_normalized = _normalize_ds_id(up_ref)
            # 优先用 ContentMap.resolve: 同时支持条目ID与章节号追踪(问题3)
            up_content = sys_req_content_map.resolve(up_ref) or ''

            if not up_content:
                up_content = sys_req_map.get(up_ref_normalized, '')

            if not up_content:
                # 尝试模糊匹配（子串）
                for key, val in sys_req_map.items():
                    if up_ref_normalized.replace(' ', '') in key.replace(' ', ''):
                        up_content = val
                        break

            if not up_content:
                # 核心ID匹配（容错前缀/零填充差异，如 FZSDCS34-SyRS005 vs DCS-SyRS005）
                up_content = sys_req_core.get(_id_core(up_ref_normalized), '')

            matrix.add_row(TraceabilityRow(
                seq_number=seq,
                downstream_id=ds_id,
                downstream_content=ds_content,
                upstream_doc=rel.upstream_doc,
                upstream_ref=up_ref,
                upstream_content=up_content,
                downstream_doc=doc_name,
            ))

        seq += 1

    matrix.compute_backward_merge_groups()
    dump_extract_summary(list(design_pdfs.values())[0], {  # 中间产物: 提取阶段
        "role": "系统设计(下游)",
        "n_design_docs": len(design_pdfs),
        "n_relations_total": len(all_relations),
        "n_design_items": sum(len(m) for m in design_maps.values()),
        "n_matrix_rows": len(matrix.rows),
        "n_empty_upstream_content": sum(1 for r in matrix.rows if not r.upstream_content.strip()),
    })
    return matrix
