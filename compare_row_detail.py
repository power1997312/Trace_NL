from __future__ import annotations
"""
逐追踪关系精细对比: 验证结果 vs 基准数据
- 按行(追踪关系)组织对比
- 每个单元格逐run分析颜色差异
- 差异分类归因
"""
import zipfile, xml.etree.ElementTree as ET, re, io, os

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'

RESULT_PATH = r'E:\Trace_NL\output\追踪验证结果_v5.xlsx'
BASELINE_PATH = r'E:\Trace_NL\基准数据\Trace_Base.xlsx'

def hex_to_color(h):
    if not h: return 'BLACK'
    h = h.upper().lstrip('#')
    if len(h) == 8: h = h[2:]
    m = {'00B050':'GREEN','0070C0':'BLUE','000000':'BLACK','FFFFFF':'WHITE'}
    return m.get(h, f'#{h}')

def get_run_color(r_el):
    rPr = r_el.find(f'{{{NS}}}rPr')
    if rPr is not None:
        c = rPr.find(f'{{{NS}}}color')
        if c is not None:
            return hex_to_color(c.get('rgb',''))
    return 'BLACK'

def get_run_text(r_el):
    t = r_el.find(f'{{{NS}}}t')
    return t.text if t is not None and t.text else ''

def extract_all_sheets(xlsx_path):
    """提取xlsx中所有sheet的单元格数据"""
    with zipfile.ZipFile(xlsx_path) as z:
        # 读取sharedStrings
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.parse(z.open('xl/sharedStrings.xml')).getroot()
            for si in root.findall(f'{{{NS}}}si'):
                runs = si.findall(f'{{{NS}}}r')
                if runs:
                    shared.append([(get_run_color(r), get_run_text(r)) for r in runs])
                else:
                    t = si.find(f'{{{NS}}}t')
                    shared.append([('BLACK', t.text if t is not None and t.text else '')])

        # 读取workbook获取sheet名
        wb_root = ET.parse(z.open('xl/workbook.xml')).getroot()
        wb_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        r_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        sheets_info = []
        for s in wb_root.findall(f'{{{wb_ns}}}sheets/{{{wb_ns}}}sheet'):
            sheets_info.append(s.get('name'))

        # 读取每个sheet
        result = {}
        for idx, sheet_name in enumerate(sheets_info):
            sheet_file = f'xl/worksheets/sheet{idx+1}.xml'
            if sheet_file not in z.namelist():
                continue
            root = ET.parse(z.open(sheet_file)).getroot()
            cells = {}
            for row_el in root.iter(f'{{{NS}}}row'):
                for cell_el in row_el.findall(f'{{{NS}}}c'):
                    ref = cell_el.get('r')
                    if not ref: continue

                    is_el = cell_el.find(f'{{{NS}}}is')
                    if is_el is not None:
                        runs = is_el.findall(f'{{{NS}}}r')
                        if runs:
                            cells[ref] = [(get_run_color(r), get_run_text(r)) for r in runs]
                        else:
                            t = is_el.find(f'{{{NS}}}t')
                            cells[ref] = [('BLACK', t.text if t is not None and t.text else '')]
                        continue

                    ct = cell_el.get('t','')
                    v = cell_el.find(f'{{{NS}}}v')
                    if ct == 's' and v is not None and v.text:
                        sidx = int(v.text)
                        if sidx < len(shared):
                            cells[ref] = shared[sidx]
                        continue
                    if v is not None and v.text:
                        cells[ref] = [('BLACK', v.text)]
            result[sheet_name] = cells
    return result


def color_stats(runs):
    g = sum(len(t) for c,t in runs if c == 'GREEN')
    b = sum(len(t) for c,t in runs if c == 'BLUE')
    k = sum(len(t) for c,t in runs if c not in ('GREEN','BLUE','WHITE'))
    return g, b, k, g+b+k


def build_char_colors(runs):
    """构建字符级颜色映射"""
    colors = []
    for c, t in runs:
        for ch in t:
            colors.append(c)
    return colors


