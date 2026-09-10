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
    # 候选发现层元数据(三模式接入)
    relation_source: str = 'table'   # 'table' | 'discover' | 'hybrid'
    candidate_score: float = 0.0     # 发现层融合置信度
    evidence: str = ''               # 通道证据摘要
    ambiguous: bool = False          # top1与top2接近, 待人工裁决
    # LLM裁决器支持字段
    ai_opinion: str = ''             # AI审核意见(Excel第12列)
    discovery_candidates: list = field(default_factory=list)
    # top候选完整信息 [{'doc','key','content','score','evidence'}],
    # 供LLM裁决时注入候选证据, 不进入Excel输出


class TraceabilityMatrix:
    """追踪矩阵数据容器"""

    def __init__(self):
        self.rows: list[TraceabilityRow] = []
        self.backward_merge_groups: dict[int, list[int]] = {}  # seq -> [row_indices]

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


def _run_candidate_discovery(downstream_pdf: str, upstream_pdfs: dict[str, str],
                             ds_doc_name: str | None = None):
    """运行候选发现层, 返回裁决列表(不依赖追踪关系表)"""
    from core.candidate_discovery import CandidateDiscovery, unitize_pdf
    if ds_doc_name is None:
        ds_doc_name = os.path.splitext(os.path.basename(downstream_pdf))[0]
    ds_units = unitize_pdf(downstream_pdf, ds_doc_name, role='downstream')
    up_units = []
    for doc_name, pdf_path in upstream_pdfs.items():
        up_units.extend(unitize_pdf(pdf_path, doc_name, role='upstream'))
    print(f"    [discover] 下游单元 {len(ds_units)}, 上游单元 {len(up_units)}")
    disc = CandidateDiscovery(ds_units, up_units, verbose=True)
    return disc.run()


