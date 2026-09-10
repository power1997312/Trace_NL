from __future__ import annotations
"""
正向追踪矩阵生成模块（系统需求 → 系统设计）

从已完成的逆向追踪矩阵Excel（系统设计→系统需求）中提取匹配数据，
结合系统需求PDF的完整条目结构，生成正向追踪矩阵（系统需求 → 系统设计）。

公共实现(XML读取/条目提取/引用匹配/union着色/Excel输出)位于 core/forward_common.py，
本模块仅保留流水线B特有的数据构建策略：对每条(上游,下游)重新执行微块级文本匹配，
并补全引用匹配不到PDF条目的孤儿上游条目。

核心逻辑:
1. 从系统需求PDF提取所有条目 (完整覆盖)
2. 从逆向矩阵获取追踪关系和匹配着色
3. 按系统需求条目分组, 上游内容合并着色 (union coloring: GREEN > BLUE > BLACK)
4. 未追踪的系统需求条目, 下游填"NA"
"""
import os
from collections import OrderedDict, defaultdict

from config import MatchCategory

from core.pdf_parser_adapter import prefetch_pdfs
from core.forward_common import (
    read_reverse_matrix_data, extract_pdf_items, build_item_key_map,
    match_ref_to_item, compute_union_coloring, dominant_category_from_runs,
    textruns_to_tuples, generate_forward_excel,
)


# ============================================================
# 正向矩阵数据构建 (流水线B: 重新微块匹配 + 孤儿补全)
# ============================================================

def _build_traced_entry(up_text, design_rows):
    """
    构建"有追踪关系"的正向条目, 并对每条(上游内容, 下游内容)对重新执行微块级文本匹配，
    不直接沿用逆向矩阵中保存的原匹配结果。

    Args:
        up_text: 上游(系统需求)完整内容
        design_rows: 与该上游条目关联的所有逆向矩阵行(每条含 ds_id/ds_text/ds_doc)

    Returns:
        dict: 含 up_runs(union着色), ds_items(各自重新匹配的下游着色) 等
    """
    # 延迟导入: 仅在需要重新匹配时才加载匹配模型
    from core.text_matcher import match_text_pair

    up_text_full = up_text or ''
    design_items = []
    all_up_run_tuples = []

    for row in design_rows:
        ds_text = row['ds_text']
        # 重新匹配上游(系统需求)与下游(系统设计)内容
        mr = match_text_pair(ds_text, up_text_full)
        ds_run_tuples = textruns_to_tuples(mr.downstream_runs)
        up_run_tuples = textruns_to_tuples(mr.upstream_runs)
        design_items.append({
            'ds_id': row['ds_id'],
            'ds_text': ds_text,
            'ds_runs': ds_run_tuples,
            'ds_category': dominant_category_from_runs(ds_run_tuples),
            'ds_doc': row.get('ds_doc', ''),
        })
        all_up_run_tuples.append(up_run_tuples)

    union_runs = compute_union_coloring(all_up_run_tuples, up_text_full) if up_text_full else []

    return {
        'up_text': up_text_full,
        'up_runs': union_runs,
        'up_category': dominant_category_from_runs(union_runs) if union_runs else MatchCategory.BLACK,
        'ds_items': design_items,
        'has_trace': True,
    }


