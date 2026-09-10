# -*- coding: utf-8 -*-
"""实验1: 各单通道判别力基线 + 融合对比 (临时, 用后即删)"""
from __future__ import annotations
import os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import (
    build_pipelines, _IDX, build_idf,
    ch_emb_whole, ch_sem_asym, ch_bigram, ch_contain, ch_idf, ch_label,
    evaluate, print_eval, Unit,
)

t0 = time.time()
print('>>> 构建流水线单元池...')
pls = build_pipelines()
for name, (ds, up, gold) in pls.items():
    print(f'    [{name}] 下游{len(ds)}单元, 上游{len(up)}单元, 金标{sum(len(v) for v in gold.values())}条')

print('>>> 编码向量...')
for name, (ds, up, gold) in pls.items():
    _IDX.add_units(ds)
    _IDX.add_units(up)
print(f'    完成 ({time.time()-t0:.1f}s)')

CHANNELS = {
    'emb_whole':  lambda d, u: ch_emb_whole(d, u),
    'sem_asym':   lambda d, u: ch_sem_asym(d, u)['score'],
    'bigram':     lambda d, u: ch_bigram(d, u),
    'contain':    lambda d, u: ch_contain(d, u),
    'idf_f1':     lambda d, u: ch_idf(d, u)['f1'],
    'label_jac':  lambda d, u: ch_label(d, u),
}

FUSIONS = {
    'F0 探针复现(0.75emb+0.25bigram)': lambda d, u: 0.75 * ch_emb_whole(d, u) + 0.25 * ch_bigram(d, u),
    'F1 sem_asym主导':   lambda d, u: 0.7 * ch_sem_asym(d, u)['score'] + 0.3 * ch_contain(d, u),
    'F2 三通道':         lambda d, u: 0.5 * ch_sem_asym(d, u)['score'] + 0.25 * ch_contain(d, u) + 0.25 * ch_idf(d, u)['f1'],
    'F3 三通道+标签':    lambda d, u: 0.45 * ch_sem_asym(d, u)['score'] + 0.2 * ch_contain(d, u) + 0.2 * ch_idf(d, u)['f1'] + 0.15 * ch_label(d, u),
}

for name, (ds, up, gold) in pls.items():
    print()
    print('=' * 110)
    print(f'流水线 {name}: 下游={ds[0].doc} 上游池={sorted(set(u.doc for u in up))}')
    print('=' * 110)
    build_idf(up + ds)
    # 单通道
    print('--- 单通道 ---')
    for cname, fn in CHANNELS.items():
        scores = {}
        for d in ds:
            scores[d.uid] = {u.uid: fn(d, u) for u in up}
        res = evaluate(scores, ds, up, gold)
        print_eval(f'{cname}', res, show_fails=(cname == 'emb_whole'))
    # 融合
    print('--- 融合 ---')
    for fname, fn in FUSIONS.items():
        scores = {}
        for d in ds:
            scores[d.uid] = {u.uid: fn(d, u) for u in up}
        res = evaluate(scores, ds, up, gold)
        print_eval(fname, res, show_fails=True)

print(f'\n总耗时 {time.time()-t0:.1f}s')
