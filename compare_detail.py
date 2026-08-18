from __future__ import annotations
"""
精细对比: 验证结果 vs 更新后基准 — 逐单元格逐run详细分析
输出到 compare_detail.txt
"""
import zipfile, xml.etree.ElementTree as ET, re, io, os

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'

RESULT_PATH = r'E:\Trace_NL\output\追踪验证结果_v5.xlsx'
BASELINE_PATH = r'E:\Trace_NL\基准数据\Trace_Base.xlsx'

def hex_to_color(h):
    if not h: return 'BLACK'
    h = h.upper().lstrip('#')
    if len(h) == 8: h = h[2:]
    m = {'00B050':'GREEN','92D050':'GREEN','0070C0':'BLUE','00B0F0':'BLUE','002060':'BLUE','000000':'BLACK','FFFFFF':'WHITE'}
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

def build_style_color_map(styles_root):
    """构建 style_index → 颜色名称 的映射"""
    # 字体列表
    fonts = styles_root.find(f'{{{NS}}}fonts')
    font_colors = {}
    # theme颜色近似映射 (Office默认主题)
    theme_map = {0: 'WHITE', 1: 'BLACK', 2: 'BLACK', 3: 'BLACK', 4: 'BLACK'}
    if fonts is not None:
        for i, font in enumerate(fonts.findall(f'{{{NS}}}font')):
            color_el = font.find(f'{{{NS}}}color')
            if color_el is not None:
                rgb = color_el.get('rgb', '')
                if rgb:
                    font_colors[i] = hex_to_color(rgb)
                else:
                    theme = color_el.get('theme', '')
                    if theme != '':
                        font_colors[i] = theme_map.get(int(theme), 'BLACK')
                    else:
                        font_colors[i] = 'BLACK'
            else:
                font_colors[i] = 'BLACK'

    # cellXfs: style_index → fontId
    cellxfs = styles_root.find(f'{{{NS}}}cellXfs')
    style_colors = {}
    if cellxfs is not None:
        for i, xf in enumerate(cellxfs.findall(f'{{{NS}}}xf')):
            font_id = int(xf.get('fontId', '0'))
            style_colors[i] = font_colors.get(font_id, 'BLACK')
    return style_colors

def extract_sheet1(xlsx_path):
    with zipfile.ZipFile(xlsx_path) as z:
        # 构建样式→颜色映射
        style_colors = {}
        if 'xl/styles.xml' in z.namelist():
            styles_root = ET.parse(z.open('xl/styles.xml')).getroot()
            style_colors = build_style_color_map(styles_root)

        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.parse(z.open('xl/sharedStrings.xml')).getroot()
            for si in root.findall(f'{{{NS}}}si'):
                runs = si.findall(f'{{{NS}}}r')
                if runs:
                    shared.append([(get_run_color(r), get_run_text(r)) for r in runs])
                else:
                    t = si.find(f'{{{NS}}}t')
                    # 标记为需要样式解析的占位符 (None表示需要从cell style获取颜色)
                    shared.append([(None, t.text if t is not None and t.text else '')])

        root = ET.parse(z.open('xl/worksheets/sheet1.xml')).getroot()
        cells = {}
        for row_el in root.iter(f'{{{NS}}}row'):
            for cell_el in row_el.findall(f'{{{NS}}}c'):
                ref = cell_el.get('r')
                if not ref: continue

                style_idx = int(cell_el.get('s', '0'))
                cell_color = style_colors.get(style_idx, 'BLACK')

                is_el = cell_el.find(f'{{{NS}}}is')
                if is_el is not None:
                    runs = is_el.findall(f'{{{NS}}}r')
                    if runs:
                        cells[ref] = [(get_run_color(r), get_run_text(r)) for r in runs]
                    else:
                        t = is_el.find(f'{{{NS}}}t')
                        cells[ref] = [(cell_color, t.text if t is not None and t.text else '')]
                    continue

                ct = cell_el.get('t','')
                v = cell_el.find(f'{{{NS}}}v')
                if ct == 's' and v is not None and v.text:
                    idx = int(v.text)
                    if idx < len(shared):
                        entry = shared[idx]
                        # 如果shared string没有runs(颜色为None), 使用单元格样式颜色
                        if entry[0][0] is None:
                            cells[ref] = [(cell_color, entry[0][1])]
                        else:
                            cells[ref] = entry
                    continue
                if v is not None and v.text:
                    cells[ref] = [(cell_color, v.text)]
        return cells

