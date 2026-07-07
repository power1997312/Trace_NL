"""
追踪矩阵构建模块
- 逆向追踪矩阵: 下游条目 -> 上游条目
- 正向追踪矩阵: 上游条目 -> 下游条目
- 支持一对多合并组计算
"""
import os
from dataclasses import dataclass, field
from collections import defaultdict

from core.pdf_parser import (
    extract_full_text, find_traceability_table, parse_traceability_table,
    TraceRelation,
)
from core.requirement_extractor import (
    build_content_map, extract_requirement_items, extract_sections,
    detect_document_type, ContentMap,
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

    # 1. 解析下游PDF中的追踪矩阵附录表
    table = find_traceability_table(downstream_pdf)
    if not table:
        raise ValueError(f"未在 {downstream_pdf} 中找到追踪矩阵附录表")

    relations = parse_traceability_table(table)
    if not relations:
        raise ValueError("追踪矩阵表解析结果为空")

    # 2. 提取下游文档的内容映射
    downstream_text = extract_full_text(downstream_pdf)
    downstream_items = extract_requirement_items(downstream_text)
    downstream_map = {item.item_id: item.content for item in downstream_items}

    # 3. 为每个上游文档构建内容映射
    upstream_maps: dict[str, ContentMap] = {}
    for doc_name, pdf_path in upstream_pdfs.items():
        text = extract_full_text(pdf_path)
        upstream_maps[doc_name] = build_content_map(text, doc_name)

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
            # 尝试模糊匹配
            for key, val in downstream_map.items():
                if ds_id.replace(' ', '') in key.replace(' ', ''):
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
    return matrix


def _normalize_ds_id(raw_id: str) -> str:
    """归一化下游条目ID"""
    raw_id = raw_id.strip()
    if raw_id.startswith('<') and raw_id.endswith('>'):
        inner = raw_id[1:-1].strip()
        return f"<{inner}>"
    return raw_id


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
