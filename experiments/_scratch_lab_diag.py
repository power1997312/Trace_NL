# -*- coding: utf-8 -*-
"""诊断: 打印B1失败案例的下游/上游gold内容, 分析失败原因 (临时)"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_lab_common import build_pipelines, GOLD_ZT, ref_matches_unit

pls = build_pipelines()
ds, up, gold = pls['B1']
up_by_key = {u.key: u for u in up}

FOCUS = ['<FZSDCS34-OTS006>', '<FZSDCS34-OTS016>', '<FZSDCS34-OTS018>',
         '<FZSDCS34-OTS053>', '<FZSDCS34-OTS054>', '<FZSDCS34-OTS056>']

for d in ds:
    if d.key not in FOCUS:
        continue
    print('=' * 90)
    print(f'下游 {d.key} ({len(d.content)}字):')
    print(f'  {d.content[:280]}')
    for ref in gold[d.key]:
        gu = next((u for u in up if ref_matches_unit(ref, u)), None)
        if gu:
            print(f'  gold -> {gu.key} ({len(gu.content)}字):')
            print(f'    {gu.content[:280]}')
    print()
