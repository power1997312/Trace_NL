# -*- coding: utf-8 -*-
"""诊断: B1失败对的句子级相似度矩阵 (临时, 用后即删)
判断: 是聚合调参问题(有信号没抓到) 还是 嵌入层信号缺失(需术语通道)
"""
from __future__ import annotations
import os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import build_pipelines, _IDX, ref_matches_unit, Unit

pls = build_pipelines()
ds, up, gold = pls['B1']
for name2, (d2, u2, g2) in pls.items():
    _IDX.add_units(d2)
    _IDX.add_units(u2)

PAIRS = [
    ('<FZSDCS34-OTS006>', '<DCS-SyRS006>'),
    ('<FZSDCS34-OTS016>', '<DCS-SyRS004>'),
    ('<FZSDCS34-OTS018>', '<DCS-SyRS010>'),
    ('<FZSDCS34-OTS020>', '<DCS-SyRS003>'),
    ('<FZSDCS34-OTS053>', '<DCS-SyRS006>'),
    ('<FZSDCS34-OTS054>', '<DCS-SyRS006>'),
    ('<FZSDCS34-OTS056>', '<DCS-SyRS006>'),
    ('<FZSDCS34-OTS008>', '<DCS-SyRS010>'),
]

ds_by_key = {d.key: d for d in ds}
up_by_key = {u.key: u for u in up}

for dk, uk in PAIRS:
    d = ds_by_key.get(dk)
    u = up_by_key.get(uk)
    if not d or not u:
        print(f'{dk} / {uk}: 单元缺失')
        continue
    S = _IDX.sent_matrix(d, u)
    s_d = _IDX.sents[d.uid]
    s_u = _IDX.sents[u.uid]
    print('=' * 100)
    print(f'{dk}({len(d.content)}字) x {uk}({len(u.content)}字)  整条余弦={float(_IDX.unit_vec[d.uid] @ _IDX.unit_vec[u.uid]):.3f}')
    # 每个下游句子的最佳落点
    for i in range(len(s_d)):
        j = int(np.argmax(S[i]))
        print(f'  D[{S[i][j]:.2f}] {s_d[i][:48]}')
        print(f'       U {s_u[j][:48]}')
    print(f'  peak={S.max():.3f}  下游句均最佳={S.max(axis=1).mean():.3f}')
    print()