def _build_forward_data(reverse_rows, pdf_items_by_doc):
    """
    构建正向矩阵数据

    1. 以系统需求PDF完整条目为基准 (完整覆盖, 全部条目均补全内容)
    2. 匹配逆向矩阵行到PDF条目, 有追踪则对每条(上游,下游)重新执行文本匹配并合并着色
    3. 无追踪: 标记has_trace=False (下游填NA), 上游内容仍为PDF完整内容
    4. 逆向矩阵中引用匹配不到任何PDF条目的"孤儿"上游项, 用逆向矩阵记录的内容补全,
       避免追踪关系丢失 (满足"上游条目缺失需补全")

    Returns:
        OrderedDict: {(doc_name, item_key): {up_text, up_runs, up_category, ds_items, has_trace}}
    """
    forward_data = OrderedDict()

    # 为所有文档构建键映射
    doc_key_maps = {}
    for doc_name, pdf_items in pdf_items_by_doc.items():
        doc_key_maps[doc_name] = build_item_key_map(pdf_items)

    # 将每条逆向矩阵行匹配到对应PDF条目 (跨文档搜索)
    doc_item_to_rows = {doc_name: defaultdict(list) for doc_name in pdf_items_by_doc}
    orphan_rows = []  # 引用匹配不到任何PDF条目的逆向行
    for row in reverse_rows:
        matched = False
        for doc_name, pdf_items in pdf_items_by_doc.items():
            if row.get('up_doc', '') == doc_name or row.get('up_doc', '') in doc_name or doc_name in row.get('up_doc', ''):
                idx = match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    matched = True
                    break
        if not matched:
            for doc_name, pdf_items in pdf_items_by_doc.items():
                idx = match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    matched = True
                    break
        if not matched:
            orphan_rows.append(row)

    # 1) 以PDF完整条目为基准构建正向数据
    for doc_name, pdf_items in pdf_items_by_doc.items():
        item_to_rows = doc_item_to_rows[doc_name]
        for item_idx, item in enumerate(pdf_items):
            key = (doc_name, item['key'])
            matching_rows = item_to_rows.get(item_idx, [])
            if matching_rows:
                # 上游内容统一使用PDF完整内容 (若为空则回退逆向矩阵记录内容)
                up_text_full = item['content'] or matching_rows[0]['up_text']
                entry = _build_traced_entry(up_text_full, matching_rows)
                forward_data[key] = entry
            else:
                # 无追踪关系: 上游内容仍为PDF完整内容, 下游填NA
                forward_data[key] = {
                    'up_text': item['content'],
                    'up_runs': None,
                    'up_category': MatchCategory.BLACK,
                    'ds_items': [],
                    'has_trace': False,
                }

    # 2) 补全孤儿上游条目: 引用匹配不到PDF条目, 用逆向矩阵记录的内容补全, 保留追踪关系
    if orphan_rows:
        orphan_groups = defaultdict(list)
        for row in orphan_rows:
            up_doc = row.get('up_doc', '')
            target_doc = None
            for dn in pdf_items_by_doc:
                if dn == up_doc or up_doc in dn or dn in up_doc:
                    target_doc = dn
                    break
            if target_doc is None:
                # 单上游文档场景: 归入唯一文档
                target_doc = next(iter(pdf_items_by_doc))
            orphan_groups[(target_doc, row['up_ref'])].append(row)

        for (up_doc, up_ref), rows in orphan_groups.items():
            key = (up_doc, up_ref)
            if key in forward_data:
                # 与已存在条目合并: 收集全部设计行后整体重新匹配(保证union着色正确)
                existing = forward_data[key]
                combined = [
                    {'ds_id': d['ds_id'], 'ds_text': d['ds_text'], 'ds_doc': d.get('ds_doc', '')}
                    for d in existing['ds_items']
                ]
                combined.extend(rows)
                forward_data[key] = _build_traced_entry(existing['up_text'], combined)
                continue
            # 用逆向矩阵记录的上游内容补全该缺失条目
            up_text = rows[0].get('up_text', '')
            forward_data[key] = _build_traced_entry(up_text, rows)

    return forward_data


def _split_by_doc(forward_data):
    """按文档名拆分"""
    by_doc = {}
    for (up_doc, up_ref), data in forward_data.items():
        if up_doc not in by_doc:
            by_doc[up_doc] = OrderedDict()
        by_doc[up_doc][(up_doc, up_ref)] = data
    return by_doc


def _infer_design_doc_name(design_item_id, design_doc_names):
    """
    从系统设计条目号推断所属的系统设计文档名

    系统设计条目号通常包含文档标识，如 <DESIGN-A001> 中的 "DESIGN-A"
    实际实现时根据项目情况调整

    Args:
        design_item_id: 系统设计条目号 (如 <DCS-DS001>)
        design_doc_names: 已知的系统设计文档名列表

    Returns:
        str: 推断出的文档名，或空字符串
    """
    if not design_item_id or not design_doc_names:
        return ''

    # 尝试从条目ID中提取文档标识前缀
    # 例如 <DCS-DS001> 中的 "DCS" 或 "DCS-DS"
    id_upper = design_item_id.upper()
    for doc_name in design_doc_names:
        # 检查文档名是否出现在条目ID中（去空格后）
        doc_key = doc_name.replace(' ', '').upper()
        if doc_key[:4] in id_upper or id_upper[:4] in doc_key:
            return doc_name

    # 兜底: 返回第一个文档名
    return design_doc_names[0] if design_doc_names else ''


# ============================================================
# 主入口
# ============================================================