def content_cells(cells):
    return {ref: runs for ref, runs in cells.items()
            if re.match(r'^[CF]\d+$', ref) and int(ref[1:]) >= 2}

def color_stats(runs):
    g = sum(len(t) for c,t in runs if c == 'GREEN')
    b = sum(len(t) for c,t in runs if c == 'BLUE')
    k = sum(len(t) for c,t in runs if c not in ('GREEN','BLUE','WHITE'))
    total = g + b + k
    return g, b, k, total

def sort_key(ref):
    return (int(ref[1:]), ref[0])

def clean_preview(text, maxlen=80):
    s = text.replace('\n','\\n').replace('\r','')
    return s[:maxlen] + ('...' if len(s)>maxlen else '')

def main():
    out = io.StringIO()
    
    r_cells = extract_sheet1(RESULT_PATH)
    b_cells = extract_sheet1(BASELINE_PATH)
    rc = content_cells(r_cells)
    bc = content_cells(b_cells)
    
    all_refs = sorted(set(rc) | set(bc), key=sort_key)
    
    # ============================================
    # PART 1: 总览表
    # ============================================
    p = lambda *a, **kw: print(*a, **kw, file=out)
    
    p("=" * 120)
    p("  精细对比: 验证结果 vs 更新后基准")
    p("=" * 120)
    
    # 总体统计
    rg = sum(color_stats(rc[r])[0] for r in rc)
    rb = sum(color_stats(rc[r])[1] for r in rc)
    rk = sum(color_stats(rc[r])[2] for r in rc)
    rt = sum(color_stats(rc[r])[3] for r in rc)
    bg = sum(color_stats(bc[r])[0] for r in bc)
    bb = sum(color_stats(bc[r])[1] for r in bc)
    bk = sum(color_stats(bc[r])[2] for r in bc)
    bt = sum(color_stats(bc[r])[3] for r in bc)
    p(f"\n  结果总计: G:{rg}({rg/max(rt,1)*100:.1f}%) B:{rb}({rb/max(rt,1)*100:.1f}%) K:{rk}({rk/max(rt,1)*100:.1f}%) total:{rt}")
    p(f"  基准总计: G:{bg}({bg/max(bt,1)*100:.1f}%) B:{bb}({bb/max(bt,1)*100:.1f}%) K:{bk}({bk/max(bt,1)*100:.1f}%) total:{bt}")
    
    # 逐单元格概览
    p(f"\n{'='*120}")
    p(f"  {'Cell':<6} {'R-runs':>6} {'R-G%':>7} {'R-B%':>7} {'R-K%':>7} | {'B-runs':>6} {'B-G%':>7} {'B-B%':>7} {'B-K%':>7} | {'Dist':>6} {'Match?'}")
    p(f"{'='*120}")
    
    diff_cells = []
    match_cells = []
    
    for ref in all_refs:
        rr = rc.get(ref, [])
        br = bc.get(ref, [])
        rg_, rb_, rk_, rt_ = color_stats(rr)
        bg_, bb_, bk_, bt_ = color_stats(br)
        if rt_ == 0 and bt_ == 0: continue
        
        rp = (rg_/max(rt_,1)*100, rb_/max(rt_,1)*100, rk_/max(rt_,1)*100)
        bp = (bg_/max(bt_,1)*100, bb_/max(bt_,1)*100, bk_/max(bt_,1)*100)
        dist = sum((a-b)**2 for a,b in zip(rp, bp))**0.5 / 100
        
        flag = ''
        if dist > 0.30: flag = '***'
        elif dist > 0.15: flag = '**'
        elif dist > 0.05: flag = '*'
        
        p(f"  {ref:<6} {len(rr):>6} {rp[0]:>6.1f}% {rp[1]:>6.1f}% {rp[2]:>6.1f}% | "
          f"{len(br):>6} {bp[0]:>6.1f}% {bp[1]:>6.1f}% {bp[2]:>6.1f}% | "
          f"{dist:>6.3f} {flag}")
        
        if dist > 0.05:
            diff_cells.append((ref, dist, rr, br))
        else:
            match_cells.append(ref)
    
    p(f"\n  匹配(<=5%): {len(match_cells)}个  差异(>5%): {len(diff_cells)}个")
    
    # ============================================
    # PART 2: 逐单元格详细对比
    # ============================================
    p(f"\n{'='*120}")
    p(f"  逐单元格详细对比 (差异>5%的单元格)")
    p(f"{'='*120}")
    
    # 差异分类统计
    over_green = 0   # 结果GREEN但基准不是
    under_green = 0  # 基准GREEN但结果不是
    over_blue = 0
    under_blue = 0
    
    for ref, dist, rr, br in sorted(diff_cells, key=lambda x: -x[1]):
        rg_, rb_, rk_, rt_ = color_stats(rr)
        bg_, bb_, bk_, bt_ = color_stats(br)
        
        p(f"\n{'─'*100}")
        p(f"  [{ref}] dist={dist:.3f}  结果:G{rg_/max(rt_,1)*100:.0f}%B{rb_/max(rt_,1)*100:.0f}%K{rk_/max(rt_,1)*100:.0f}%  "
          f"基准:G{bg_/max(bt_,1)*100:.0f}%B{bb_/max(bt_,1)*100:.0f}%K{bk_/max(bt_,1)*100:.0f}%")
        p(f"{'─'*100}")
        
        # 结果runs
        p(f"  结果 ({len(rr)} runs, {rt_} chars):")
        for i, (c, t) in enumerate(rr):
            p(f"    R{i+1:2d} [{c:5}] ({len(t):4d}ch) {clean_preview(t, 90)}")
        
        # 基准runs
        p(f"  基准 ({len(br)} runs, {bt_} chars):")
        for i, (c, t) in enumerate(br):
            p(f"    B{i+1:2d} [{c:5}] ({len(t):4d}ch) {clean_preview(t, 90)}")
        
        # 差异分析: 逐字符颜色对比 (简化版)
        r_text = ''.join(t for _,t in rr)
        b_text = ''.join(t for _,t in br)
        
        # 构建字符级颜色映射
        def build_char_colors(runs):
            colors = []
            for c, t in runs:
                for ch in t:
                    colors.append(c)
            return colors
        
        r_colors = build_char_colors(rr)
        b_colors = build_char_colors(br)
        
        # 逐字符比较 (取较短长度)
        min_len = min(len(r_colors), len(b_colors))
        mismatches = []
        for i in range(min_len):
            if r_colors[i] != b_colors[i]:
                mismatches.append(i)
        
        # 统计颜色不匹配的字符
        r_green_not_b = 0
        b_green_not_r = 0
        r_blue_not_b = 0
        b_blue_not_r = 0
        
        for i in mismatches:
            rc_ = r_colors[i]
            bc_ = b_colors[i]
            if rc_ == 'GREEN' and bc_ != 'GREEN': r_green_not_b += 1
            if bc_ == 'GREEN' and rc_ != 'GREEN': b_green_not_r += 1
            if rc_ == 'BLUE' and bc_ != 'BLUE': r_blue_not_b += 1
            if bc_ == 'BLUE' and rc_ != 'BLUE': b_blue_not_r += 1
        
        over_green += r_green_not_b
        under_green += b_green_not_r
        over_blue += r_blue_not_b
        under_blue += b_blue_not_r
        
        if mismatches:
            p(f"  颜色不匹配: {len(mismatches)}/{min_len} 字符 ({len(mismatches)/max(min_len,1)*100:.1f}%)")
            p(f"    结果多GREEN: {r_green_not_b}ch  基准多GREEN: {b_green_not_r}ch")
            p(f"    结果多BLUE:  {r_blue_not_b}ch  基准多BLUE:  {b_blue_not_r}ch")
            
            # 显示不匹配区段 (合并连续不匹配)
            segments = []
            if mismatches:
                start = mismatches[0]
                prev = mismatches[0]
                for m in mismatches[1:]:
                    if m - prev > 3:  # gap > 3 → 新segment
                        segments.append((start, prev))
                        start = m
                    prev = m
                segments.append((start, prev))
            
            p(f"  不匹配区段 ({len(segments)}段):")
            for seg_start, seg_end in segments[:10]:
                # 上下文
                ctx_start = max(0, seg_start - 5)
                ctx_end = min(min_len, seg_end + 6)
                r_ctx = ''.join(
                    r_text[i] if i < len(r_text) else '?'
                    for i in range(ctx_start, ctx_end)
                ).replace('\n','\\n')
                
                r_seg_colors = [r_colors[i] for i in range(seg_start, seg_end+1)]
                b_seg_colors = [b_colors[i] for i in range(seg_start, seg_end+1)]
                r_color_str = '/'.join(set(r_seg_colors))
                b_color_str = '/'.join(set(b_seg_colors))
                
                p(f"    pos {seg_start:4d}-{seg_end:4d} ({seg_end-seg_start+1:3d}ch) "
                  f"结果:{r_color_str:12s} 基准:{b_color_str:12s} 「{r_ctx}」")
        else:
            if rt_ != bt_:
                p(f"  颜色完全匹配但长度不同: 结果{rt_}ch vs 基准{bt_}ch")
            else:
                p(f"  颜色完全匹配")
    
    # ============================================
    # PART 3: 差异分类汇总
    # ============================================
    p(f"\n{'='*120}")
    p(f"  差异分类汇总")
    p(f"{'='*120}")
    p(f"  结果多GREEN(过度匹配): {over_green} 字符")
    p(f"  基准多GREEN(匹配不足): {under_green} 字符")
    p(f"  结果多BLUE (过度语义):  {over_blue} 字符")
    p(f"  基准多BLUE (语义不足):  {b_blue_not_r} 字符")
    p(f"  总不匹配字符: {over_green + under_green + over_blue + b_blue_not_r}")
    
    # PART 4: 完全匹配的单元格列表
    p(f"\n{'='*120}")
    p(f"  完全匹配或近似匹配的单元格 (dist <= 5%)")
    p(f"{'='*120}")
    for ref in match_cells:
        rr = rc.get(ref, [])
        br = bc.get(ref, [])
        rg_, rb_, rk_, rt_ = color_stats(rr)
        bg_, bb_, bk_, bt_ = color_stats(br)
        dist = sum((a-b)**2 for a,b in zip(
            (rg_/max(rt_,1), rb_/max(rt_,1), rk_/max(rt_,1)),
            (bg_/max(bt_,1), bb_/max(bt_,1), bk_/max(bt_,1))
        ))**0.5
        p(f"  {ref}: dist={dist:.3f}  结果:G{rg_/max(rt_,1)*100:.0f}%B{rb_/max(rt_,1)*100:.0f}%K{rk_/max(rt_,1)*100:.0f}%  "
          f"基准:G{bg_/max(bt_,1)*100:.0f}%B{bb_/max(bt_,1)*100:.0f}%K{bk_/max(bt_,1)*100:.0f}%")
    
    result_text = out.getvalue()
    outpath = r'E:\Trace_NL\compare_detail.txt'
    with open(outpath, 'w', encoding='utf-8') as f:
        f.write(result_text)
    print(f"Done. {len(result_text)} chars → {outpath}")

if __name__ == '__main__':
    main()