def build_backward_matrix(
    downstream_pdf: str,
    upstream_pdfs: dict[str, str],
    mode: str = 'table',
) -> TraceabilityMatrix:
    """
    构建逆向追踪矩阵

    Args:
        downstream_pdf: 下游文档PDF路径
        upstream_pdfs: 上游文档映射 {文档名(不含.pdf): PDF路径}
            如 {"DCS设备技术规格书": "path/to/file.pdf", "RPS系统需求规范书": "..."}
        mode: 运行模式
            'table'   (默认) 有表走表, 行为零变化
            'discover' 忽略追踪表, 纯内容候选发现
            'hybrid'  以表为准, 发现结果用于补漏(added)和纠偏(suspicious)

    Returns:
        TraceabilityMatrix: 构建完成的逆向追踪矩阵
    """
    if mode not in ('table', 'discover', 'hybrid'):
        raise ValueError(f"未知的运行模式: {mode} (应为 table/discover/hybrid)")

    matrix = TraceabilityMatrix()

    # 0. 并行预解析所有PDF, 后续各步骤直接命中缓存(不改变任何解析结果)
    ds_doc_name = os.path.splitext(os.path.basename(downstream_pdf))[0]
    prefetch_pdfs({ds_doc_name: downstream_pdf, **upstream_pdfs})

    # discover模式: 不解析追踪表, 直接走候选发现层
    if mode == 'discover':
        print(">>> 运行模式: discover (纯内容候选发现, 忽略追踪关系表)")
        decisions = _run_candidate_discovery(downstream_pdf, upstream_pdfs, ds_doc_name)
        for row in _discovery_to_rows(decisions, 'discover'):
            matrix.add_row(row)
        matrix.compute_backward_merge_groups()
        n_traced = sum(1 for d in decisions if d.status == 'traced')
        dump_extract_summary(downstream_pdf, {
            "role": "系统需求(下游)", "mode": "discover",
            "n_downstream_units": len(decisions),
            "n_traced": n_traced, "n_untraced": len(decisions) - n_traced,
            "n_matrix_rows": len(matrix.rows),
        })
        return matrix

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

    # hybrid模式: 以表为准, 发现结果用于补漏(added)和纠偏(suspicious)
    if mode == 'hybrid':
        print(">>> 运行模式: hybrid (表为准, 发现补漏/纠偏)")
        decisions = _run_candidate_discovery(downstream_pdf, upstream_pdfs, ds_doc_name)
        from core.candidate_discovery import ref_matches_unit

        table_ds_cores = {_id_core(ds_id) for ds_id in ds_groups}
        n_added = n_suspicious = 0
        for dec in decisions:
            ds_core = _id_core(dec.ds_unit.key)
            if ds_core not in table_ds_cores:
                # 补漏: 表中无该条目的关系, 追加发现结果
                for row in _discovery_to_rows([dec], 'hybrid'):
                    row.seq_number = seq
                    if row.upstream_ref:
                        row.evidence = '[added] ' + row.evidence
                        n_added += 1
                    matrix.add_row(row)
                seq += 1
                continue
            # 表中已有该条目: 标注发现层校验结论(一致/次要支持/未支持/未溯源)
            _mark_hybrid_consensus(matrix, dec, ds_core)
            # 纠偏: 发现main与表中关系不一致时标记suspicious
            if dec.status != 'traced' or dec.main is None:
                continue
            matched = any(
                _id_core(r.downstream_id) == ds_core and r.upstream_ref
                and r.upstream_doc == dec.main.unit.doc
                and ref_matches_unit(r.upstream_ref, dec.main.unit)
                for r in matrix.rows
            )
            if not matched:
                for r in matrix.rows:
                    if _id_core(r.downstream_id) == ds_core and r.relation_source == 'table':
                        r.relation_source = 'hybrid'
                        r.evidence = (f"[suspicious] 发现建议 {dec.main.unit.doc}:"
                                      f"{dec.main.unit.key[:30]}({dec.main.score:.2f})")
                        r.discovery_candidates = _dec_to_disc_cands(dec)
                        n_suspicious += 1
                        break
        print(f"    [hybrid] 补漏 {n_added} 行, 纠偏标记 {n_suspicious} 行")

    matrix.compute_backward_merge_groups()
    dump_extract_summary(downstream_pdf, {  # 中间产物: 提取阶段
        "role": "系统需求(下游)",
        "mode": mode,
        "n_relations": len(relations),
        "n_downstream_items": len(downstream_items),
        "n_upstream_docs": len(upstream_maps),
        "n_matrix_rows": len(matrix.rows),
        "n_empty_upstream_content": sum(1 for r in matrix.rows if not r.upstream_content.strip()),
    })
    return matrix


def _dec_to_disc_cands(dec) -> list[dict]:
    """发现的top候选 -> LLM裁决证据结构 [{'doc','key','content','score','evidence'}]

    保留前5名: 未溯源组的LLM审查需要更大审查面(融合排序不可靠时,
    正确答案可能排在第4~5名), 见 config.LLM_UNTRACED_CANDS。
    """
    from core.candidate_discovery import format_evidence
    return [{
        'doc': c.unit.doc, 'key': c.unit.key, 'content': c.unit.content,
        'score': c.score, 'evidence': format_evidence(c.evidence),
    } for c in (dec.top_candidates or [])[:5]]