def merge_runs(runs):
    """合并相邻同色run"""
    if not runs:
        return []
    merged = [runs[0]]
    for c, t in runs[1:]:
        if c == merged[-1][0]:
            merged[-1] = (c, merged[-1][1] + t)
        else:
            merged.append((c, t))
    return merged


def clean_preview(text, maxlen=60):
    s = text.replace('\n','\\n').replace('\r','')
    return s[:maxlen] + ('...' if len(s)>maxlen else '')


def diff_analysis(r_runs, b_runs):
    """分析两个单元格的差异细节"""
    r_text = ''.join(t for _,t in r_runs)
    b_text = ''.join(t for _,t in b_runs)
    r_colors = build_char_colors(r_runs)
    b_colors = build_char_colors(b_runs)

    result = {
        'text_same': r_text == b_text,
        'r_len': len(r_text),
        'b_len': len(b_text),
        'r_runs': len(r_runs),
        'b_runs': len(b_runs),
    }

    rg, rb, rk, rt = color_stats(r_runs)
    bg, bb, bk, bt = color_stats(b_runs)
    result['r_g'] = rg; result['r_b'] = rb; result['r_k'] = rk; result['r_t'] = rt
    result['b_g'] = bg; result['b_b'] = bb; result['b_k'] = bk; result['b_t'] = bt

    # 颜色分布距离
    if rt > 0 and bt > 0:
        rp = (rg/rt*100, rb/rt*100, rk/rt*100)
        bp = (bg/bt*100, bb/bt*100, bk/bt*100)
        result['dist'] = sum((a-b)**2 for a,b in zip(rp, bp))**0.5 / 100
    else:
        result['dist'] = 0.0 if rt == bt else 1.0

    # 字符级颜色对比
    min_len = min(len(r_colors), len(b_colors))
    r_extra_green = 0; b_extra_green = 0
    r_extra_blue = 0; b_extra_blue = 0
    mismatches = []

    for i in range(min_len):
        rc = r_colors[i]
        bc = b_colors[i]
        if rc != bc:
            mismatches.append(i)
            if rc == 'GREEN' and bc != 'GREEN': r_extra_green += 1
            if bc == 'GREEN' and rc != 'GREEN': b_extra_green += 1
            if rc == 'BLUE' and bc != 'BLUE': r_extra_blue += 1
            if bc == 'BLUE' and rc != 'BLUE': b_extra_blue += 1

    result['mismatches'] = len(mismatches)
    result['mismatch_pct'] = len(mismatches) / max(min_len, 1) * 100
    result['r_extra_green'] = r_extra_green
    result['b_extra_green'] = b_extra_green
    result['r_extra_blue'] = r_extra_blue
    result['b_extra_blue'] = b_extra_blue

    # 不匹配区段
    segments = []
    if mismatches:
        start = mismatches[0]; prev = mismatches[0]
        for m in mismatches[1:]:
            if m - prev > 3:
                segments.append((start, prev))
                start = m
            prev = m
        segments.append((start, prev))
    result['segments'] = segments

    # 构建区段详情
    segment_details = []
    for seg_start, seg_end in segments:
        r_seg_colors = set(r_colors[seg_start:seg_end+1])
        b_seg_colors = set(b_colors[seg_start:seg_end+1])
        ctx_start = max(0, seg_start - 5)
        ctx_end = min(min_len, seg_end + 6)
        r_ctx = ''.join(r_text[i] if i < len(r_text) else '?' for i in range(ctx_start, ctx_end))
        segment_details.append({
            'start': seg_start, 'end': seg_end,
            'len': seg_end - seg_start + 1,
            'r_colors': '/'.join(sorted(r_seg_colors)),
            'b_colors': '/'.join(sorted(b_seg_colors)),
            'context': r_ctx,
        })
    result['segment_details'] = segment_details

    # 差异分类
    if result['text_same'] and result['dist'] == 0.0:
        result['diff_type'] = '完全一致'
    elif result['text_same'] and result['dist'] < 0.05:
        result['diff_type'] = '近似一致'
    elif not result['text_same']:
        result['diff_type'] = '文本不同'
    elif r_extra_green > b_extra_green + r_extra_blue + b_extra_blue:
        result['diff_type'] = '结果过度GREEN'
    elif b_extra_green > r_extra_green + r_extra_blue + b_extra_blue:
        result['diff_type'] = '结果GREEN不足'
    elif r_extra_blue > b_extra_blue + r_extra_green + b_extra_green:
        result['diff_type'] = '结果过度BLUE'
    elif b_extra_blue > r_extra_blue + r_extra_green + b_extra_green:
        result['diff_type'] = '结果BLUE不足'
    else:
        result['diff_type'] = '颜色分布差异'

    return result


