# -*- coding: utf-8 -*-
"""实验5: TF-IDF稀疏余弦通道 + 并集召回 + 权重网格搜索 (临时, 用后即删)
目标: 攻克B1"架构伞"关系(设计细化总体架构, 仅共享结构术语)
"""
from __future__ import annotations
import os, sys, time, math
import numpy as np
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import (
    build_pipelines, _IDX, build_idf, _IDF_CACHE,
    ch_sem_asym, ch_contain,
    evaluate, print_eval, Unit, ref_matches_unit, tokenize,
    build_containment, idf_asym, base_score,
)

t0 = time.time()
pls = build_pipelines()
for name, (ds, up, gold) in pls.items():
    _IDX.add_units(ds)
    _IDX.add_units(up)
print(f'编码完成 ({time.time()-t0:.1f}s)')


# ---------- TF-IDF 稀疏向量通道 ----------
def build_tfidf(units: list[Unit]):
    """对单元池构建 TF-IDF 向量(汉字2-gram + ASCII技术词)"""
    df, N = _IDF_CACHE['df'], _IDF_CACHE['N']
    vocab = sorted(df.keys())
    vidx = {t: i for i, t in enumerate(vocab)}
    vecs = np.zeros((len(units), len(vocab)), dtype=np.float32)
    for r, u in enumerate(units):
        toks = tokenize(u.content)
        if not toks:
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t, c in tf.items():
            if t in vidx:
                idf = math.log((N + 1) / (df[t] + 1)) + 1.0
                vecs[r, vidx[t]] = (1 + math.log(c)) * idf
        n = np.linalg.norm(vecs[r])
        if n > 0:
            vecs[r] /= n
    return vecs


def gold_rank(d, up, gold, sc):
    """返回gold单元在sc排序中的最小排名(不在候选中返回len)"""
    order = sorted(sc.items(), key=lambda kv: -kv[1])
    uid_rank = {uid: r for r, (uid, _) in enumerate(order)}
    best = len(order)
    for ref in gold[d.key]:
        ref_doc, ref_id = ref if isinstance(ref, tuple) else (None, ref)
        for u in up:
            if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u) and u.uid in uid_rank:
                best = min(best, uid_rank[u.uid])
    return best