def _mark_hybrid_consensus(matrix, dec, ds_core: str, doc_name: str = '') -> None:
    """
    hybrid模式: 对表中已有的该下游条目各行, 标注发现层校验结论。

    让"以表为准"的校验价值可见 — 否则表完整且一致时输出与table模式
    逐字节相同, 无法分辨发现层是否真的运行过。结论写入evidence列:
    - 发现层一致(置信):  表关系即发现层主关系
    - 发现层次要支持:     表关系在发现层次级关系(一对多)中
    - 发现层未支持:       发现层主/次级均不包含该表关系(软信号, 提示复核)
    - 发现层未溯源:       发现层未找到达标关系(表中关系值得人工复核)

    doc_name 非空时仅触碰该下游文档的行(多设计文档场景下,
    不同文档可能存在同核心ID的条目, 防止跨文档污染)。
    """
    from core.candidate_discovery import ref_matches_unit

    if doc_name:
        rows = [r for r in matrix.rows
                if _id_core(r.downstream_id) == ds_core
                and r.downstream_doc == doc_name
                and r.relation_source == 'table']
    else:
        rows = [r for r in matrix.rows
                if _id_core(r.downstream_id) == ds_core and r.relation_source == 'table']
    if not rows:
        return

    if dec.status != 'traced' or dec.main is None:
        best = f"({dec.top_candidates[0].score:.2f})" if dec.top_candidates else ''
        for r in rows:
            r.evidence = (r.evidence + ' ' if r.evidence else '') + \
                f'发现层未溯源{best}'
        return

    main_u = dec.main
    sec_us = list(dec.secondaries or [])

    def _hits(cand) -> bool:
        return any(r.upstream_doc == cand.unit.doc and r.upstream_ref
                  and ref_matches_unit(r.upstream_ref, cand.unit) for r in rows)

    main_hit = _hits(main_u)
    for r in rows:
        tag = None
        if main_hit and r.upstream_doc == main_u.unit.doc and r.upstream_ref \
                and ref_matches_unit(r.upstream_ref, main_u.unit):
            tag = f'发现层一致({main_u.score:.2f})'
        elif any(r.upstream_doc == s.unit.doc and r.upstream_ref
                 and ref_matches_unit(r.upstream_ref, s.unit) for s in sec_us):
            tag = '发现层次要支持'
        else:
            tag = '发现层未支持'
        r.evidence = (r.evidence + ' ' if r.evidence else '') + tag


