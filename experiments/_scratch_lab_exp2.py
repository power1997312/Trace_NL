# -*- coding: utf-8 -*-
"""实验2: 包含图具体性折叠(修复章节污染) + 非对称IDF (临时, 用后即删)"""
from __future__ import annotations
import os, sys, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import (
    build_pipelines, _IDX, build_idf, _IDF_CACHE,
    ch_emb_whole, ch_sem_asym, ch_bigram, ch_contain, ch_idf, ch_label,
    evaluate, print_eval, Unit, tokenize, norm_for_tokens, ref_matches_unit,
)
from core.text_matcher import _char_bigram_jaccard, _normalize_for_char_compare

t0 = time.time()
pls = build_pipelines()
for name, (ds, up, gold) in pls.items():
    _IDX.add_units(ds)
    _IDX.add_units(up)
print(f'编码完成 ({time.time()-t0:.1f}s)')

_ASCII_TOK = __import__('re').compile(r'[A-Za-z][A-Za-z0-9\-/\.]{1,}')


def bigram_set(text: str) -> set:
    t = _normalize_for_char_compare(text)
    return {t[i:i+2] for i in range(len(t)-1)}


def build_containment(up: list[Unit]) -> dict:
    """返回 section_uid -> [item_uid] 包含关系 (条目bigram>=70%落在章节内)"""
    items = [u for u in up if u.kind == 'item']
    secs = [u for u in up if u.kind == 'section']
    bg = {u.uid: bigram_set(u.content) for u in up}
    children = {}
    for s in secs:
        if not bg[s.uid]:
            continue
        kids = []
        for it in items:
            if not bg[it.uid]:
                continue
            ratio = len(bg[it.uid] & bg[s.uid]) / len(bg[it.uid])
            if ratio >= 0.70:
                kids.append(it.uid)
        children[s.uid] = kids
    return children


def idf_asym(d: Unit, u: Unit, mode='R') -> float:
    """非对称IDF: R=上游稀有词被下游命中率(细化匹配方向)"""
    from _scratch_lab_common import rare_tokens
    rd, ru = rare_tokens(d), rare_tokens(u)
    td, tu = set(tokenize(d.content)), set(tokenize(u.content))
    def _pr(rare_a, tok_b):
        if not rare_a:
            return 0.0
        return sum(w for t, w in rare_a.items() if t in tok_b) / sum(rare_a.values())
    P = _pr(rd, tu)
    R = _pr(ru, td)
    if mode == 'R':
        return R
    if mode == 'max':
        return max(P, R)
    if mode == 'f2':  # 偏召回的F-beta
        return 5 * P * R / (4 * P + R) if (P + R) > 0 else 0.0
    return 2 * P * R / (P + R) if (P + R) > 0 else 0.0


def base_score(d: Unit, u: Unit, asym_mode: str, w_sem=0.5, w_con=0.2, w_idf=0.3) -> float:
    return (w_sem * ch_sem_asym(d, u)['score']
            + w_con * ch_contain(d, u)
            + w_idf * idf_asym(d, u, asym_mode))


def decide_with_folding(d: Unit, up: list[Unit], children: dict,
                        scores: dict, delta=0.05, sec_penalty=1.0) -> list:
    """具体性折叠: top为章节且其内条目分数接近 -> 折叠到条目"""
    sc = dict(scores)
    if sec_penalty != 1.0:
        for u in up:
            if u.kind == 'section' and children.get(u.uid):
                sc[u.uid] *= sec_penalty
    order = sorted(sc.items(), key=lambda kv: -kv[1])
    if not order:
        return order
    top_uid, top_s = order[0]
    top_u = next(x for x in up if x.uid == top_uid)
    if top_u.kind == 'section':
        kids = children.get(top_uid, [])
        passing = [(k, sc[k]) for k in kids if sc.get(k, 0) >= top_s - delta]
        if len(passing) == 1:
            # 折叠到该条目
            k, s = passing[0]
            order = [(k, s)] + [(a, b) for a, b in order if a != k]
        # >=2 个条目过线: 章节覆盖广, 保留章节(不折叠)
    return order


def evaluate_with_folding(name, ds, up, gold, children, scores_all, delta=0.05, sec_penalty=1.0):
    hits = {1: 0, 3: 0, 5: 0}
    mrr = 0.0
    n_rel = 0
    fails = []
    for d in ds:
        if d.key not in gold:
            continue
        order = decide_with_folding(d, up, children, scores_all[d.uid], delta, sec_penalty)
        uid_rank = {uid: r for r, (uid, _) in enumerate(order)}
        for ref in gold[d.key]:
            n_rel += 1
            if isinstance(ref, tuple):
                ref_doc, ref_id = ref
            else:
                ref_doc, ref_id = None, ref
            ranks = [uid_rank[u.uid] for u in up
                     if (ref_doc is None or u.doc == ref_doc)
                     and ref_matches_unit(ref_id, u) and u.uid in uid_rank]
            if not ranks:
                fails.append((d.key, ref, 'NO_UNIT', -1, []))
                continue
            r = min(ranks)
            mrr += 1.0 / (r + 1)
            for k in (1, 3, 5):
                if r < k:
                    hits[k] += 1
            if r >= 5:
                top3 = [(next(u.key for u in up if u.uid == uid), s) for uid, s in order[:3]]
                gold_u = next((u.key for u in up
                               if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u)), '?')
                fails.append((d.key, ref, gold_u, r, top3))
    res = {f'top{k}': (hits[k], n_rel) for k in (1, 3, 5)}
    res['mrr'] = mrr / n_rel if n_rel else 0.0
    res['n_rel'] = n_rel
    res['fails'] = fails
    print_eval(name, res, show_fails=True)
    return res


for name, (ds, up, gold) in pls.items():
    if __name__ != '__main__':
        break
    print()
    print('=' * 100)
    print(f'流水线 {name}')
    print('=' * 100)
    build_idf(up + ds)
    children = build_containment(up)
    n_sec_with_kids = sum(1 for s in children.values() if s)
    print(f'包含图: {n_sec_with_kids} 个章节包含条目 (共{sum(len(v) for v in children.values())}条包含边)')

    for asym_mode in ('f1', 'R', 'max'):
        scores_all = {d.uid: {u.uid: base_score(d, u, asym_mode) for u in up} for d in ds}
        # 无折叠基线
        res0 = evaluate(scores_all, ds, up, gold)
        print_eval(f'base idf={asym_mode} (无折叠)', res0, show_fails=False)
        # 折叠
        evaluate_with_folding(f'base idf={asym_mode} +折叠', ds, up, gold, children, scores_all)
        # 折叠+章节降权
        evaluate_with_folding(f'base idf={asym_mode} +折叠+降权0.85', ds, up, gold, children, scores_all, sec_penalty=0.85)

print(f'\n总耗时 {time.time()-t0:.1f}s')