def generate_forward_sd_matrix(
    reverse_matrix_path,
    sys_req_pdf_dir,
    output_path,
    design_pdf_dir=None,
):
    """
    从已完成的逆向追踪矩阵生成正向追踪矩阵（系统需求 → 系统设计）

    流程:
    1. 读取逆向矩阵Excel, 提取追踪关系及匹配颜色
    2. 从系统需求PDF提取所有条目 (完整覆盖)
    3. 匹配逆向矩阵行到系统需求条目, 计算union coloring
    4. 按文档拆分, 生成Excel
    5. 未追踪条目下游填NA

    Args:
        reverse_matrix_path: 逆向追踪矩阵Excel路径 (含匹配着色)
        sys_req_pdf_dir: 系统需求PDF目录路径
        output_path: 输出正向矩阵Excel路径
        design_pdf_dir: 系统设计PDF目录路径 (用于获取下游文档名)
    """
    # 0. 获取系统设计文档名列表
    design_doc_names = []
    if design_pdf_dir and os.path.isdir(design_pdf_dir):
        for fname in sorted(os.listdir(design_pdf_dir)):
            if fname.lower().endswith('.pdf'):
                design_doc_names.append(fname.replace('.pdf', ''))

    # 1. 读取逆向矩阵数据
    print(">>> 读取逆向追踪矩阵数据...")
    reverse_rows = read_reverse_matrix_data(reverse_matrix_path)
    print(f"    读取到 {len(reverse_rows)} 条追踪关系")

    # 2. 从系统需求PDF提取所有条目
    print(">>> 提取系统需求PDF条目...")
    pdf_items_by_doc = {}
    if os.path.isdir(sys_req_pdf_dir):
        # 多份文档先并行预解析, 下面的循环直接命中缓存
        prefetch_pdfs({
            f.replace('.pdf', ''): os.path.join(sys_req_pdf_dir, f)
            for f in sorted(os.listdir(sys_req_pdf_dir)) if f.lower().endswith('.pdf')
        })
        for fname in sorted(os.listdir(sys_req_pdf_dir)):
            if not fname.lower().endswith('.pdf'):
                continue
            doc_name = fname.replace('.pdf', '')
            pdf_path = os.path.join(sys_req_pdf_dir, fname)
            items = extract_pdf_items(pdf_path, doc_name)
            pdf_items_by_doc[doc_name] = items
            print(f"    [{doc_name}]: {len(items)} 个条目")

    # 3. 构建正向矩阵数据 (匹配 + union coloring)
    print(">>> 构建正向矩阵数据 (匹配 + union coloring)...")
    forward_data = _build_forward_data(reverse_rows, pdf_items_by_doc)

    # 统计
    traced = sum(1 for d in forward_data.values() if d['has_trace'])
    untraced = len(forward_data) - traced
    total_design = sum(len(d['ds_items']) for d in forward_data.values() if d['has_trace'])
    print(f"    总条目: {len(forward_data)}, 有追踪: {traced}, 未追踪: {untraced}")
    print(f"    系统设计追踪关系: {total_design}")

    # 4. 按文档拆分
    by_doc = _split_by_doc(forward_data)

    # 过滤: 仅保留系统需求目录中存在的PDF文档
    doc_names = set(pdf_items_by_doc.keys())
    if doc_names:
        by_doc = {k: v for k, v in by_doc.items() if k in doc_names}

    for doc_name, entries in by_doc.items():
        n = len(entries)
        t = sum(1 for d in entries.values() if d['has_trace'])
        print(f"    [{doc_name}]: {n} 个条目 ({t} 有追踪, {n-t} 未追踪)")

    # 5. 生成Excel (D列下游文档名: 优先逆向矩阵记录的ds_doc, 缺失时按条目号前缀推断)
    print(f">>> 生成正向追踪矩阵Excel...")
    generate_forward_excel(
        by_doc, output_path,
        lambda it: it.get('ds_doc') or _infer_design_doc_name(it['ds_id'], design_doc_names),
    )
    print(f"    已保存至 {output_path}")

    return {
        'total_entries': len(forward_data),
        'traced': traced,
        'untraced': untraced,
        'total_design_items': total_design,
        'doc_count': len(by_doc),
    }


if __name__ == "__main__":
    import sys
    from config import PROJECT_ROOT, OUTPUT_DIR, make_output_path, SYSTEM_DESIGN_DIR, SYSTEM_REQUIREMENT_DIR

    reverse_matrix_path = os.path.join(OUTPUT_DIR, "追踪验证结果_系统设计.xlsx")
    sys_req_pdf_dir = SYSTEM_REQUIREMENT_DIR
    design_pdf_dir = SYSTEM_DESIGN_DIR
    output_path = make_output_path("正向追踪矩阵_系统设计.xlsx")

    # 确保目录存在
    if not os.path.isdir(sys_req_pdf_dir):
        print(f"[ERROR] 系统需求PDF目录不存在: {sys_req_pdf_dir}")
        sys.exit(1)
    if not os.path.isdir(design_pdf_dir):
        print(f"[WARN] 系统设计PDF目录不存在: {design_pdf_dir}")
        design_pdf_dir = None

    if not os.path.exists(reverse_matrix_path):
        print(f"[ERROR] 逆向矩阵文件不存在: {reverse_matrix_path}")
        print("请先运行逆向矩阵生成脚本。")
        sys.exit(1)

    result = generate_forward_sd_matrix(
        reverse_matrix_path, sys_req_pdf_dir, output_path, design_pdf_dir,
    )
    print(f"\n=== 正向追踪矩阵（系统需求→系统设计）生成完成 ===")
    print(f"  文档数: {result['doc_count']}")
    print(f"  总条目: {result['total_entries']}")
    print(f"  有追踪: {result['traced']}, 未追踪: {result['untraced']}")
    print(f"  系统设计追踪: {result['total_design_items']}")
