# 临时探针实验: 不依赖追踪表, 纯内容候选发现 (验证方案可行性, 用后即删)
import re
import numpy as np

from core.pdf_parser_adapter import extract_full_text
from core.requirement_extractor import (
    extract_requirement_items, extract_sections, detect_document_type,
)
from models.embedding_model import encode, cosine_similarity_matrix
from core.text_matcher import _char_bigram_jaccard, _normalize_for_char_compare

# ---------- 1. 条目化 ----------
DS_PDF = r'系统需求/DCS需求说明书.pdf'
UP_PDFS = {
    'RPS系统需求规范书': r'用户需求/RPS系统需求规范书.pdf',
    'DCS设备技术规格书': r'用户需求/DCS设备技术规格书.pdf',
}

ds_text = extract_full_text(DS_PDF)
ds_items = extract_requirement_items(ds_text)
print(f'下游条目: {len(ds_items)} ({detect_document_type(ds_text)})')

up_units = []  # (doc, key, content)
for doc, path in UP_PDFS.items():
    t = extract_full_text(path)
    dt = detect_document_type(t)
    if dt == 'id_based':
        for it in extract_requirement_items(t):
            up_units.append((doc, it.item_id, it.content))
    secs = extract_sections(t)
    for s in secs:
        if len(s.content) >= 30:  # 过滤空壳章节
            up_units.append((doc, f'{s.section_number} {s.section_title}'[:40], s.content))
print(f'上游候选单元: {len(up_units)}')
for d, k, c in up_units:
    print(f'   [{d}] {k}  ({len(c)}字)')

# ---------- 2. 候选打分: 嵌入 + 字符bigram ----------
ds_texts = [it.content for it in ds_items]
up_texts = [c for _, _, c in up_units]
ds_emb = encode(ds_texts)
up_emb = encode(up_texts)
sim = cosine_similarity_matrix(ds_emb, up_emb)

# 基准(Trace_Base逆向矩阵): ds_id -> [(doc, ref)]
BASE = {
    '<DCS-SyRS001>': [('DCS设备技术规格书', '3.2.2.1')],
    '<DCS-SyRS002>': [('DCS设备技术规格书', '3.2.2.2')],
    '<DCS-SyRS003>': [('DCS设备技术规格书', '3.2.2.2')],
    '<DCS-SyRS004>': [('RPS系统需求规范书', '<RPS-SYS-RQ-008>')],
    '<DCS-SyRS005>': [('RPS系统需求规范书', '<RPS-SYS-RQ-008>'), ('RPS系统需求规范书', '<RPS-SYS-RQ-009>')],
    '<DCS-SyRS006>': [('RPS系统需求规范书', '<RPS-SYS-RQ-010>'), ('DCS设备技术规格书', '3.2.1')],
    '<DCS-SyRS007>': [('RPS系统需求规范书', '<RPS-SYS-RQ-012>')],
    '<DCS-SyRS008>': [('RPS系统需求规范书', '<RPS-SYS-RQ-010>')],
    '<DCS-SyRS009>': [('RPS系统需求规范书', '<RPS-SYS-RQ-010>')],
    '<DCS-SyRS010>': [('RPS系统需求规范书', '<RPS-SYS-RQ-011>')],
    '<DCS-SyRS011>': [('RPS系统需求规范书', '<RPS-SYS-RQ-012>')],
    '<DCS-SyRS012>': [('RPS系统需求规范书', '<RPS-SYS-RQ-012>')],
    '<DCS-SyRS013>': [('RPS系统需求规范书', '<RPS-SYS-RQ-012>')],
    '<DCS-SyRS014>': [('RPS系统需求规范书', '<RPS-SYS-RQ-013>')],
}

def hit(key_ref, doc, key):
    ref = key_ref.replace(' ', '')
    k = key.replace(' ', '')
    if ref.startswith('3') or ref.startswith('<') is False and ref[0].isdigit():
        return k.startswith(ref) or ref in k  # 章节号前缀
    core = re.findall(r'([A-Za-z]+?)(\d+)', ref)
    kc = re.findall(r'([A-Za-z]+?)(\d+)', k)
    if core and kc:
        return f'{core[-1][0].lower()}{int(core[-1][1])}' == f'{kc[-1][0].lower()}{int(kc[-1][1])}'
    return ref in k

top1 = top3 = total = 0
print('\n========== 无表候选发现结果(每条下游条目 top3) ==========')
for i, it in enumerate(ds_items):
    # 混合分: 嵌入为主, 字符bigram加权
    lex = np.array([_char_bigram_jaccard(_normalize_for_char_compare(it.content), _normalize_for_char_compare(u)) for u in up_texts])
    hybrid = 0.75 * sim[i] + 0.25 * lex
    order = np.argsort(-hybrid)[:3]
    gold = BASE.get(it.item_id, [])
    gold_keys = {(d, r) for d, r in gold}
    got = [(up_units[j][0], up_units[j][1]) for j in order]
    # 命中判定: top1 / top3
    def _h(g):
        for d, k in g:
            for gd, gr in gold_keys:
                if d == gd and hit(gr, d, k):
                    return True
        return False
    t1 = _h(got[:1]); t3 = _h(got[:3])
    top1 += t1; top3 += t3; total += 1
    golds = ', '.join(f'{d}:{r}' for d, r in gold) or '(无)'
    marks = ' '.join(f'{up_units[j][1]}({hybrid[j]:.2f}|e{sim[i][j]:.2f})' for j in order)
    print(f'{it.item_id}: top1={"✓" if t1 else "✗"} top3={"✓" if t3 else "✗"} | gold={golds}')
    print(f'    cand: {marks}')

print(f'\n===== top-1 命中率: {top1}/{total} = {top1/total:.0%} | top-3 命中率: {top3}/{total} = {top3/total:.0%} =====')
