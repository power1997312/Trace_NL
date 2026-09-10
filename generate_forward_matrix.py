from __future__ import annotations
"""
正向追踪矩阵生成模块（用户需求 → 系统需求）

从已完成的逆向追踪矩阵Excel中提取匹配数据，结合用户需求PDF的完整章节结构，
生成正向追踪矩阵（用户需求 → 系统需求）。

公共实现(XML读取/条目提取/引用匹配/union着色/Excel输出)位于 core/forward_common.py，
本模块仅保留流水线A特有的数据构建策略：沿用逆向矩阵已保存的着色runs，不重新匹配。

核心逻辑:
1. 从用户需求PDF提取所有章节/条目 (完整覆盖)
2. 从逆向矩阵获取追踪关系和匹配着色
3. 按用户需求分组, 上游内容合并着色 (union coloring: GREEN > BLUE > BLACK)
4. 未追踪的用户需求, 下游填"NA"
"""
import os
from collections import OrderedDict, defaultdict

from config import MatchCategory

from core.pdf_parser_adapter import prefetch_pdfs
from core.forward_common import (
    read_reverse_matrix_data, extract_pdf_items, build_item_key_map,
    match_ref_to_item, compute_union_coloring, dominant_category_from_runs,
    generate_forward_excel,
)


# ============================================================
# 正向矩阵数据构建 (流水线A: 沿用逆向矩阵着色, 不重新匹配)
# ============================================================

def _build_forward_data(reverse_rows, pdf_items_by_doc):
    """
    构建正向矩阵数据

    1. 以PDF条目为基准 (完整覆盖)
    2. 匹配逆向矩阵行到PDF条目
    3. 有追踪: 收集所有up_runs, 计算union coloring
    4. 无追踪: 标记has_trace=False (下游填NA)

    Returns:
        OrderedDict: {(doc_name, item_key): {up_text, up_runs, up_category, ds_items, has_trace}}
    """
    forward_data = OrderedDict()

    # 为所有文档构建键映射
    doc_key_maps = {}
    for doc_name, pdf_items in pdf_items_by_doc.items():
        doc_key_maps[doc_name] = build_item_key_map(pdf_items)

    # 将每条逆向矩阵行匹配到对应PDF条目 (跨文档搜索)
    # up_doc可能是目录名(如"系统需求")而非PDF文件名(如"DCS需求说明书")
    # 因此不能仅靠up_doc过滤, 需要尝试在所有文档中匹配up_ref
    doc_item_to_rows = {doc_name: defaultdict(list) for doc_name in pdf_items_by_doc}
    for row in reverse_rows:
        matched = False
        # 优先在up_doc对应的文档中查找
        for doc_name, pdf_items in pdf_items_by_doc.items():
            if row.get('up_doc', '') == doc_name or row.get('up_doc', '') in doc_name or doc_name in row.get('up_doc', ''):
                idx = match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    matched = True
                    break
        # 如果up_doc不匹配任何文档名, 在所有文档中搜索
        if not matched:
            for doc_name, pdf_items in pdf_items_by_doc.items():
                idx = match_ref_to_item(row['up_ref'], doc_key_maps[doc_name], pdf_items)
                if idx is not None:
                    doc_item_to_rows[doc_name][idx].append(row)
                    break

    for doc_name, pdf_items in pdf_items_by_doc.items():
        item_to_rows = doc_item_to_rows[doc_name]

        # 为每个PDF条目构建正向数据
        for item_idx, item in enumerate(pdf_items):
            key = (doc_name, item['key'])
            matching_rows = item_to_rows.get(item_idx, [])

            if matching_rows:
                # 有追踪关系
                all_up_runs = [r['up_runs'] for r in matching_rows]
                up_text = matching_rows[0]['up_text']
                union_runs = compute_union_coloring(all_up_runs, up_text)

                if union_runs:
                    category = dominant_category_from_runs(union_runs)
                else:
                    category = matching_rows[0]['up_category']

                ds_items = [{
                    'ds_id': row['ds_id'],
                    'ds_text': row['ds_text'],
                    'ds_runs': row['ds_runs'],
                    'ds_category': row['ds_category'],
                    'ds_doc': row.get('ds_doc', ''),
                } for row in matching_rows]

                forward_data[key] = {
                    'up_text': up_text,
                    'up_runs': union_runs,
                    'up_category': category,
                    'ds_items': ds_items,
                    'has_trace': True,
                }
            else:
                # 无追踪关系
                forward_data[key] = {
                    'up_text': item['content'],
                    'up_runs': None,
                    'up_category': MatchCategory.BLACK,
                    'ds_items': [],
                    'has_trace': False,
                }

    return forward_data


def _split_by_user_doc(forward_data):
    """按用户需求文档名拆分"""
    by_doc = {}
    for (up_doc, up_ref), data in forward_data.items():
        if up_doc not in by_doc:
            by_doc[up_doc] = OrderedDict()
        by_doc[up_doc][(up_doc, up_ref)] = data
    return by_doc


# ============================================================
# 主入口
# ============================================================

