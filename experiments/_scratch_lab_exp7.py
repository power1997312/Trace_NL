# -*- coding: utf-8 -*-
"""实验7: 列表项锚点通道 + 稀有n-gram通道 (临时, 用后即删)
场景: 下游长文本(设计细化) 包含 上游列表项片段(机柜温度/电源故障...)
"""
from __future__ import annotations
import os, sys, time, re, math
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import (
    build_pipelines, _IDX, build_idf, _IDF_CACHE,
    ch_sem_asym, ch_contain,
    evaluate, print_eval, Unit, ref_matches_unit, tokenize, norm_for_tokens,
    build_containment, idf_asym,
)
from core.text_matcher import _normalize_for_char_compare

t0 = time.time()
pls = build_pipelines()
for name, (ds, up, gold) in pls.items():
    _IDX.add_units(ds)
    _IDX.add_units(up)
print(f'编码完成 ({time.time()-t0:.1f}s)')

_LIST_ITEM_RE = re.compile(r'^\s*(?:[-•●]|\d+[)）]|[a-zA-Z][)）])\s*(.+)$')


def extract_list_items(text: str, min_len=3) -> list[str]:
    """提取列表项(去编号), 也收集短句(<=25字)作为锚点候选"""
    items = []
    for line in text.split('\n'):
        m = _LIST_ITEM_RE.match(line)
        frag = m.group(1).strip() if m else line.strip()
        frag = frag.rstrip('；;。')
        if len(frag) >= min_len:
            items.append(frag)
    return items


def ch_anchor(d: Unit, u: Unit) -> dict:
    """列表项锚点: 上游列表项被下游(归一化后)包含的比例(长度加权)
    方向: 下游长文本包含上游短锚点"""
    d_norm = _normalize_for_char_compare(d.content)
    anchors = extract_list_items(u.content)
    if not anchors:
        return {'score': 0.0, 'hits': []}
    hits = []
    total_w = 0.0; hit_w = 0.0
    for a in anchors:
        a_norm = _normalize_for_char_compare(a)
        if len(a_norm) < 3:
            continue
        w = len(a_norm)
        total_w += w
        if a_norm in d_norm:
            hit_w += w
            hits.append(a)
        elif len(a_norm) >= 6:
            # 允许80%的bigram落在下游
            bg_a = {a_norm[i:i+2] for i in range(len(a_norm)-1)}
            bg_d = {d_norm[i:i+2] for i in range(len(d_norm)-1)}
            if bg_a and len(bg_a & bg_d) / len(bg_a) >= 0.85:
                hit_w += w * 0.8
                hits.append(a + '~')
    score = hit_w / total_w if total_w else 0.0
    return {'score': score, 'hits': hits}


def ch_ngram(d: Unit, u: Unit, n=4) -> float:
    """稀有CJK n-gram共享度: 双方共有的稀有n-gram权重 / 下游稀有n-gram总权重"""
    df, rmax = _IDF_CACHE['df'], _IDF_CACHE['rare_max']
    N = _IDF_CACHE['N']
    def cjk_ngrams(x):
        t = norm_for_tokens(x)
        runs = re.findall(r'[\u4e00-\u9fff]+', t)
        grams = set()
        for run in runs:
            for i in range(len(run) - n + 1):
                grams.add(run[i:i+n])
        return grams
    gd, gu = cjk_ngrams(d.content), cjk_ngrams(u.content)
    if not gd:
        return 0.0
    # n-gram的df近似: 用其2-gram子串的df最大值估计(保守)
    shared_w = 0.0; total_w = 0.0
    for g in gd:
        # 稀有度: 取子2-gram的最小df
        sub_dfs = [df.get(g[i:i+2], N) for i in range(len(g)-1)]
        gdf = min(sub_dfs) if sub_dfs else N
        if gdf <= rmax:
            w = math.log(N / gdf) + 1.0
            total_w += w
            if g in gu:
                shared_w += w
    return shared_w / total_w if total_w else 0.0


for name, (ds, up, gold) in pls.items():
    print()
    print('=' * 100)
    print(f'流水线 {name}')
    print('=' * 100)
    build_idf(up + ds)
    children = build_containment(up)

    # 单通道判别力
    for cname, fn in [('anchor', lambda d, u: ch_anchor(d, u)['score']),
                      ('ngram4', lambda d, u: ch_ngram(d, u, 4)),
                      ('ngram3', lambda d, u: ch_ngram(d, u, 3))]:
        scores = {d.uid: {u.uid: fn(d, u) for u in up} for d in ds}
        res = evaluate(scores, ds, up, gold)
        print_eval(f'{cname}', res, show_fails=False)

    # 融合: 基础分 + anchor加成
    def fused(d, u, w_anchor, w_ngram):
        base = (0.5 * ch_sem_asym(d, u)['score'] + 0.2 * ch_contain(d, u)
                + 0.2 * idf_asym(d, u, 'R') + 0.1 * 0.0)
        anc = ch_anchor(d, u)['score']
        ng = ch_ngram(d, u, 4)
        # anchor/ngram 作乘性加成(有锚点证据时抬升), 也加性混合
        return (1 - w_anchor - w_ngram) * base + w_anchor * anc + w_ngram * ng

    for wa, wn in [(0.2, 0.1), (0.3, 0.1), (0.25, 0.15), (0.35, 0.05)]:
        scores = {d.uid: {u.uid: fused(d, u, wa, wn) for u in up} for d in ds}
        # 章节降权
        for d in ds:
            for u in up:
                if u.kind == 'section' and children.get(u.uid):
                    scores[d.uid][u.uid] *= 0.85
        res = evaluate(scores, ds, up, gold)
        print_eval(f'base+anchor{wa}+ngram{wn}', res, show_fails=(wa == 0.3 and wn == 0.1))

    # anchor证据样例
    print('--- anchor证据样例(前5条下游) ---')
    shown = 0
    for d in ds:
        if shown >= 5:
            break
        best_u, best_s, best_hits = None, 0, []
        for u in up:
            r = ch_anchor(d, u)
            if r['score'] > best_s:
                best_u, best_s, best_hits = u, r['score'], r['hits']
        if best_u and best_s > 0.15:
            shown += 1
            is_gold = any(
                (isinstance(g, tuple) and g[0] == best_u.doc and ref_matches_unit(g[1], best_u))
                or (not isinstance(g, tuple) and ref_matches_unit(g, best_u))
                for g in gold.get(d.key, [])
            )
            print(f'  {d.key} -> {best_u.key[:24]}({best_s:.2f}){"★" if is_gold else " "} 锚点: {best_hits[:6]}')

print(f'\n总耗时 {time.time()-t0:.1f}s')