def main():
    out = io.StringIO()
    p = lambda *a, **kw: print(*a, **kw, file=out)

    # 读取文件
    r_sheets = extract_all_sheets(RESULT_PATH)
    b_sheets = extract_all_sheets(BASELINE_PATH)

    r_sheet_names = list(r_sheets.keys())
    b_sheet_names = list(b_sheets.keys())

    p("=" * 100)
    p("  逐追踪关系精细对比报告")
    p("  结果文件: 追踪验证结果_v3.xlsx")
    p("  基准文件: Trace_Base.xlsx")
    p("=" * 100)
    p(f"\n  结果sheets: {r_sheet_names}")
    p(f"  基准sheets: {b_sheet_names}")

    # ============================================
    # Part 1: 逆向追踪矩阵对比
    # ============================================
    r_cells = r_sheets[r_sheet_names[0]]  # 第一个sheet=逆向矩阵
    b_cells = b_sheets[b_sheet_names[0]]

    p(f"\n{'='*100}")
    p(f"  第一部分: 逆向追踪矩阵对比")
    p(f"{'='*100}")

    # 列含义: A=序号, B=下游ID, C=下游内容, D=上游文档, E=上游引用, F=上游内容
    COL_NAMES = {'A': '序号', 'B': '下游ID', 'C': '下游内容', 'D': '上游文档', 'E': '上游引用', 'F': '上游内容'}
    CONTENT_COLS = ['C', 'F']  # 需要对比颜色的内容列

    # 收集所有行的信息
    row_info = {}
    for col_letter in ['A', 'B', 'D', 'E']:
        for r in range(2, 20):
            ref = f'{col_letter}{r}'
            if ref in r_cells:
                row_num = r
                if row_num not in row_info:
                    row_info[row_num] = {}
                text = ''.join(t for _,t in r_cells[ref])
                row_info[row_num][col_letter] = text

    # 逐行对比
    all_rows = sorted(row_info.keys())
    total_content_cells = 0
    matched_cells = 0
    diff_cells_list = []

    # 差异类型统计
    diff_type_counts = {}
    total_r_extra_green = 0
    total_b_extra_green = 0
    total_r_extra_blue = 0
    total_b_extra_blue = 0

    p(f"\n  ┌──────┬──────────────────────┬────────────────────┬────────────────────────┬────────────┬────────┐")
    p(f"  │  行  │ 下游ID               │ 上游引用           │ 差异类型               │ C列 dist  │ F dist │")
    p(f"  ├──────┼──────────────────────┼────────────────────┼────────────────────────┼────────────┼────────┤")

    for row_num in all_rows:
        info = row_info[row_num]
        ds_id = info.get('B', '?')
        up_ref = info.get('E', '?')
        seq = info.get('A', '?')

        row_diff_types = []

        for col in CONTENT_COLS:
            ref = f'{col}{row_num}'
            r_runs = r_cells.get(ref, [])
            b_runs = b_cells.get(ref, [])
            if not r_runs and not b_runs:
                continue

            total_content_cells += 1
            analysis = diff_analysis(r_runs, b_runs)

            if analysis['dist'] <= 0.05:
                matched_cells += 1
            else:
                diff_cells_list.append((row_num, col, analysis, r_runs, b_runs))
                row_diff_types.append(f"{col}:{analysis['diff_type']}")

            total_r_extra_green += analysis['r_extra_green']
            total_b_extra_green += analysis['b_extra_green']
            total_r_extra_blue += analysis['r_extra_blue']
            total_b_extra_blue += analysis['b_extra_blue']

            dt = analysis['diff_type']
            diff_type_counts[dt] = diff_type_counts.get(dt, 0) + 1

        c_dist = ''
        f_dist = ''
        for rn, col, ana, _, _ in diff_cells_list:
            if rn == row_num and col == 'C':
                c_dist = f"{ana['dist']:.3f}"
            if rn == row_num and col == 'F':
                f_dist = f"{ana['dist']:.3f}"

        # 检查是否完全匹配
        has_diff = any(rn == row_num for rn, _, _, _, _ in diff_cells_list)
        diff_str = ', '.join(row_diff_types) if row_diff_types else '完全一致'
        if not has_diff:
            c_dist = '✓'
            f_dist = '✓'

        p(f"  │ {seq:>4} │ {ds_id[:20]:<20} │ {up_ref[:18]:<18} │ {diff_str[:22]:<22} │ {c_dist:>10} │ {f_dist:>6} │")

    p(f"  └──────┴──────────────────────┴────────────────────┴────────────────────────┴────────────┴────────┘")

    p(f"\n  总内容单元格: {total_content_cells}")
    p(f"  匹配(dist≤5%): {matched_cells}")
    p(f"  差异(dist>5%): {len(diff_cells_list)}")

    p(f"\n  差异类型分布:")
    for dt, cnt in sorted(diff_type_counts.items(), key=lambda x: -x[1]):
        p(f"    {dt}: {cnt}个")

    p(f"\n  颜色偏差汇总:")
    p(f"    结果多GREEN: {total_r_extra_green} 字符")
    p(f"    基准多GREEN: {total_b_extra_green} 字符")
    p(f"    结果多BLUE:  {total_r_extra_blue} 字符")
    p(f"    基准多BLUE:  {total_b_extra_blue} 字符")

    # ============================================
    # Part 2: 逐行逐单元格详细分析
    # ============================================
    p(f"\n{'='*100}")
    p(f"  第二部分: 差异单元格详细分析")
    p(f"{'='*100}")

    # 按差异距离降序排列
    diff_cells_list.sort(key=lambda x: -x[2]['dist'])

    for row_num, col, analysis, r_runs, b_runs in diff_cells_list:
        info = row_info.get(row_num, {})
        seq = info.get('A', '?')
        ds_id = info.get('B', '?')
        up_ref = info.get('E', '?')
        col_name = COL_NAMES.get(col, col)

        p(f"\n{'─'*100}")
        p(f"  行{seq} [{col}列:{col_name}] dist={analysis['dist']:.3f}  差异类型: {analysis['diff_type']}")
        p(f"  下游: {ds_id}  上游引用: {up_ref}")
        p(f"{'─'*100}")

        # 文本对比
        if not analysis['text_same']:
            p(f"  [文本差异] 结果{analysis['r_len']}字符 vs 基准{analysis['b_len']}字符")
            r_text = ''.join(t for _,t in r_runs)
            b_text = ''.join(t for _,t in b_runs)
            # 找第一个不同位置
            min_t = min(len(r_text), len(b_text))
            first_diff = -1
            for i in range(min_t):
                if r_text[i] != b_text[i]:
                    first_diff = i
                    break
            if first_diff == -1 and len(r_text) != len(b_text):
                first_diff = min_t
            if first_diff >= 0:
                ctx_s = max(0, first_diff - 10)
                ctx_e = min(max(len(r_text), len(b_text)), first_diff + 20)
                p(f"    首个差异位置: 第{first_diff}字符")
                p(f"    结果: ...{clean_preview(r_text[ctx_s:ctx_e], 50)}...")
                p(f"    基准: ...{clean_preview(b_text[ctx_s:ctx_e], 50)}...")
        else:
            p(f"  [文本相同] {analysis['r_len']}字符")

        # 颜色分布
        p(f"  结果颜色: G:{analysis['r_g']}({analysis['r_g']/max(analysis['r_t'],1)*100:.0f}%) "
          f"B:{analysis['r_b']}({analysis['r_b']/max(analysis['r_t'],1)*100:.0f}%) "
          f"K:{analysis['r_k']}({analysis['r_k']/max(analysis['r_t'],1)*100:.0f}%) "
          f"({analysis['r_runs']} runs)")
        p(f"  基准颜色: G:{analysis['b_g']}({analysis['b_g']/max(analysis['b_t'],1)*100:.0f}%) "
          f"B:{analysis['b_b']}({analysis['b_b']/max(analysis['b_t'],1)*100:.0f}%) "
          f"K:{analysis['b_k']}({analysis['b_k']/max(analysis['b_t'],1)*100:.0f}%) "
          f"({analysis['b_runs']} runs)")

        # 颜色偏差
        p(f"  颜色偏差: 结果多GREEN:{analysis['r_extra_green']}ch "
          f"基准多GREEN:{analysis['b_extra_green']}ch "
          f"结果多BLUE:{analysis['r_extra_blue']}ch "
          f"基准多BLUE:{analysis['b_extra_blue']}ch")

        # 不匹配区段详情
        if analysis['segment_details']:
            p(f"  不匹配区段 ({len(analysis['segment_details'])}段):")
            for sd in analysis['segment_details'][:8]:
                p(f"    pos {sd['start']:4d}-{sd['end']:4d} ({sd['len']:3d}ch) "
                  f"结果:{sd['r_colors']:15s} 基准:{sd['b_colors']:15s}")
                p(f"      「{clean_preview(sd['context'], 70)}」")

        # Run对比摘要
        r_merged = merge_runs(r_runs)
        b_merged = merge_runs(b_runs)
        p(f"  结果runs(合并后{len(r_merged)}个):")
        for i, (c, t) in enumerate(r_merged[:8]):
            p(f"    R{i+1:2d} [{c:5}] ({len(t):4d}ch) {clean_preview(t, 70)}")
        if len(r_merged) > 8:
            p(f"    ... 还有{len(r_merged)-8}个run")

        p(f"  基准runs(合并后{len(b_merged)}个):")
        for i, (c, t) in enumerate(b_merged[:8]):
            p(f"    B{i+1:2d} [{c:5}] ({len(t):4d}ch) {clean_preview(t, 70)}")
        if len(b_merged) > 8:
            p(f"    ... 还有{len(b_merged)-8}个run")

    # ============================================
    # Part 3: 差异原因归类
    # ============================================
    p(f"\n{'='*100}")
    p(f"  第三部分: 差异原因归类分析")
    p(f"{'='*100}")

    # 归类
    categories = {
        'A_文本不同_PDF提取差异': [],
        'B_结果过度GREEN': [],
        'C_结果GREEN不足': [],
        'D_结果过度BLUE': [],
        'E_结果BLUE不足': [],
        'F_颜色分布差异': [],
    }

    for row_num, col, analysis, r_runs, b_runs in diff_cells_list:
        dt = analysis['diff_type']
        info = row_info.get(row_num, {})
        entry = (row_num, col, info.get('A','?'), info.get('B','?'), info.get('E','?'), analysis)

        if dt == '文本不同':
            categories['A_文本不同_PDF提取差异'].append(entry)
        elif dt == '结果过度GREEN':
            categories['B_结果过度GREEN'].append(entry)
        elif dt == '结果GREEN不足':
            categories['C_结果GREEN不足'].append(entry)
        elif dt == '结果过度BLUE':
            categories['D_结果过度BLUE'].append(entry)
        elif dt == '结果BLUE不足':
            categories['E_结果BLUE不足'].append(entry)
        else:
            categories['F_颜色分布差异'].append(entry)

    for cat_name, entries in categories.items():
        if not entries:
            continue
        p(f"\n  ── {cat_name} ({len(entries)}个) ──")
        for row_num, col, seq, ds_id, up_ref, ana in entries:
            col_name = COL_NAMES.get(col, col)
            p(f"    行{seq} [{col}列] dist={ana['dist']:.3f}  "
              f"结果:G{ana['r_g']/max(ana['r_t'],1)*100:.0f}%B{ana['r_b']/max(ana['r_t'],1)*100:.0f}%K{ana['r_k']/max(ana['r_t'],1)*100:.0f}%  "
              f"基准:G{ana['b_g']/max(ana['b_t'],1)*100:.0f}%B{ana['b_b']/max(ana['b_t'],1)*100:.0f}%K{ana['b_k']/max(ana['b_t'],1)*100:.0f}%")

            # 简要原因分析
            if cat_name.startswith('A'):
                if abs(ana['r_len'] - ana['b_len']) > 10:
                    p(f"      → 长度差异大({ana['r_len']} vs {ana['b_len']}), PDF文本提取结果不同")
                else:
                    p(f"      → 长度相近({ana['r_len']} vs {ana['b_len']}), 可能是格式/标点差异")
            elif cat_name.startswith('B'):
                p(f"      → 结果将基准的BLACK/BLUE内容误标为GREEN")
            elif cat_name.startswith('C'):
                p(f"      → 基准标GREEN但结果未识别, 基准多GREEN:{ana['b_extra_green']}ch")
            elif cat_name.startswith('D'):
                p(f"      → 结果将基准的BLACK/GREEN内容误标为BLUE")
            elif cat_name.startswith('E'):
                p(f"      → 基准标BLUE但结果未识别, 基准多BLUE:{ana['b_extra_blue']}ch")
            else:
                p(f"      → 颜色分布不同但总量相近")

    # ============================================
    # Part 4: 总体统计
    # ============================================
    p(f"\n{'='*100}")
    p(f"  第四部分: 总体统计与改进建议")
    p(f"{'='*100}")

    p(f"\n  1. 匹配率: {matched_cells}/{total_content_cells} "
      f"({matched_cells/total_content_cells*100:.1f}%) 的单元格颜色分布与基准一致(dist≤5%)")

    text_diff_count = len(categories['A_文本不同_PDF提取差异'])
    color_diff_count = sum(len(v) for k,v in categories.items() if not k.startswith('A'))
    p(f"\n  2. 差异分解:")
    p(f"     文本层面差异: {text_diff_count}个单元格 (PDF提取导致)")
    p(f"     颜色标注差异: {color_diff_count}个单元格 (匹配算法导致)")

    p(f"\n  3. 颜色偏差方向:")
    p(f"     结果倾向过度GREEN: {total_r_extra_green}字符 vs 基准多GREEN: {total_b_extra_green}字符")
    p(f"     结果倾向过度BLUE:  {total_r_extra_blue}字符 vs 基准多BLUE:  {total_b_extra_blue}字符")

    if total_b_extra_green > total_r_extra_green:
        p(f"     → 整体: GREEN标注不足 (缺少{total_b_extra_green - total_r_extra_green}字符的GREEN)")
    elif total_r_extra_green > total_b_extra_green:
        p(f"     → 整体: GREEN标注过度 (多出{total_r_extra_green - total_b_extra_green}字符的GREEN)")

    if total_b_extra_blue > total_r_extra_blue:
        p(f"     → 整体: BLUE标注不足 (缺少{total_b_extra_blue - total_r_extra_blue}字符的BLUE)")
    elif total_r_extra_blue > total_b_extra_blue:
        p(f"     → 整体: BLUE标注过度 (多出{total_r_extra_blue - total_b_extra_blue}字符的BLUE)")

    result_text = out.getvalue()
    outpath = r'E:\Trace_NL\逐行精细对比报告.txt'
    with open(outpath, 'w', encoding='utf-8') as f:
        f.write(result_text)
    print(f"Done. {len(result_text)} chars -> {outpath}")


if __name__ == '__main__':
    main()
