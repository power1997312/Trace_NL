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

    return root_dir, downstream_dir, upstream_dirs


# ============================================================
# 主区域: 操作面板
# ============================================================
def render_main_area(root_dir, downstream_dir, upstream_dirs):
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
        _run_backward_only(root_dir, downstream_dir, upstream_dirs)
    elif btn_forward:
        _run_forward_only(root_dir, downstream_dir, upstream_dirs)
    elif btn_verify:
        _run_verification(root_dir, downstream_dir, upstream_dirs)
    elif btn_full:
        _run_full_pipeline(root_dir, downstream_dir, upstream_dirs)

    # 显示历史结果
    if 'result' in st.session_state:
        _render_results()


# ============================================================
# 流水线执行
# ============================================================
def _build_matrix(root_dir, downstream_dir, upstream_dirs):
    """构建追踪矩阵"""
    from core.traceability_matrix import build_backward_matrix

    downstream_pdf = _find_downstream_pdf(root_dir, downstream_dir)
    if not downstream_pdf:
        raise ValueError(f"在 {downstream_dir} 目录中未找到PDF文件")

    upstream_pdfs = _find_upstream_pdfs(root_dir, upstream_dirs)
    if not upstream_pdfs:
        raise ValueError(f"在 {upstream_dirs} 目录中未找到PDF文件")

    return build_backward_matrix(downstream_pdf, upstream_pdfs)


def _run_backward_only(root_dir, downstream_dir, upstream_dirs):
    """仅生成逆向追踪矩阵(无匹配着色)"""
    from core.excel_generator import generate_excel

    with st.status("正在生成逆向追踪矩阵...", expanded=True) as status:
        st.write("📄 解析PDF文档并提取追踪关系...")
        matrix = _build_matrix(root_dir, downstream_dir, upstream_dirs)
        st.write(f"✅ 提取到 {len(matrix.rows)} 条追踪关系")

        output_path = os.path.join(OUTPUT_DIR, "逆向追踪矩阵.xlsx")
        st.write("📝 生成Excel文件...")
        generate_excel(matrix, output_path, include_forward=False)
        st.write(f"✅ 已保存至 {output_path}")

        status.update(label="逆向追踪矩阵生成完成", state="complete")

    st.session_state['result'] = {
        'type': 'backward',
        'matrix': matrix,
        'output_path': output_path,
    }


def _run_forward_only(root_dir, downstream_dir, upstream_dirs):
    """生成正向追踪矩阵(无匹配着色)"""
    from core.excel_generator import generate_excel

    with st.status("正在生成正向追踪矩阵...", expanded=True) as status:
        st.write("📄 解析PDF文档并构建逆向矩阵...")
        matrix = _build_matrix(root_dir, downstream_dir, upstream_dirs)
        st.write(f"✅ 提取到 {len(matrix.rows)} 条追踪关系")

        output_path = os.path.join(OUTPUT_DIR, "正向追踪矩阵.xlsx")
        st.write("🔄 转换为正向矩阵并生成Excel...")
        generate_excel(matrix, output_path, include_forward=True)
        st.write(f"✅ 已保存至 {output_path}")

        status.update(label="正向追踪矩阵生成完成", state="complete")

    st.session_state['result'] = {
        'type': 'forward',
        'matrix': matrix,
        'output_path': output_path,
    }


def _run_verification(root_dir, downstream_dir, upstream_dirs):
    """执行完整追踪验证(含文本匹配)"""
    from core.text_matcher import verify_matrix
    from core.excel_generator import generate_excel

    with st.status("正在执行追踪验证...", expanded=True) as status:
        st.write("📄 构建追踪矩阵...")
        matrix = _build_matrix(root_dir, downstream_dir, upstream_dirs)
        st.write(f"✅ {len(matrix.rows)} 条追踪关系")

        st.write("🔬 执行微块级文本匹配验证(加载ML模型中)...")
        t0 = time.time()
        verify_matrix(matrix)
        elapsed = time.time() - t0
        st.write(f"✅ 文本匹配完成 ({elapsed:.1f}s)")

        output_path = os.path.join(OUTPUT_DIR, "追踪验证结果.xlsx")
        st.write("📝 生成逆向追踪矩阵Excel...")
        generate_excel(matrix, output_path, include_forward=False)
        st.write(f"✅ 已保存至 {output_path}")

        # 生成正向追踪矩阵 (独立Excel)
        forward_path = os.path.join(OUTPUT_DIR, "正向追踪矩阵.xlsx")
        st.write("🔄 生成正向追踪矩阵...")
        try:
            from generate_forward_matrix import generate_forward_matrix
            upstream_pdf_dir = os.path.join(root_dir, upstream_dirs[0]) if upstream_dirs else ''
            sys_req_dir = os.path.join(root_dir, downstream_dir)
            if upstream_pdf_dir and os.path.isdir(upstream_pdf_dir):
                generate_forward_matrix(output_path, upstream_pdf_dir, forward_path, sys_req_dir)
                st.write(f"✅ 正向矩阵已保存至 {forward_path}")
        except Exception as e:
            st.write(f"⚠️ 正向矩阵生成失败: {e}")

        status.update(label="追踪验证完成", state="complete")

    st.session_state['result'] = {
        'type': 'verification',
        'matrix': matrix,
        'output_path': output_path,
    }


