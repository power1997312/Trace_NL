"""
核仪控工程文档追踪验证系统 - Streamlit Web界面
"""
import os
import sys
import time
import warnings

warnings.filterwarnings('ignore')

# 确保项目根目录在路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st
from config import PROJECT_ROOT, DOCUMENT_LEVELS, OUTPUT_DIR, MatchCategory

# ============================================================
# 页面配置
# ============================================================
st.set_page_config(
    page_title="文档追踪验证系统",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 自定义CSS
st.markdown("""
<style>
.color-green { color: #00B050; font-weight: bold; }
.color-blue { color: #0070C0; font-weight: bold; }
.color-black { color: #333333; }
.stat-card {
    padding: 1rem;
    border-radius: 0.5rem;
    border: 1px solid #ddd;
    text-align: center;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# 侧边栏: 文档配置
# ============================================================
def render_sidebar():
    """渲染侧边栏配置面板"""
    st.sidebar.title("📋 文档配置")

    # 项目根目录
    root_dir = st.sidebar.text_input(
        "项目根目录",
        value=PROJECT_ROOT,
        help="包含所有文档目录的根路径",
    )

    # 追踪流水线选择 (两条业务链路)
    pipeline = st.sidebar.radio(
        "追踪流水线",
        options=["A", "B"],
        index=0,
        horizontal=True,
        format_func=lambda p: "A: 需求→用户需求" if p == "A" else "B: 设计→系统需求",
        help=(
            "A: 系统需求 → 用户需求 (下游=系统需求, 上游=用户需求)\n"
            "B: 系统设计 → 系统需求 (下游=系统设计[多文档], 上游=系统需求)"
        ),
    )

    if pipeline == "B":
        # 流水线B: 下游固定为系统设计(多文档), 上游固定为系统需求(单文档)
        downstream_dir = "系统设计"
        upstream_dirs = ["系统需求"]
        st.sidebar.caption("流水线B: 下游=系统设计(目录内全部PDF), 上游=系统需求")
    else:
        # 流水线A: 目录自由选择
        # 扫描可用目录
        available_dirs = _scan_document_dirs(root_dir)

        # 下游文档目录选择
        downstream_options = [d for d in available_dirs if d != "用户需求"]
        if not downstream_options:
            downstream_options = available_dirs

        downstream_dir = st.sidebar.selectbox(
            "下游文档目录",
            options=downstream_options,
            index=0 if downstream_options else None,
            help="包含下游文档(如系统需求)的目录",
        )

        # 上游文档目录选择(可多选)
        upstream_options = [d for d in available_dirs if d != downstream_dir]
        upstream_dirs = st.sidebar.multiselect(
            "上游文档目录",
            options=upstream_options,
            default=[u for u in upstream_options if u == "用户需求"],
            help="包含上游文档(如用户需求)的目录，可多选",
        )

    st.sidebar.divider()

    # 运行模式选择 (三模式接入)
    trace_mode = st.sidebar.radio(
        "追踪关系来源模式",
        options=["table", "discover", "hybrid"],
        index=0,
        horizontal=True,
        help=(
            "table: 使用下游文档附录追踪关系表(现状默认)\n"
            "discover: 忽略追踪表, 纯内容候选发现\n"
            "hybrid: 以表为准, 发现结果补漏(added)/纠偏(suspicious)"
        ),
    )

    st.sidebar.divider()

    # 扫描文档按钮
    if st.sidebar.button("🔍 扫描文档"):
        scan_result = _scan_pdfs(root_dir, downstream_dir, upstream_dirs)
        st.session_state['scan_result'] = scan_result

    # 显示扫描结果
    if 'scan_result' in st.session_state:
        sr = st.session_state['scan_result']
        st.sidebar.markdown("**扫描结果:**")
        st.sidebar.markdown(f"- 下游PDF: **{sr['downstream_count']}** 份")
        st.sidebar.markdown(f"- 上游PDF: **{sr['upstream_count']}** 份")
        for pdf_info in sr.get('downstream_pdfs', []):
            st.sidebar.caption(f"  ↓ {pdf_info['name']} ({pdf_info['pages']}页, {pdf_info['type']})")
        for pdf_info in sr.get('upstream_pdfs', []):
            st.sidebar.caption(f"  ↑ {pdf_info['name']} ({pdf_info['pages']}页, {pdf_info['type']})")

    return root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline


# ============================================================
# 主区域: 操作面板
# ============================================================
def render_main_area(root_dir, downstream_dir, upstream_dirs, trace_mode='table', pipeline='A'):
    """渲染主区域操作面板"""
    st.title("核仪控工程文档追踪验证系统")
    st.markdown("仪控工程领域文档追踪关系验证工具，支持逆向/正向追踪矩阵生成和微块级文本匹配验证。")

    st.divider()

    # 操作按钮组
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        btn_backward = st.button("📊 生成逆向追踪矩阵", use_container_width=True)
    with col2:
        btn_forward = st.button("🔄 生成正向追踪矩阵", use_container_width=True)
    with col3:
        btn_verify = st.button("🔬 追踪验证(全文匹配)", use_container_width=True)
    with col4:
        btn_full = st.button("⚡ 一键全流程", use_container_width=True, type="primary")

    st.divider()

    # 执行操作
    if btn_backward:
        _run_backward_only(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)
    elif btn_forward:
        _run_forward_only(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)
    elif btn_verify:
        _run_verification(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)
    elif btn_full:
        _run_full_pipeline(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)

    # 显示历史结果
    if 'result' in st.session_state:
        _render_results()


# ============================================================
# 流水线执行
# ============================================================
def _build_matrix(root_dir, downstream_dir, upstream_dirs, trace_mode='table', pipeline='A'):
    """构建追踪矩阵 (A: 系统需求→用户需求; B: 系统设计→系统需求)"""
    if pipeline == 'B':
        from core.traceability_matrix import build_backward_matrix_from_design

        design_dir = os.path.join(root_dir, "系统设计")
        sys_req_dir = os.path.join(root_dir, "系统需求")
        if not os.path.isdir(design_dir):
            raise ValueError(f"系统设计目录不存在: {design_dir}")
        design_pdfs = {f.replace('.pdf', ''): os.path.join(design_dir, f)
                       for f in sorted(os.listdir(design_dir)) if f.lower().endswith('.pdf')}
        if not design_pdfs:
            raise ValueError(f"在 {design_dir} 目录中未找到PDF文件")
        sys_req_pdf = _find_downstream_pdf(root_dir, "系统需求")
        if not sys_req_pdf:
            raise ValueError("在 系统需求 目录中未找到PDF文件")

        return build_backward_matrix_from_design(design_pdfs, sys_req_pdf, mode=trace_mode)

    from core.traceability_matrix import build_backward_matrix

    downstream_pdf = _find_downstream_pdf(root_dir, downstream_dir)
    if not downstream_pdf:
        raise ValueError(f"在 {downstream_dir} 目录中未找到PDF文件")

    upstream_pdfs = _find_upstream_pdfs(root_dir, upstream_dirs)
    if not upstream_pdfs:
        raise ValueError(f"在 {upstream_dirs} 目录中未找到PDF文件")

    return build_backward_matrix(downstream_pdf, upstream_pdfs, mode=trace_mode)


def _generate_forward_matrix(root_dir, downstream_dir, upstream_dirs,
                             reverse_path, forward_path):
    """按流水线生成正向矩阵 (A: 用户需求→系统需求; B: 系统需求→系统设计)"""
    if downstream_dir == "系统设计" and "系统需求" in (upstream_dirs or []):
        from generate_forward_sd_matrix import generate_forward_sd_matrix
        sys_req_dir = os.path.join(root_dir, "系统需求")
        design_dir = os.path.join(root_dir, "系统设计")
        if not os.path.isdir(sys_req_dir):
            raise ValueError(f"系统需求目录不存在: {sys_req_dir}")
        return generate_forward_sd_matrix(reverse_path, sys_req_dir, forward_path, design_dir)

    from generate_forward_matrix import generate_forward_matrix
    upstream_pdf_dir = os.path.join(root_dir, upstream_dirs[0]) if upstream_dirs else ''
    if not (upstream_pdf_dir and os.path.isdir(upstream_pdf_dir)):
        raise ValueError(f"上游文档目录不存在({upstream_pdf_dir}), 无法生成正向矩阵")
    sys_req_dir = os.path.join(root_dir, downstream_dir)
    return generate_forward_matrix(reverse_path, upstream_pdf_dir, forward_path, sys_req_dir)


def _hybrid_stats_write(matrix, trace_mode):
    """混合模式: 在前端显示发现层校验结论统计(否则与表格模式无异, 无法分辨是否生效)"""
    if trace_mode != 'hybrid':
        return
    n_added = sum(1 for r in matrix.rows if (r.evidence or '').startswith('[added]'))
    n_susp = sum(1 for r in matrix.rows if '[suspicious]' in (r.evidence or ''))
    n_ok = sum(1 for r in matrix.rows if '发现层一致' in (r.evidence or ''))
    n_sec = sum(1 for r in matrix.rows if '发现层次要支持' in (r.evidence or ''))
    n_unsup = sum(1 for r in matrix.rows if '发现层未支持' in (r.evidence or ''))
    n_untr = sum(1 for r in matrix.rows if '发现层未溯源' in (r.evidence or ''))
    st.write(f"🔎 混合模式校验结论: 发现层一致 {n_ok} 行 | 次要支持 {n_sec} 行 | "
             f"未支持 {n_unsup} 行 | 未溯源 {n_untr} 行 | 补漏 {n_added} 行 | "
             f"纠偏标记 {n_susp} 行")


def _run_backward_only(root_dir, downstream_dir, upstream_dirs, trace_mode='table', pipeline='A'):
    """仅生成逆向追踪矩阵(无匹配着色)"""
    from core.excel_generator import generate_excel

    with st.status("正在生成逆向追踪矩阵...", expanded=True) as status:
        st.write("📄 解析PDF文档并提取追踪关系...")
        matrix = _build_matrix(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)
        st.write(f"✅ 提取到 {len(matrix.rows)} 条追踪关系")
        _hybrid_stats_write(matrix, trace_mode)

        output_path = os.path.join(OUTPUT_DIR, "逆向追踪矩阵.xlsx")
        st.write("📝 生成Excel文件...")
        generate_excel(matrix, output_path)
        st.write(f"✅ 已保存至 {output_path}")

        status.update(label="逆向追踪矩阵生成完成", state="complete")

    st.session_state['result'] = {
        'type': 'backward',
        'matrix': matrix,
        'output_path': output_path,
    }


def _run_forward_only(root_dir, downstream_dir, upstream_dirs, trace_mode='table', pipeline='A'):
    """生成正向追踪矩阵(结构无着色; 正向视图由逆向矩阵Excel转换而来)"""
    from core.excel_generator import generate_excel

    with st.status("正在生成正向追踪矩阵...", expanded=True) as status:
        st.write("📄 解析PDF文档并构建逆向矩阵...")
        matrix = _build_matrix(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)
        st.write(f"✅ 提取到 {len(matrix.rows)} 条追踪关系")
        _hybrid_stats_write(matrix, trace_mode)

        # 正向矩阵数据源: 逆向矩阵Excel(追踪关系 + 着色runs)
        reverse_path = os.path.join(OUTPUT_DIR, "逆向追踪矩阵.xlsx")
        generate_excel(matrix, reverse_path)
        st.write(f"✅ 逆向矩阵已保存至 {reverse_path}")

        output_path = os.path.join(OUTPUT_DIR, "正向追踪矩阵.xlsx")
        try:
            _generate_forward_matrix(root_dir, downstream_dir, upstream_dirs,
                                     reverse_path, output_path)
            st.write(f"✅ 已保存至 {output_path}")
            status.update(label="正向追踪矩阵生成完成", state="complete")
        except Exception as e:
            st.write(f"⚠️ 正向矩阵生成失败: {e}")
            status.update(label="正向追踪矩阵生成失败", state="error")
            output_path = reverse_path  # 至少可下载逆向矩阵

    st.session_state['result'] = {
        'type': 'forward',
        'matrix': matrix,
        'output_path': output_path,
    }


def _run_verification(root_dir, downstream_dir, upstream_dirs, trace_mode='table', pipeline='A'):
    """执行完整追踪验证(含文本匹配)"""
    from core.text_matcher import verify_matrix
    from core.excel_generator import generate_excel

    with st.status("正在执行追踪验证...", expanded=True) as status:
        st.write("📄 构建追踪矩阵...")
        matrix = _build_matrix(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)
        st.write(f"✅ {len(matrix.rows)} 条追踪关系")
        _hybrid_stats_write(matrix, trace_mode)

        st.write("🔬 执行微块级文本匹配验证(加载ML模型中)...")
        t0 = time.time()
        progress_bar = st.progress(0.0, text="微块级匹配进行中...")

        def _on_match_progress(done: int, total: int):
            progress_bar.progress(min(done / max(total, 1), 1.0),
                                  text=f"微块级匹配进行中... {done}/{total}")

        verify_matrix(matrix, progress_cb=_on_match_progress)
        progress_bar.empty()
        elapsed = time.time() - t0
        st.write(f"✅ 文本匹配完成 ({elapsed:.1f}s)")

        # LLM裁决(存疑/未溯源/hybrid干预行; 未配置TRACE_NL_LLM_API_KEY时自动跳过)
        from config import LLM_ENABLED
        if LLM_ENABLED:
            st.write("🤖 AI 裁决待人工审核行(存疑/未溯源)...")
            try:
                from core.llm_adjudicator import adjudicate_matrix
                llm_stats = adjudicate_matrix(matrix)
                st.write(f"✅ AI 裁决完成: 待裁决 {llm_stats['n_tasks']} 组, "
                         f"高置信确认 {llm_stats['n_confirmed']}, "
                         f"给出意见 {llm_stats['n_resolved']}, "
                         f"失败 {llm_stats['n_failed']}")
                # 失败明细透传(否则前端只看到"失败N", 看不到原因)
                for fr in llm_stats.get('fail_reasons', [])[:5]:
                    st.write(f"　❌ {fr}")
            except Exception as e:
                st.write(f"⚠️ AI 裁决异常(已跳过): {e}")
        else:
            st.write("ℹ️ 未配置 TRACE_NL_LLM_API_KEY — 已跳过 AI 裁决。"
                     "如需启用请在启动本应用前设置环境变量(见侧边栏\"AI 配置\"提示)并重启。")

        output_path = os.path.join(OUTPUT_DIR, "追踪验证结果.xlsx")
        st.write("📝 生成逆向追踪矩阵Excel...")
        generate_excel(matrix, output_path)
        st.write(f"✅ 已保存至 {output_path}")

        # 生成正向追踪矩阵 (独立Excel)
        forward_path = os.path.join(OUTPUT_DIR, "正向追踪矩阵.xlsx")
        st.write("🔄 生成正向追踪矩阵...")
        try:
            _generate_forward_matrix(root_dir, downstream_dir, upstream_dirs,
                                     output_path, forward_path)
            st.write(f"✅ 正向矩阵已保存至 {forward_path}")
        except Exception as e:
            st.write(f"⚠️ 正向矩阵生成失败: {e}")

        status.update(label="追踪验证完成", state="complete")

    st.session_state['result'] = {
        'type': 'verification',
        'matrix': matrix,
        'output_path': output_path,
    }


def _run_full_pipeline(root_dir, downstream_dir, upstream_dirs, trace_mode='table', pipeline='A'):
    """一键全流程"""
    _run_verification(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)


# ============================================================
# 结果展示
# ============================================================
def _char_stats(runs) -> tuple[int, int, int]:
    """统计TextRun字符级着色: (一致, 相近, 未匹配)"""
    g = b = k = 0
    for run in runs:
        n = len(run.text)
        if run.category == MatchCategory.GREEN:
            g += n
        elif run.category == MatchCategory.BLUE:
            b += n
        else:
            k += n
    return g, b, k


def _render_results():
    """渲染结果展示区域(两Tab: 内容一致统计 / 追踪矩阵预览)"""
    result = st.session_state['result']
    matrix = result['matrix']
    output_path = result['output_path']

    st.subheader("📊 结果展示")

    # 下载按钮(结果文件缺失时提前退出, 避免 open 崩溃)
    if not os.path.exists(output_path):
        st.warning(f"结果文件不存在或生成失败: {output_path}")
        return
    with open(output_path, 'rb') as f:
        file_data = f.read()

    st.download_button(
        label="📥 下载Excel文件",
        data=file_data,
        file_name=os.path.basename(output_path),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    tab_stat, tab_preview = st.tabs(
        ["📈 内容一致统计", "📋 追踪矩阵预览"])

    with tab_stat:
        _render_match_statistics(matrix)
    with tab_preview:
        _render_preview_table(matrix, result['type'])


def _render_match_statistics(matrix):
    """渲染内容一致统计

    统计粒度: 针对每份文档的每条下游需求条目, 统计其内容中
    一致(绿=逐字承接) / 相近(蓝=语义匹配) / 未匹配(黑) 的具体字符量。
    基于下游内容字符级着色, 与Excel着色口径完全一致。
    """
    st.markdown("#### 内容一致统计")
    st.caption("统计对象: 每条下游需求条目(按所属文档分组); "
               "口径: 下游内容字符级着色 — 绿=完全一致, 蓝=语义相近, 黑=未匹配。"
               "所有结果最终均需人工确认。")

    # ---- 提取每条需求(每个下游条目)的字符级统计 ----
    item_rows = []
    seen = set()
    for row in matrix.rows:
        if row.downstream_id in seen:
            continue
        seen.add(row.downstream_id)
        mr = row.match_result
        if not mr or not mr.downstream_runs:
            continue
        g, b, k = _char_stats(mr.downstream_runs)
        total = g + b + k
        if total == 0:
            continue
        item_rows.append({
            '文档': row.downstream_doc or '—',
            '下游条目号': row.downstream_id,
            '总字符': total,
            '一致': g, '一致占比': g / total,
            '相近': b, '相近占比': b / total,
            '未匹配': k, '未匹配占比': k / total,
        })

    if not item_rows:
        st.info("文本匹配统计仅在运行\"追踪验证(全文匹配)\"后可用。")
        return

    # ---- 全矩阵总览 ----
    tg = sum(r['一致'] for r in item_rows)
    tb = sum(r['相近'] for r in item_rows)
    tk = sum(r['未匹配'] for r in item_rows)
    tc = tg + tb + tk
    st.markdown("##### 全矩阵总览")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("需求条目数", len(item_rows))
    with c2:
        st.metric("一致字符", f"{tg} ({tg/tc:.0%})")
    with c3:
        st.metric("相近字符", f"{tb} ({tb/tc:.0%})")
    with c4:
        st.metric("未匹配字符", f"{tk} ({tk/tc:.0%})")
    st.markdown(f"""
    <div style="display:flex; height:18px; border-radius:6px; overflow:hidden; font-size:0.75em; color:white; text-align:center;">
        <div style="background:#00B050; width:{tg/tc*100:.1f}%;">一致 {tg/tc:.0%}</div>
        <div style="background:#0070C0; width:{tb/tc*100:.1f}%;">相近 {tb/tc:.0%}</div>
        <div style="background:#555; width:{tk/tc*100:.1f}%;">未匹配 {tk/tc:.0%}</div>
    </div>
    """, unsafe_allow_html=True)

    # ---- 按文档汇总 ----
    import pandas as pd
    from collections import OrderedDict
    doc_groups = OrderedDict()
    for r in item_rows:
        doc_groups.setdefault(r['文档'], []).append(r)

    doc_rows = []
    for doc, rs in doc_groups.items():
        dg = sum(x['一致'] for x in rs)
        db = sum(x['相近'] for x in rs)
        dk = sum(x['未匹配'] for x in rs)
        dt = dg + db + dk
        doc_rows.append({
            '文档': doc,
            '需求条目数': len(rs),
            '总字符': dt,
            '一致占比': dg / dt,
            '相近占比': db / dt,
            '未匹配占比': dk / dt,
        })
    st.markdown("##### 按文档汇总")
    st.dataframe(pd.DataFrame(doc_rows), hide_index=True, use_container_width=True,
                 column_config={
                     '一致占比': st.column_config.ProgressColumn(
                         '一致', min_value=0.0, max_value=1.0, format='%.0f%%'),
                     '相近占比': st.column_config.ProgressColumn(
                         '相近', min_value=0.0, max_value=1.0, format='%.0f%%'),
                     '未匹配占比': st.column_config.ProgressColumn(
                         '未匹配', min_value=0.0, max_value=1.0, format='%.0f%%'),
                 })

    # ---- 每条需求明细 ----
    st.markdown("##### 每条需求明细")
    st.dataframe(pd.DataFrame(item_rows), hide_index=True, use_container_width=True,
                 column_config={
                     '一致': st.column_config.NumberColumn('一致字符'),
                     '一致占比': st.column_config.ProgressColumn(
                         '一致', min_value=0.0, max_value=1.0, format='%.0f%%'),
                     '相近': st.column_config.NumberColumn('相近字符'),
                     '相近占比': st.column_config.ProgressColumn(
                         '相近', min_value=0.0, max_value=1.0, format='%.0f%%'),
                     '未匹配': st.column_config.NumberColumn('未匹配字符'),
                     '未匹配占比': st.column_config.ProgressColumn(
                         '未匹配', min_value=0.0, max_value=1.0, format='%.0f%%'),
                 })


def _render_preview_table(matrix, result_type: str):
    """渲染预览表格(筛选器 + 补充元数据列)"""
    st.markdown("#### 追踪矩阵预览")

    rows = matrix.rows
    if not rows:
        st.info("矩阵为空。")
        return

    # ---- 筛选器 ----
    c1, c2, c3 = st.columns(3)
    with c1:
        f_source = st.selectbox("关系来源", ['全部', 'table', 'discover', 'hybrid'],
                                key='filter_source')
    with c2:
        f_search = st.text_input("搜索条目号", key='filter_search',
                                 placeholder="如 DCS-SyRS005")
    with c3:
        f_cat = st.selectbox("匹配行类别", ['全部', '🟢 GREEN', '🔵 BLUE', '⚫ BLACK'],
                             key='filter_cat',
                             disabled=(result_type != 'verification'))

    filtered = []
    for r in rows:
        if f_source != '全部' and r.relation_source != f_source:
            continue
        if f_search and f_search.strip() \
                and f_search.strip().upper() not in r.downstream_id.upper():
            continue
        if result_type == 'verification' and f_cat != '全部' and r.match_result:
            cat_label = {MatchCategory.GREEN: '🟢 GREEN',
                         MatchCategory.BLUE: '🔵 BLUE',
                         MatchCategory.BLACK: '⚫ BLACK'}.get(r.match_result.overall_category)
            if cat_label != f_cat:
                continue
        filtered.append(r)

    st.caption(f"显示 {len(filtered)} / {len(rows)} 行")
    if not filtered:
        st.info("当前筛选条件下无匹配行。")
        return

    # 带颜色的HTML表格(verification)或简单表格
    if result_type == 'verification':
        html = _build_colored_html_table(filtered)
        st.markdown(html, unsafe_allow_html=True)
    else:
        import pandas as pd
        data = []
        for row in filtered:
            data.append({
                '序号': row.seq_number,
                '下游条目号': row.downstream_id,
                '下游内容': row.downstream_content[:80] + '...' if len(row.downstream_content) > 80 else row.downstream_content,
                '上游文档': row.upstream_doc,
                '上游条目号/章节': row.upstream_ref[:30],
                '上游内容': row.upstream_content[:80] + '...' if len(row.upstream_content) > 80 else row.upstream_content,
            })
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True)


def _build_colored_html_table(rows) -> str:
    """构建带颜色标记与元数据列的HTML预览表格"""
    rows_html = []

    for row in rows:
        ds_cell = _build_colored_cell_html(row.downstream_content, row.match_result, 'downstream')
        up_cell = _build_colored_cell_html(row.upstream_content, row.match_result, 'upstream')

        # 元数据列
        conf = f"{row.candidate_score:.2f}" if row.candidate_score else '—'
        ambiguous_html = ('<span style="color:#E81123; font-weight:bold;">待人工确认</span>'
                          if row.ambiguous else '—')
        if row.ai_opinion:
            ai = row.ai_opinion
            if ai.startswith('AI确认') or ai.startswith('AI认同'):
                ai_color = '#00B050'
            elif ai.startswith('AI建议'):
                ai_color = '#0070C0'
            else:
                ai_color = '#555'
            ai_html = (f'<span style="color:{ai_color}" title="{_html_escape(ai)}">'
                       f'{_html_escape(ai[:60])}{"…" if len(ai) > 60 else ""}</span>')
        else:
            ai_html = '—'

        rows_html.append(f"""
        <tr>
            <td style="text-align:center">{row.seq_number}</td>
            <td>{_html_escape(row.downstream_id)}</td>
            <td style="font-size:0.85em">{ds_cell}</td>
            <td>{_html_escape(row.upstream_doc)}</td>
            <td>{_html_escape(row.upstream_ref[:30])}</td>
            <td style="font-size:0.85em">{up_cell}</td>
            <td style="text-align:center">{_html_escape(row.relation_source)}</td>
            <td style="text-align:center">{conf}</td>
            <td style="text-align:center">{ambiguous_html}</td>
            <td style="font-size:0.8em; max-width:220px;">{ai_html}</td>
        </tr>
        """)

    html = f"""
    <div style="overflow-x:auto; max-height:600px; overflow-y:auto;">
    <table style="border-collapse:collapse; width:100%; font-size:0.8em;">
    <thead>
    <tr style="background:#4472C4; color:white; position:sticky; top:0;">
        <th style="padding:8px; border:1px solid #ddd; width:40px;">序号</th>
        <th style="padding:8px; border:1px solid #ddd; width:110px;">下游条目号</th>
        <th style="padding:8px; border:1px solid #ddd;">下游内容</th>
        <th style="padding:8px; border:1px solid #ddd; width:100px;">上游文档</th>
        <th style="padding:8px; border:1px solid #ddd; width:120px;">上游条目号/章节</th>
        <th style="padding:8px; border:1px solid #ddd;">上游内容</th>
        <th style="padding:8px; border:1px solid #ddd; width:60px;">关系来源</th>
        <th style="padding:8px; border:1px solid #ddd; width:60px;">置信度</th>
        <th style="padding:8px; border:1px solid #ddd; width:80px;">待人工确认</th>
        <th style="padding:8px; border:1px solid #ddd; width:220px;">AI 审核意见</th>
    </tr>
    </thead>
    <tbody>
    {''.join(rows_html)}
    </tbody>
    </table>
    </div>
    <p style="font-size:0.75em; color:#666; margin-top:0.5em;">
        <span style="color:#00B050; font-weight:bold;">■ 绿色=完全一致</span> &nbsp;
        <span style="color:#0070C0; font-weight:bold;">■ 蓝色=语义匹配</span> &nbsp;
        <span style="color:#333;">■ 黑色=未匹配</span> &nbsp;|&nbsp;
        <span style="color:#E81123; font-weight:bold;">待人工确认</span> = 算法存疑行
    </p>
    """
    return html


def _build_colored_cell_html(content: str, match_result, side: str) -> str:
    """构建带颜色标记的单元格HTML"""
    if not match_result:
        return _html_escape(content[:200])

    runs = match_result.downstream_runs if side == 'downstream' else match_result.upstream_runs
    if not runs:
        return _html_escape(content[:200])

    parts = []
    total_len = 0
    for run in runs:
        color_map = {
            'exact': '#00B050',
            'semantic': '#0070C0',
            'unmatched': '#333333',
        }
        color = color_map.get(run.category.value, '#333333')
        text = _html_escape(run.text)
        parts.append(f'<span style="color:{color}">{text}</span>')
        total_len += len(run.text)
        if total_len > 300:
            parts.append('<span style="color:#999">...</span>')
            break

    return ''.join(parts)


# ============================================================
# 辅助函数
# ============================================================
def _scan_document_dirs(root_dir: str) -> list[str]:
    """扫描项目根目录下的文档目录"""
    dirs = []
    if not os.path.isdir(root_dir):
        return dirs
    for name in sorted(os.listdir(root_dir)):
        full_path = os.path.join(root_dir, name)
        if os.path.isdir(full_path) and name in DOCUMENT_LEVELS:
            dirs.append(name)
    return dirs


def _scan_pdfs(root_dir: str, downstream_dir: str, upstream_dirs: list[str]) -> dict:
    """扫描PDF文件"""
    import fitz

    result = {
        'downstream_count': 0,
        'upstream_count': 0,
        'downstream_pdfs': [],
        'upstream_pdfs': [],
    }

    from core.requirement_extractor import detect_document_type

    for dir_name in [downstream_dir] + upstream_dirs:
        dir_path = os.path.join(root_dir, dir_name)
        if not os.path.isdir(dir_path):
            continue
        for fname in os.listdir(dir_path):
            if not fname.lower().endswith('.pdf'):
                continue
            pdf_path = os.path.join(dir_path, fname)
            try:
                doc = fitz.open(pdf_path)
                pages = doc.page_count
                text = ''
                for i in range(min(3, pages)):
                    text += doc[i].get_text()
                doc.close()
                doc_type = detect_document_type(text)
                type_label = "ID型" if doc_type == "id_based" else "章节型"
            except Exception:
                pages = 0
                type_label = "未知"

            info = {'name': fname, 'pages': pages, 'type': type_label}
            if dir_name == downstream_dir:
                result['downstream_pdfs'].append(info)
                result['downstream_count'] += 1
            else:
                result['upstream_pdfs'].append(info)
                result['upstream_count'] += 1

    return result


def _find_downstream_pdf(root_dir: str, downstream_dir: str) -> str | None:
    """查找下游PDF文件"""
    dir_path = os.path.join(root_dir, downstream_dir)
    if not os.path.isdir(dir_path):
        return None
    for fname in os.listdir(dir_path):
        if fname.lower().endswith('.pdf'):
            return os.path.join(dir_path, fname)
    return None


def _find_upstream_pdfs(root_dir: str, upstream_dirs: list[str]) -> dict[str, str]:
    """查找上游PDF文件, 返回 {文档名: PDF路径}"""
    result = {}
    for dir_name in upstream_dirs:
        dir_path = os.path.join(root_dir, dir_name)
        if not os.path.isdir(dir_path):
            continue
        for fname in os.listdir(dir_path):
            if fname.lower().endswith('.pdf'):
                doc_name = fname.replace('.pdf', '')
                result[doc_name] = os.path.join(dir_path, fname)
    return result


def _html_escape(text: str) -> str:
    """HTML转义"""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('\n', '<br>')
            )


# ============================================================
# 主入口
# ============================================================
def main():
    root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline = render_sidebar()
    render_main_area(root_dir, downstream_dir, upstream_dirs, trace_mode, pipeline)


if __name__ == '__main__':
    main()
