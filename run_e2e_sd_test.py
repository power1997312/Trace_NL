"""
端到端验证脚本 — 系统设计 → 系统需求 追踪矩阵

从多份系统设计PDF的附录追踪关系表构建逆向追踪矩阵（系统设计→系统需求），
执行微块级文本匹配验证，生成逆向和正向追踪矩阵Excel。
"""
from __future__ import annotations
import os
import sys
import time
import warnings
warnings.filterwarnings('ignore')

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import PROJECT_ROOT, OUTPUT_DIR, make_output_path, MatchCategory
from config import SYSTEM_DESIGN_DIR, SYSTEM_REQUIREMENT_DIR
from core.traceability_matrix import build_backward_matrix_from_design
from core.text_matcher import verify_matrix
from core.excel_generator import generate_excel


def main():
    # === 0. 运行模式 (table / discover / hybrid), 默认 discover ===
    mode = sys.argv[1] if len(sys.argv) > 1 else 'discover'
    if mode not in ('table', 'discover', 'hybrid'):
        print(f"[ERROR] 未知模式: {mode} (应为 table/discover/hybrid)"); return

    # === 1. 配置路径 ===
    design_dir = SYSTEM_DESIGN_DIR
    sys_req_dir = SYSTEM_REQUIREMENT_DIR

    # 检查目录是否存在
    if not os.path.isdir(design_dir):
        print(f"[ERROR] 系统设计文档目录不存在: {design_dir}")
        print("请创建目录并放入系统设计PDF文档。")
        return
    if not os.path.isdir(sys_req_dir):
        print(f"[ERROR] 系统需求文档目录不存在: {sys_req_dir}")
        return

    # 收集所有系统设计PDF
    design_pdfs = {}
    for fname in sorted(os.listdir(design_dir)):
        if fname.lower().endswith('.pdf'):
            doc_name = fname.replace('.pdf', '')
            design_pdfs[doc_name] = os.path.join(design_dir, fname)

    if not design_pdfs:
        print(f"[ERROR] 在 {design_dir} 中未找到PDF文件")
        return

    # 获取系统需求PDF（仅一份）
    sys_req_pdf = None
    for fname in os.listdir(sys_req_dir):
        if fname.lower().endswith('.pdf'):
            sys_req_pdf = os.path.join(sys_req_dir, fname)
            break
    if not sys_req_pdf:
        print(f"[ERROR] 在 {sys_req_dir} 中未找到PDF")
        return

    print(f"系统设计PDFs: {list(design_pdfs.keys())}")
    print(f"系统需求PDF: {os.path.basename(sys_req_pdf)}")
    print()

    # === 2. 构建逆向追踪矩阵 ===
    print(f">>> 构建逆向追踪矩阵（系统设计→系统需求, 模式: {mode}）...")
    t0 = time.time()
    matrix = build_backward_matrix_from_design(design_pdfs, sys_req_pdf, mode=mode)
    print(f"    完成: {len(matrix.rows)} 条关系 ({time.time()-t0:.1f}s)")

    # 检查上游内容填充情况
    empty_up = sum(1 for r in matrix.rows if not r.upstream_content.strip())
    print(f"    上游（系统需求）内容为空: {empty_up}/{len(matrix.rows)}")
    empty_ds = sum(1 for r in matrix.rows if not r.downstream_content.strip())
    print(f"    下游（系统设计）内容为空: {empty_ds}/{len(matrix.rows)}")

    # 打印每行摘要
    print("\n    行号 | 下游ID(设计)         | 上游文档            | 上游引用(需求)           | 上游内容长度 | 下游内容长度")
    print("    " + "-" * 110)
    for r in matrix.rows:
        ds_short = r.downstream_id[:22]
        doc_short = r.upstream_doc[:18]
        ref_short = r.upstream_ref[:24]
        print(f"    {r.seq_number:>4} | {ds_short:<22} | {doc_short:<18} | {ref_short:<24} | {len(r.upstream_content):>6} | {len(r.downstream_content):>6}")

    # === 3. 执行微块匹配验证 ===
    print("\n>>> 执行微块级文本匹配验证...")
    t1 = time.time()
    verify_matrix(matrix)
    print(f"    完成 ({time.time()-t1:.1f}s)")

    # === 3b. LLM 裁决(存疑/未溯源/hybrid干预行; 未配置API key时自动跳过) ===
    from core.llm_adjudicator import adjudicate_matrix
    adjudicate_matrix(matrix)

    # 统计匹配结果: 整体类别行数 + 字符级着色占比(与Excel观感一致)
    from collections import Counter
    cat_counter = Counter()
    g_chars = b_chars = k_chars = 0
    for r in matrix.rows:
        mr = r.match_result
        if not mr:
            continue
        cat_counter[mr.overall_category] += 1
        for run in mr.downstream_runs:
            n = len(run.text)
            if run.category == MatchCategory.GREEN:
                g_chars += n
            elif run.category == MatchCategory.BLUE:
                b_chars += n
            else:
                k_chars += n
    total = len(matrix.rows)
    cg = cat_counter.get(MatchCategory.GREEN, 0)
    cb = cat_counter.get(MatchCategory.BLUE, 0)
    ck = cat_counter.get(MatchCategory.BLACK, 0)
    csum = g_chars + b_chars + k_chars
    print(f"\n    匹配统计(整体类别=行内最高微块类别, 非整行一致):")
    print(f"    GREEN行(至少一段完全一致): {cg}/{total} ({cg/total*100:.0f}%)")
    print(f"    BLUE行:                     {cb}/{total} ({cb/total*100:.0f}%)")
    print(f"    BLACK行:                    {ck}/{total} ({ck/total*100:.0f}%)")
    if csum:
        print(f"    下游内容字符级着色: 绿 {g_chars/csum:.0%} | "
              f"蓝 {b_chars/csum:.0%} | 黑 {k_chars/csum:.0%} (共{csum}字符)")

    # === 4. 生成逆向追踪矩阵Excel ===
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = make_output_path("追踪验证结果_系统设计.xlsx")
    print(f"\n>>> 生成逆向追踪矩阵Excel: {output_path}")
    generate_excel(matrix, output_path)
    print(f"    完成!")

    # === 5. 生成正向追踪矩阵Excel（系统需求→系统设计） ===
    forward_output = make_output_path("正向追踪矩阵_系统设计.xlsx")
    print(f"\n>>> 生成正向追踪矩阵Excel（系统需求→系统设计）: {forward_output}")
    try:
        from generate_forward_sd_matrix import generate_forward_sd_matrix
        fwd_result = generate_forward_sd_matrix(
            output_path, sys_req_dir, forward_output, design_dir,
        )
        print(f"    文档数: {fwd_result['doc_count']}, "
              f"总条目: {fwd_result['total_entries']}, "
              f"有追踪: {fwd_result['traced']}, 未追踪: {fwd_result['untraced']}, "
              f"系统设计追踪: {fwd_result['total_design_items']}")
    except Exception as e:
        print(f"    [ERROR] 正向矩阵生成失败: {e}")
        import traceback; traceback.print_exc()

    print("\n=== 系统设计追踪矩阵生成完成 ===")


if __name__ == '__main__':
    main()