def generate_forward_matrix(
    reverse_matrix_path,
    user_pdf_dir,
    output_path,
    sys_req_dir=None,
):
    """
    从已完成的逆向追踪矩阵生成正向追踪矩阵

    流程:
    1. 读取逆向矩阵Excel, 提取追踪关系及匹配颜色
    2. 从用户需求PDF提取所有章节/条目 (完整覆盖)
    3. 匹配逆向矩阵行到PDF条目, 计算union coloring
    4. 按用户需求文档拆分, 每份文档一个sheet
    5. 生成Excel, 上游内容合并着色, 未追踪条目下游填NA

    Args:
        reverse_matrix_path: 逆向追踪矩阵Excel路径 (含匹配着色)
        user_pdf_dir: 用户需求PDF目录路径
        output_path: 输出正向矩阵Excel路径
        sys_req_dir: 系统需求PDF目录路径 (用于获取下游文档名)
    """
    # 0. 获取系统需求文档名
    sys_req_doc_name = ''
    if sys_req_dir and os.path.isdir(sys_req_dir):
        for fname in os.listdir(sys_req_dir):
            if fname.lower().endswith('.pdf'):
                sys_req_doc_name = fname.replace('.pdf', '')
                break

    # 1. 读取逆向矩阵数据
    print(">>> 读取逆向追踪矩阵数据...")
    reverse_rows = read_reverse_matrix_data(reverse_matrix_path)
    print(f"    读取到 {len(reverse_rows)} 条追踪关系")

    # 2. 从上游PDF提取所有条目 (仅搜索user_pdf_dir)
    print(">>> 提取上游PDF条目...")
    pdf_items_by_doc = {}
    if user_pdf_dir and os.path.isdir(user_pdf_dir):
        # 多份文档先并行预解析, 下面的循环直接命中缓存
        prefetch_pdfs({
            f.replace('.pdf', ''): os.path.join(user_pdf_dir, f)
            for f in sorted(os.listdir(user_pdf_dir)) if f.lower().endswith('.pdf')
        })
        for fname in sorted(os.listdir(user_pdf_dir)):
            if not fname.lower().endswith('.pdf'):
                continue
            doc_name = fname.replace('.pdf', '')
            pdf_path = os.path.join(user_pdf_dir, fname)
            items = extract_pdf_items(pdf_path, doc_name)
            pdf_items_by_doc[doc_name] = items
            print(f"    [{doc_name}]: {len(items)} 个条目")

    # 3. 构建正向矩阵数据 (匹配 + union coloring)
    print(">>> 构建正向矩阵数据 (匹配 + union coloring)...")
    forward_data = _build_forward_data(reverse_rows, pdf_items_by_doc)

    # 统计
    traced = sum(1 for d in forward_data.values() if d['has_trace'])
    untraced = len(forward_data) - traced
    total_sys = sum(len(d['ds_items']) for d in forward_data.values() if d['has_trace'])
    print(f"    总条目: {len(forward_data)}, 有追踪: {traced}, 未追踪: {untraced}")
    print(f"    系统需求追踪关系: {total_sys}")

    # 4. 按文档拆分
    by_doc = _split_by_user_doc(forward_data)

    # 过滤: 仅保留用户需求目录中存在的PDF文档
    user_doc_names = set(pdf_items_by_doc.keys())
    if user_doc_names:
        by_doc = {k: v for k, v in by_doc.items() if k in user_doc_names}

    for doc_name, entries in by_doc.items():
        n = len(entries)
        t = sum(1 for d in entries.values() if d['has_trace'])
        print(f"    [{doc_name}]: {n} 个条目 ({t} 有追踪, {n-t} 未追踪)")

    # 5. 生成Excel (D列下游文档名: 优先系统需求目录文档名, 缺失回退逆向矩阵记录)
    print(f">>> 生成正向追踪矩阵Excel...")
    generate_forward_excel(
        by_doc, output_path,
        lambda it: sys_req_doc_name or it.get('ds_doc', ''),
    )
    print(f"    已保存至 {output_path}")

    return {
        'total_entries': len(forward_data),
        'traced': traced,
        'untraced': untraced,
        'total_sys_reqs': total_sys,
        'doc_count': len(by_doc),
    }


if __name__ == "__main__":
    from config import PROJECT_ROOT, OUTPUT_DIR, make_output_path

    reverse_matrix_path = os.path.join(OUTPUT_DIR, "追踪验证结果_v5.xlsx")
    user_pdf_dir = os.path.join(PROJECT_ROOT, "用户需求")
    sys_req_dir = os.path.join(PROJECT_ROOT, "系统需求")
    output_path = make_output_path("正向追踪矩阵.xlsx")

    result = generate_forward_matrix(
        reverse_matrix_path, user_pdf_dir, output_path, sys_req_dir,
    )
    print(f"\n=== 正向追踪矩阵生成完成 ===")
    print(f"  文档数: {result['doc_count']}")
    print(f"  总条目: {result['total_entries']}")
    print(f"  有追踪: {result['traced']}, 未追踪: {result['untraced']}")
    print(f"  系统需求追踪: {result['total_sys_reqs']}")