def _run_full_pipeline(root_dir, downstream_dir, upstream_dirs):
    """一键全流程"""
    _run_verification(root_dir, downstream_dir, upstream_dirs)


# ============================================================
# 结果展示
# ============================================================
def _render_results():
    """渲染结果展示区域"""
    result = st.session_state['result']
    matrix = result['matrix']
    output_path = result['output_path']

    st.subheader("📊 结果展示")

    # 下载按钮
    with open(output_path, 'rb') as f:
        file_data = f.read()

    st.download_button(
        label="📥 下载Excel文件",
        data=file_data,
        file_name=os.path.basename(output_path),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # 匹配统计
    if result['type'] == 'verification':
        _render_match_statistics(matrix)

    # 预览表格
    _render_preview_table(matrix, result['type'])


def _render_match_statistics(matrix):
    """渲染匹配统计"""
    st.markdown("### 匹配统计")

    green_count = 0
    blue_count = 0
    black_count = 0

    for row in matrix.rows:
        if row.match_result:
            cat = row.match_result.overall_category
            if cat == MatchCategory.GREEN:
                green_count += 1
            elif cat == MatchCategory.BLUE:
                blue_count += 1
            else:
                black_count += 1

    total = len(matrix.rows)
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("总行数", total)
    with col2:
        pct = f"{green_count/total*100:.0f}%" if total else "0%"
        st.metric("🟢 完全一致", f"{green_count} ({pct})")
    with col3:
        pct = f"{blue_count/total*100:.0f}%" if total else "0%"
        st.metric("🔵 语义匹配", f"{blue_count} ({pct})")
    with col4:
        pct = f"{black_count/total*100:.0f}%" if total else "0%"
        st.metric("⚫ 未匹配", f"{black_count} ({pct})")


def _render_preview_table(matrix, result_type: str):
    """渲染预览表格(HTML带颜色)"""
    st.markdown("### 追踪矩阵预览")

    if result_type == 'verification':
        # 带颜色的HTML表格
        html = _build_colored_html_table(matrix)
        st.markdown(html, unsafe_allow_html=True)
    else:
        # 简单表格
        import pandas as pd
        data = []
        for row in matrix.rows:
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


def _build_colored_html_table(matrix) -> str:
    """构建带颜色标记的HTML预览表格"""
    rows_html = []

    for row in matrix.rows:
        ds_cell = _build_colored_cell_html(row.downstream_content, row.match_result, 'downstream')
        up_cell = _build_colored_cell_html(row.upstream_content, row.match_result, 'upstream')

        rows_html.append(f"""
        <tr>
            <td style="text-align:center">{row.seq_number}</td>
            <td>{_html_escape(row.downstream_id)}</td>
            <td style="font-size:0.85em">{ds_cell}</td>
            <td>{_html_escape(row.upstream_doc)}</td>
            <td>{_html_escape(row.upstream_ref[:30])}</td>
            <td style="font-size:0.85em">{up_cell}</td>
        </tr>
        """)

    html = f"""
    <div style="overflow-x:auto; max-height:600px; overflow-y:auto;">
    <table style="border-collapse:collapse; width:100%; font-size:0.8em;">
    <thead>
    <tr style="background:#4472C4; color:white; position:sticky; top:0;">
        <th style="padding:8px; border:1px solid #ddd; width:40px;">序号</th>
        <th style="padding:8px; border:1px solid #ddd; width:100px;">下游条目号</th>
        <th style="padding:8px; border:1px solid #ddd;">下游内容</th>
        <th style="padding:8px; border:1px solid #ddd; width:100px;">上游文档</th>
        <th style="padding:8px; border:1px solid #ddd; width:120px;">上游条目号/章节</th>
        <th style="padding:8px; border:1px solid #ddd;">上游内容</th>
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
        <span style="color:#333;">■ 黑色=未匹配</span>
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

    from core.requirement_extractor import detect_document_type, extract_full_text

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
    root_dir, downstream_dir, upstream_dirs = render_sidebar()
    render_main_area(root_dir, downstream_dir, upstream_dirs)


if __name__ == '__main__':
    main()