def _discovery_to_rows(decisions, relation_source: str,
                       start_seq: int = 1, downstream_doc: str = '') -> list[TraceabilityRow]:
    """
    将候选发现裁决转换为矩阵行(不依赖追踪关系表)

    每个下游单元一组(同一seq_number):
    - traced: main为主行, secondaries为次级行(一对多)
    - untraced: 输出空溯源行 + top3参考候选(写入evidence, 供人工审查)

    Args:
        decisions: CandidateDiscovery.run() 的输出
        relation_source: 'discover' | 'hybrid'
        start_seq: 起始序号(多文档时续编)
        downstream_doc: 该行所属下游文档名(多设计文档时用于分sheet)

    Returns:
        list[TraceabilityRow]
    """
    from core.candidate_discovery import format_evidence, format_untraced_evidence

    rows: list[TraceabilityRow] = []
    seq = start_seq
    for dec in decisions:
        ds_id = dec.ds_unit.key
        ds_content = dec.ds_unit.content

        # top候选完整信息(LLM裁决证据来源; 含content, 不进Excel)
        disc_cands = _dec_to_disc_cands(dec)

        if dec.status == 'untraced':
            rows.append(TraceabilityRow(
                seq_number=seq,
                downstream_id=ds_id,
                downstream_content=ds_content,
                upstream_doc='',
                upstream_ref='',
                upstream_content='',
                downstream_doc=downstream_doc,
                relation_source=relation_source,
                candidate_score=0.0,
                evidence=format_untraced_evidence(dec),
                ambiguous=False,
                discovery_candidates=disc_cands,
            ))
            seq += 1
            continue

        # traced: main + secondaries
        cands = [dec.main] + list(dec.secondaries)
        for cand in cands:
            if cand is None:
                continue
            rows.append(TraceabilityRow(
                seq_number=seq,
                downstream_id=ds_id,
                downstream_content=ds_content,
                upstream_doc=cand.unit.doc,
                upstream_ref=cand.unit.key,
                upstream_content=cand.unit.content,
                downstream_doc=downstream_doc,
                relation_source=relation_source,
                candidate_score=cand.score,
                evidence=format_evidence(cand.evidence),
                ambiguous=dec.ambiguous,
                discovery_candidates=disc_cands,
            ))
        seq += 1

    return rows


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
    mode: str = 'table',
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
        mode: 运行模式 'table' | 'discover' | 'hybrid'

    Returns:
        TraceabilityMatrix: 构建完成的逆向追踪矩阵
    """
    if mode not in ('table', 'discover', 'hybrid'):
        raise ValueError(f"未知的运行模式: {mode} (应为 table/discover/hybrid)")

    matrix = TraceabilityMatrix()

    # 0. 并行预解析所有PDF, 后续各步骤直接命中缓存(不改变任何解析结果)
    prefetch_pdfs({'系统需求': sys_req_pdf, **design_pdfs})

    # discover模式: 不解析追踪表, 每份设计文档独立走候选发现层
    if mode == 'discover':
        print(">>> 运行模式: discover (纯内容候选发现, 忽略追踪关系表)")
        seq = 1
        for doc_name, pdf_path in design_pdfs.items():
            print(f"    处理设计文档: {doc_name}")
            decisions = _run_candidate_discovery(
                pdf_path, {'系统需求': sys_req_pdf}, doc_name)
            for row in _discovery_to_rows(decisions, 'discover',
                                          start_seq=seq, downstream_doc=doc_name):
                matrix.add_row(row)
                seq = row.seq_number + 1
        matrix.compute_backward_merge_groups()
        dump_extract_summary(list(design_pdfs.values())[0], {
            "role": "系统设计(下游)", "mode": "discover",
            "n_design_docs": len(design_pdfs),
            "n_matrix_rows": len(matrix.rows),
        })
        return matrix

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

    # hybrid模式: 以表为准, 发现结果用于补漏(added)和纠偏(suspicious)
    if mode == 'hybrid':
        print(">>> 运行模式: hybrid (表为准, 发现补漏/纠偏)")
        from core.candidate_discovery import ref_matches_unit

        # 按 (doc_name, core ID) 作键: 多份设计文档可能包含同核心的条目,
        # 不能把"存在于另一份文档的表"当成"本文档表已存在"(跨文档污染)。
        table_ds_keys = {(doc, _id_core(ds_id)) for (doc, ds_id) in ds_groups}
        n_added = n_suspicious = 0
        for doc_name, pdf_path in design_pdfs.items():
            decisions = _run_candidate_discovery(
                pdf_path, {'系统需求': sys_req_pdf}, doc_name)
            for dec in decisions:
                ds_core = _id_core(dec.ds_unit.key)
                if (doc_name, ds_core) not in table_ds_keys:
                    # 补漏: 表中无该条目的关系, 追加发现结果
                    for row in _discovery_to_rows([dec], 'hybrid',
                                                  start_seq=seq, downstream_doc=doc_name):
                        if row.upstream_ref:
                            row.evidence = '[added] ' + row.evidence
                            n_added += 1
                        matrix.add_row(row)
                        seq = row.seq_number + 1
                    continue
                # 表中已有该条目: 标注发现层校验结论(限定本文档的行)
                _mark_hybrid_consensus(matrix, dec, ds_core, doc_name=doc_name)
                # 纠偏: 发现main与表中关系不一致时标记suspicious
                if dec.status != 'traced' or dec.main is None:
                    continue
                matched = any(
                    _id_core(r.downstream_id) == ds_core and r.upstream_ref
                    and r.downstream_doc == doc_name
                    and r.upstream_doc == dec.main.unit.doc
                    and ref_matches_unit(r.upstream_ref, dec.main.unit)
                    for r in matrix.rows
                )
                if not matched:
                    for r in matrix.rows:
                        if (_id_core(r.downstream_id) == ds_core
                                and r.downstream_doc == doc_name
                                and r.relation_source == 'table'):
                            r.relation_source = 'hybrid'
                            r.evidence = (f"[suspicious] 发现建议 {dec.main.unit.doc}:"
                                          f"{dec.main.unit.key[:30]}({dec.main.score:.2f})")
                            r.discovery_candidates = _dec_to_disc_cands(dec)
                            n_suspicious += 1
                            break
        print(f"    [hybrid] 补漏 {n_added} 行, 纠偏标记 {n_suspicious} 行")

    matrix.compute_backward_merge_groups()
    dump_extract_summary(list(design_pdfs.values())[0], {  # 中间产物: 提取阶段
        "role": "系统设计(下游)",
        "mode": mode,
        "n_design_docs": len(design_pdfs),
        "n_relations_total": len(all_relations),
        "n_design_items": sum(len(m) for m in design_maps.values()),
        "n_matrix_rows": len(matrix.rows),
        "n_empty_upstream_content": sum(1 for r in matrix.rows if not r.upstream_content.strip()),
    })
    return matrix
