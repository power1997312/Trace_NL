# -*- coding: utf-8 -*-
"""实验6: 工程应用级验证 — 阈值裁决/精确率召回率/UNTRACED/ambiguous (临时, 用后即删)
用各流水线最佳权重, 评估: 关系级精确率/召回率, UNTRACED误报, ambiguous标记率
"""
from __future__ import annotations
import os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import (
    build_pipelines, _IDX, build_idf, _IDF_CACHE,
    ch_sem_asym, ch_contain,
    Unit, ref_matches_unit, tokenize,
    build_containment, idf_asym,
)
import math

t0 = time.time()
pls = build_pipelines()
for name, (ds, up, gold) in pls.items():
    _IDX.add_units(ds)
    _IDX.add_units(up)
print(f'编码完成 ({time.time()-t0:.1f}s)')

# 各流水线最佳权重 (来自实验5网格搜索)
BEST_W = {
    'A':  (0.5, 0.2, 0.2, 0.1),
    'B1': (0.3, 0.2, 0.3, 0.2),
    'B2': (0.5, 0.2, 0.2, 0.1),
}


def build_tfidf_sim(ds, up):
    df, N = _IDF_CACHE['df'], _IDF_CACHE['N']
    vocab = sorted(df.keys())
    vidx = {t: i for i, t in enumerate(vocab)}
    def vec(u):
        toks = tokenize(u.content)
        v = np.zeros(len(vocab), dtype=np.float32)
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            if t in vidx:
                v[vidx[t]] = (1 + math.log(c)) * (math.log((N + 1) / (df[t] + 1)) + 1.0)
        n = np.linalg.norm(v)
        return v / n if n > 0 else v
    mu = np.stack([vec(u) for u in up])
    md = np.stack([vec(d) for d in ds])
    return md @ mu.T


def gold_uids_for(d, up, gold):
    out = set()
    for ref in gold.get(d.key, []):
        ref_doc, ref_id = ref if isinstance(ref, tuple) else (None, ref)
        for u in up:
            if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u):
                out.add(u.uid)
    return out


