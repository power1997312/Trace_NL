# -*- coding: utf-8 -*-
"""实验4(高效版): 单元级NLI方向性精排 (临时, 用后即删)
每候选仅1对(截断512), 只对粗筛top-8计算 => 快速
方向: fwd=(下游,上游) bwd=(上游,下游)
"""
from __future__ import annotations
import os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import (
    build_pipelines, _IDX, build_idf,
    evaluate, print_eval, Unit, ref_matches_unit,
    build_containment, base_score,
)
from models.nli_model import predict_batch

MAX_CHARS = 400  # NLI输入截断(字符), 控制CPU耗时

t0 = time.time()
pls = build_pipelines()
for name, (ds, up, gold) in pls.items():
    _IDX.add_units(ds)
    _IDX.add_units(up)
print(f'编码完成 ({time.time()-t0:.1f}s)')


def trunc(t: str) -> str:
    return t[:MAX_CHARS]


for name, (ds, up, gold) in pls.items():
    print()
    print('=' * 100)
    print(f'流水线 {name}')
    print('=' * 100)
    build_idf(up + ds)
    children = build_containment(up)

    # 粗筛分(三通道+章节降权)
    coarse = {}
    for d in ds:
        sc = {u.uid: base_score(d, u, 'f1') for u in up}
        for u in up:
            if u.kind == 'section' and children.get(u.uid):
                sc[u.uid] *= 0.85
        coarse[d.uid] = sc

    # 单元级NLI: 收集top-8候选对, 双向一次批量
    fwd_pairs, bwd_pairs, pair_keys = [], [], []
    for d in ds:
        order = sorted(coarse[d.uid].items(), key=lambda kv: -kv[1])[:8]
        for uid, _ in order:
            u = next(x for x in up if x.uid == uid)
            fwd_pairs.append((trunc(d.content), trunc(u.content)))
            bwd_pairs.append((trunc(u.content), trunc(d.content)))
            pair_keys.append((d.uid, uid))
    print(f'>>> 单元级NLI {len(fwd_pairs)}对(双向)...')
    fwd_res = predict_batch(fwd_pairs, batch_size=16)
    bwd_res = predict_batch(bwd_pairs, batch_size=16)
    nli_map = {}
    for i, key in enumerate(pair_keys):
        nli_map[key] = {
            'fwd_ent': fwd_res[i]['ENTAILMENT'], 'fwd_contra': fwd_res[i]['CONTRADICTION'],
            'bwd_ent': bwd_res[i]['ENTAILMENT'], 'bwd_contra': bwd_res[i]['CONTRADICTION'],
        }
    print(f'    完成 ({time.time()-t0:.1f}s)')

    def fold_order(d, sc):
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

    def eval_variant(label, w_nli, nli_mode):
        scores = {}
        for d in ds:
            sc = {}
            order = sorted(coarse[d.uid].items(), key=lambda kv: -kv[1])[:8]
            for uid, cs in order:
                nl = nli_map.get((d.uid, uid), {})
                if nli_mode == 'fwd':
                    term = nl.get('fwd_ent', 0) * (1 - nl.get('fwd_contra', 0))
                elif nli_mode == 'bwd':
                    term = nl.get('bwd_ent', 0) * (1 - nl.get('bwd_contra', 0))
                else:
                    term = max(nl.get('fwd_ent', 0) * (1 - nl.get('fwd_contra', 0)),
                               nl.get('bwd_ent', 0) * (1 - nl.get('bwd_contra', 0)))
                sc[uid] = (1 - w_nli) * cs + w_nli * term
            scores[d.uid] = sc
        res = evaluate(scores, ds, up, gold)
        print_eval(label, res, show_fails=True)
        return res

    eval_variant('粗筛基线(无NLI)', 0.0, 'max')
    for mode in ('fwd', 'bwd', 'max'):
        for w in (0.25, 0.4):
            eval_variant(f'+NLI {mode} w={w}', w, mode)

    # 诊断: gold对与强干扰对的NLI特征
    print('--- NLI特征诊断(失败下游) ---')
    shown = 0
    for d in ds:
        if d.key not in gold or shown >= 5:
            continue
        order = sorted(coarse[d.uid].items(), key=lambda kv: -kv[1])[:6]
        gold_uids = set()
        for ref in gold[d.key]:
            ref_doc, ref_id = ref if isinstance(ref, tuple) else (None, ref)
            for u in up:
                if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u):
                    gold_uids.add(u.uid)
        if any(uid in gold_uids for uid, _ in order[:3]):
            continue
        shown += 1
        gkeys = sorted(u.key for u in up if u.uid in gold_uids)
        print(f'  {d.key} gold={gkeys}:')
        for uid, cs in order:
            nl = nli_map.get((d.uid, uid), {})
            mark = '★' if uid in gold_uids else ' '
            u = next(x for x in up if x.uid == uid)
            print(f'    {mark} {u.key[:26]:26s} coarse={cs:.3f} fwd_ent={nl.get("fwd_ent",0):.2f} bwd_ent={nl.get("bwd_ent",0):.2f}')

print(f'\n总耗时 {time.time()-t0:.1f}s')