for name, (ds, up, gold) in pls.items():
    print()
    print('=' * 100)
    print(f'流水线 {name}')
    print('=' * 100)
    build_idf(up + ds)
    children = build_containment(up)
    tfidf = build_tfidf(up + ds)
    n_ds, n_up = len(ds), len(up)
    # 单元索引映射: up+ds 顺序 => tfidf行
    tfidf_up = tfidf[n_ds:n_ds + n_up]
    tfidf_ds = tfidf[:n_ds]
    tfidf_sim = tfidf_ds @ tfidf_up.T  # (n_ds, n_up)

    # 各通道分数矩阵
    sem = np.zeros((n_ds, n_up))
    con = np.zeros((n_ds, n_up))
    idfR = np.zeros((n_ds, n_up))
    for i, d in enumerate(ds):
        sa = [ch_sem_asym(d, u)['score'] for u in up]
        sem[i] = sa
        con[i] = [ch_contain(d, u) for u in up]
        idfR[i] = [idf_asym(d, u, 'R') for u in up]

    # 章节降权掩码
    sec_mask = np.ones(n_up)
    for j, u in enumerate(up):
        if u.kind == 'section' and children.get(u.uid):
            sec_mask[j] = 0.85

    def recall_at(scores, K):
        hits = 0; tot = 0
        for i, d in enumerate(ds):
            if d.key not in gold:
                continue
            sc = {up[j].uid: float(scores[i, j]) for j in range(n_up)}
            r = gold_rank(d, up, gold, sc)
            tot += len(gold[d.key])
            # 每个gold关系单独计
            for ref in gold[d.key]:
                ref_doc, ref_id = ref if isinstance(ref, tuple) else (None, ref)
                for u in up:
                    if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u):
                        sc2 = {up[j].uid: float(scores[i, j]) for j in range(n_up)}
                        order = sorted(sc2.items(), key=lambda kv: -kv[1])
                        uid_rank = {uid: r for r, (uid, _) in enumerate(order)}
                        if uid_rank.get(u.uid, 999) < K:
                            hits += 1
                        break
        return hits, tot

    print('--- 召回率 recall@K (gold关系进入top-K的比例) ---')
    for label, scores in [('sem_asym', sem), ('contain', con), ('idfR', idfR), ('tfidf', tfidf_sim)]:
        line = f'  {label:10s}'
        for K in (5, 8, 10, 15):
            h, tot = recall_at(scores * sec_mask, K)
            line += f' | R@{K}={h}/{tot}({h/tot:.0%})'
        print(line)

    # 并集召回: 每通道取top-K的并集
    def union_recall(K):
        hits = 0; tot = 0
        for i, d in enumerate(ds):
            if d.key not in gold:
                continue
            cand = set()
            for scores in (sem, con, idfR, tfidf_sim):
                s = scores[i] * sec_mask
                top = np.argsort(-s)[:K]
                cand |= set(int(x) for x in top)
            for ref in gold[d.key]:
                tot += 1
                ref_doc, ref_id = ref if isinstance(ref, tuple) else (None, ref)
                ok = False
                for j, u in enumerate(up):
                    if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u) and j in cand:
                        ok = True; break
                hits += ok
        return hits, tot

    for K in (5, 8, 10):
        h, tot = union_recall(K)
        print(f'  并集(4通道) R@{K}={h}/{tot}({h/tot:.0%})')

    # 权重网格搜索 (4通道: sem, con, idfR, tfidf)
    print('--- 权重网格搜索 (top1/top3/top5, 含折叠) ---')
    best = None
    weights_grid = []
    for w_sem in (0.3, 0.4, 0.5):
        for w_con in (0.1, 0.2):
            for w_idf in (0.2, 0.3):
                w_tf = round(1 - w_sem - w_con - w_idf, 2)
                if w_tf < 0.05:
                    continue
                weights_grid.append((w_sem, w_con, w_idf, w_tf))

    def eval_weights(w, use_fold=True):
        w_sem, w_con, w_idf, w_tf = w
        scores = w_sem * sem + w_con * con + w_idf * idfR + w_tf * tfidf_sim
        scores = scores * sec_mask
        sc_all = {ds[i].uid: {up[j].uid: float(scores[i, j]) for j in range(n_up)} for i in range(n_ds)}
        if not use_fold:
            return evaluate(sc_all, ds, up, gold)
        # 折叠
        hits = {1: 0, 3: 0, 5: 0}; mrr = 0.0; n_rel = 0; fails = []
        for i, d in enumerate(ds):
            if d.key not in gold:
                continue
            sc = dict(sc_all[d.uid])
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
            uid_rank = {uid: r for r, (uid, _) in enumerate(order)}
            for ref in gold[d.key]:
                n_rel += 1
                ref_doc, ref_id = ref if isinstance(ref, tuple) else (None, ref)
                ranks = [uid_rank[u.uid] for u in up
                         if (ref_doc is None or u.doc == ref_doc)
                         and ref_matches_unit(ref_id, u) and u.uid in uid_rank]
                if not ranks:
                    fails.append((d.key, ref, 'NO_UNIT', -1, [])); continue
                r = min(ranks)
                mrr += 1.0 / (r + 1)
                for k in (1, 3, 5):
                    if r < k:
                        hits[k] += 1
        res = {f'top{k}': (hits[k], n_rel) for k in (1, 3, 5)}
        res['mrr'] = mrr / n_rel if n_rel else 0.0
        res['n_rel'] = n_rel
        res['fails'] = fails
        return res

    results = []
    for w in weights_grid:
        res = eval_weights(w)
        t1 = res['top1'][0]; t3 = res['top3'][0]; t5 = res['top5'][0]
        results.append((t1 * 3 + t3 * 2 + t5, t1, t3, t5, res['mrr'], w))
    results.sort(reverse=True)
    for score, t1, t3, t5, mrr, w in results[:6]:
        print(f'  w(sem={w[0]},con={w[1]},idf={w[2]},tf={w[3]}) top1={t1} top3={t3} top5={t5} MRR={mrr:.3f}')
    # 最佳配置详情
    _, _, _, _, _, bw = results[0]
    res = eval_weights(bw)
    print_eval(f'最佳 w={bw}', res, show_fails=True)

print(f'\n总耗时 {time.time()-t0:.1f}s')