for name, (ds, up, gold) in pls.items():
    print()
    print('=' * 100)
    print(f'流水线 {name}  (权重 sem/con/idf/tf = {BEST_W[name]})')
    print('=' * 100)
    build_idf(up + ds)
    children = build_containment(up)
    w_sem, w_con, w_idf, w_tf = BEST_W[name]

    n_ds, n_up = len(ds), len(up)
    sem = np.zeros((n_ds, n_up)); con = np.zeros((n_ds, n_up)); idfR = np.zeros((n_ds, n_up))
    for i, d in enumerate(ds):
        sem[i] = [ch_sem_asym(d, u)['score'] for u in up]
        con[i] = [ch_contain(d, u) for u in up]
        idfR[i] = [idf_asym(d, u, 'R') for u in up]
    tfidf_sim = build_tfidf_sim(ds, up)
    sec_mask = np.ones(n_up)
    for j, u in enumerate(up):
        if u.kind == 'section' and children.get(u.uid):
            sec_mask[j] = 0.85

    scores = (w_sem * sem + w_con * con + w_idf * idfR + w_tf * tfidf_sim) * sec_mask

    def fold_top(d_idx, sc):
        order = sorted(sc.items(), key=lambda kv: -kv[1])
        if order:
            top_uid, top_s = order[0]
            top_u = next(x for x in up if x.uid == top_uid)
            if top_u.kind == 'section':
                kids = children.get(top_uid, [])
                passing = [(kk, sc[kk]) for kk in kids if sc.get(kk, 0) >= top_s - 0.05]
                if len(passing) == 1:
                    kk, ss = passing[0]
                    order = [(kk, ss)] + [(a, b) for a, b in order if a != kk]
        return order

    # 工程级裁决: 对每个下游条目输出候选关系集
    def engineer_decide(theta, margin, max_rel):
        """返回: 每条目 -> {main, secondaries, untraced, ambiguous}"""
        decisions = {}
        for i, d in enumerate(ds):
            sc = {up[j].uid: float(scores[i, j]) for j in range(n_up)}
            order = fold_top(i, sc)
            if not order:
                decisions[d.uid] = {'main': None, 'rels': [], 'untraced': True, 'ambiguous': False}
                continue
            main_uid, main_s = order[0]
            if main_s < theta:
                decisions[d.uid] = {'main': None, 'rels': [], 'untraced': True, 'ambiguous': False,
                                    'top3': order[:3]}
                continue
            rels = [(main_uid, main_s)]
            for uid, s in order[1:]:
                if len(rels) >= max_rel:
                    break
                if s >= max(theta, 0.85 * main_s):
                    # 与main无包含关系才作次级
                    rels.append((uid, s))
            ambiguous = False
            if len(order) >= 2:
                second_s = order[1][1]
                if main_s - second_s < margin:
                    ambiguous = True
            decisions[d.uid] = {'main': main_uid, 'rels': rels, 'untraced': False,
                                'ambiguous': ambiguous}
        return decisions

    # 指标计算
    def metrics(decisions):
        # 关系级: gold关系是否被输出候选覆盖 (召回); 输出关系中gold占比 (精确率)
        gold_total = 0; gold_hit = 0
        out_total = 0; out_correct = 0
        untraced_false = 0  # 有gold却判UNTRACED
        untraced_total = 0
        amb_total = 0
        n_items_with_gold = 0
        for i, d in enumerate(ds):
            guids = gold_uids_for(d, up, gold)
            dec = decisions[d.uid]
            if guids:
                n_items_with_gold += 1
            # gold关系召回
            for g in guids:
                gold_total += 1
                if any(uid == g for uid, _ in dec['rels']):
                    gold_hit += 1
            # 输出关系精确率
            for uid, _ in dec['rels']:
                out_total += 1
                if uid in guids:
                    out_correct += 1
            # UNTRACED
            if dec['untraced']:
                untraced_total += 1
                if guids:
                    untraced_false += 1
            if dec.get('ambiguous'):
                amb_total += 1
        recall = gold_hit / gold_total if gold_total else 0
        precision = out_correct / out_total if out_total else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
        return {
            'recall': recall, 'precision': precision, 'f1': f1,
            'gold_hit': gold_hit, 'gold_total': gold_total,
            'out_total': out_total, 'out_correct': out_correct,
            'untraced_total': untraced_total, 'untraced_false': untraced_false,
            'ambiguous': amb_total, 'n_items': len(ds),
            'n_items_with_gold': n_items_with_gold,
        }

    print(f'{"theta":>6} {"margin":>6} {"maxrel":>6} | {"精确率":>7} {"召回率":>7} {"F1":>6} | '
          f'{"输出关系":>8} {"UNTRACED":>9} {"UNTRACED误报":>11} {"ambiguous":>9}')
    for theta in (0.40, 0.45, 0.50, 0.55):
        for margin in (0.05,):
            for max_rel in (3,):
                dec = engineer_decide(theta, margin, max_rel)
                m = metrics(dec)
                print(f'{theta:>6.2f} {margin:>6.2f} {max_rel:>6} | '
                      f'{m["precision"]:>7.1%} {m["recall"]:>7.1%} {m["f1"]:>6.3f} | '
                      f'{m["out_total"]:>8} {m["untraced_total"]:>9} {m["untraced_false"]:>11} {m["ambiguous"]:>9}')

    # 详细: theta=0.50 的逐条目决策
    print(f'--- theta=0.50 逐条目决策 (★=main命中gold) ---')
    dec = engineer_decide(0.50, 0.05, 3)
    for i, d in enumerate(ds):
        guids = gold_uids_for(d, up, gold)
        if not guids and d.key in gold:
            continue
        dd = dec[d.uid]
        if dd['untraced']:
            tag = 'UNTRACED'
            top3 = dd.get('top3', [])
            t3s = ' '.join(f'{next(u.key for u in up if u.uid==uid)[:20]}({s:.2f})' for uid, s in top3)
            print(f'  {d.key:24s} {tag:9s} gold={[next((u.key for u in up if u.uid==g),"?") for g in guids]} top3:{t3s}')
            continue
        main_key = next(u.key for u in up if u.uid == dd['main'])
        hit = '★' if dd['main'] in guids else ' '
        amb = 'AMB' if dd['ambiguous'] else '   '
        secs = ' '.join(f'{next(u.key for u in up if u.uid==uid)[:18]}({s:.2f})' for uid, s in dd['rels'][1:])
        gkeys = [next((u.key for u in up if u.uid == g), '?') for g in guids]
        print(f'  {d.key:24s} {hit}main={main_key[:22]:22s}({dd["rels"][0][1]:.2f}) {amb} 次级:[{secs}] gold={gkeys}')

print(f'\n总耗时 {time.time()-t0:.1f}s')
