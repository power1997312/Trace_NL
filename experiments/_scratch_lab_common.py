# -*- coding: utf-8 -*-
"""
候选追踪发现实验框架 (临时, 用后即删)
- 从真实PDF提取上下游单元池
- 内置3条流水线共47条金标关系(从基准Excel人工标色数据提取)
- 实现多通道打分与评估指标
"""
from __future__ import annotations
import os, re, sys, pickle, math
from dataclasses import dataclass, field
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.pdf_parser_adapter import extract_full_text
from core.requirement_extractor import (
    extract_requirement_items, extract_sections, detect_document_type,
)
from models.embedding_model import encode, cosine_similarity_matrix
from core.text_matcher import _char_bigram_jaccard, _normalize_for_char_compare

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_scratch_lab_cache.pkl')

# ============================================================
# 金标数据 (从基准数据/*.xlsx 富文本标色提取)
# ============================================================
# 流水线A: 系统需求(DCS需求说明书) -> 用户需求(2份)
GOLD_A = {
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
# 流水线B1: 系统设计(总体方案) -> 系统需求(DCS需求说明书)
GOLD_ZT = {
    '<FZSDCS34-OTS001>': ['<FZSDCS34-SyRS004>'],
    '<FZSDCS34-OTS002>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-OTS004>': ['<FZSDCS34-SyRS006>'],
    '<FZSDCS34-OTS006>': ['<FZSDCS34-SyRS006>'],
    '<FZSDCS34-OTS008>': ['<FZSDCS34-SyRS010>', '<FZSDCS34-SyRS011>'],
    '<FZSDCS34-OTS009>': ['<FZSDCS34-SyRS009>'],
    '<FZSDCS34-OTS010>': ['<FZSDCS34-SyRS010>'],
    '<FZSDCS34-OTS011>': ['<FZSDCS34-SyRS003>'],
    '<FZSDCS34-OTS012>': ['<FZSDCS34-SyRS012>'],
    '<FZSDCS34-OTS016>': ['<FZSDCS34-SyRS004>'],
    '<FZSDCS34-OTS017>': ['<FZSDCS34-SyRS014>'],
    '<FZSDCS34-OTS018>': ['<FZSDCS34-SyRS010>'],
    '<FZSDCS34-OTS019>': ['<FZSDCS34-SyRS010>'],
    '<FZSDCS34-OTS020>': ['<FZSDCS34-SyRS003>'],
    '<FZSDCS34-OTS026>': ['<FZSDCS34-SyRS003>', '<FZSDCS34-SyRS004>'],
    '<FZSDCS34-OTS053>': ['<FZSDCS34-SyRS006>'],
    '<FZSDCS34-OTS054>': ['<FZSDCS34-SyRS006>'],
    '<FZSDCS34-OTS056>': ['<FZSDCS34-SyRS006>'],
}
# 流水线B2: 系统设计(仪控报警) -> 系统需求 (seq2行B列空, 归入ICADS001)
GOLD_ICADS = {
    '<FZSDCS34-ICADS001>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS002>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS004>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS005>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS006>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS007>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS008>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS009>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS011>': ['<FZSDCS34-SyRS005>'],
    '<FZSDCS34-ICADS012>': ['<FZSDCS34-SyRS005>'],
}


@dataclass
class Unit:
    doc: str
    kind: str          # item | section
    key: str
    content: str
    order: int = 0

    @property
    def uid(self):
        return f'{self.doc}#{self.key}'


def id_core(raw_id: str) -> str:
    s = raw_id.strip().strip('<>').replace(' ', '').replace('\n', '')
    groups = re.findall(r'([A-Za-z]+?)(\d+)', s)
    if not groups:
        return s.lower()
    alpha, num = groups[-1]
    return f'{alpha.lower()}{int(num)}'


def ref_matches_unit(ref: str, unit: Unit) -> bool:
    """金标引用 是否匹配 候选单元"""
    ref = ref.strip()
    if unit.kind == 'section':
        sec_no = unit.key.split(' ')[0]
        return sec_no.startswith(ref) or ref.startswith(sec_no)
    return id_core(ref) == id_core(unit.key)


# ============================================================
# 单元池构建
# ============================================================
def build_pool(pdf: str, doc_name: str, kinds: str) -> list[Unit]:
    """kinds: 'items' | 'sections' | 'both'"""
    text = extract_full_text(pdf)
    units = []
    if kinds in ('items', 'both'):
        for i, it in enumerate(extract_requirement_items(text)):
            if len(it.content.strip()) >= 8:
                units.append(Unit(doc_name, 'item', it.item_id, it.content, i))
    if kinds in ('sections', 'both'):
        for i, s in enumerate(extract_sections(text)):
            if len(s.content.strip()) >= 30:
                units.append(Unit(doc_name, 'section', f'{s.section_number} {s.section_title}'.strip()[:60], s.content, 1000 + i))
    return units


def build_pipelines():
    """返回3条流水线的 (下游单元列表, 上游单元列表, 金标dict)"""
    P = os.path.dirname(os.path.abspath(__file__))
    pls = {}
    # A: 系统需求 -> 用户需求
    ds_a = build_pool(os.path.join(P, '系统需求', 'DCS需求说明书.pdf'), 'DCS需求说明书', 'items')
    up_a = (build_pool(os.path.join(P, '用户需求', 'RPS系统需求规范书.pdf'), 'RPS系统需求规范书', 'both')
            + build_pool(os.path.join(P, '用户需求', 'DCS设备技术规格书.pdf'), 'DCS设备技术规格书', 'both'))
    pls['A'] = (ds_a, up_a, GOLD_A)
    # B1: 总体方案 -> 系统需求
    ds_zt = build_pool(os.path.join(P, '系统设计', '总体方案.pdf'), '总体方案', 'items')
    up_sr = build_pool(os.path.join(P, '系统需求', 'DCS需求说明书.pdf'), 'DCS需求说明书', 'both')
    pls['B1'] = (ds_zt, up_sr, GOLD_ZT)
    # B2: 仪控报警 -> 系统需求
    ds_ic = build_pool(os.path.join(P, '系统设计', '仪控报警.pdf'), '仪控报警', 'items')
    pls['B2'] = (ds_ic, up_sr, GOLD_ICADS)
    return pls


# ============================================================
# 文本处理: 句子切分 / token化
# ============================================================
_SENT_SPLIT = re.compile(r'[。；;！!？?\n]+')

def split_sents(text: str, min_len: int = 4) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(text):
        s = s.strip()
        if len(s) >= min_len:
            out.append(s)
    return out


_ASCII_TOK = re.compile(r'[A-Za-z][A-Za-z0-9\-/\.]{1,}')
_CJK = re.compile(r'[\u4e00-\u9fff]+')

def norm_for_tokens(text: str) -> str:
    t = _normalize_for_char_compare(text)
    return t.upper()

def tokenize(text: str) -> list[str]:
    """ASCII技术词 + 汉字2-gram"""
    t = norm_for_tokens(text)
    toks = _ASCII_TOK.findall(t)
    for run in _CJK.findall(t):
        toks += [run[i:i+2] for i in range(len(run) - 1)]
    return toks


# ============================================================
# 向量缓存 (单元级 + 句子级)
# ============================================================
class EmbIndex:
    def __init__(self):
        self.unit_vec = {}     # uid -> vec (长文分段加权均值)
        self.sent_vec = {}     # (uid, si) -> vec
        self.sents = {}        # uid -> [sent]

    def add_units(self, units: list[Unit]):
        texts, short_uids = [], []
        sent_texts, sent_keys = [], []
        for u in units:
            ss = split_sents(u.content)
            self.sents[u.uid] = ss
            if len(u.content) <= 300 or not ss:
                texts.append(u.content); short_uids.append(u.uid)
            else:
                for si, s in enumerate(ss):
                    sent_texts.append(s); sent_keys.append((u.uid, si))
        if texts:
            vecs = encode(texts)
            for uid, v in zip(short_uids, vecs):
                self.unit_vec[uid] = v
        if sent_texts:
            svecs = encode(sent_texts)
            by_unit = {}
            for (uid, si), v in zip(sent_keys, svecs):
                self.sent_vec[(uid, si)] = v
                by_unit.setdefault(uid, []).append((si, v))
            # 合成长单元向量(长度加权)
            for uid, lst in by_unit.items():
                u = next(x for x in units if x.uid == uid)
                ss = self.sents[uid]
                w = np.array([max(len(ss[si]), 1) for si, _ in lst], dtype=np.float32)
                mat = np.stack([v for _, v in lst])
                vec = (mat * w[:, None]).sum(0) / w.sum()
                vec = vec / (np.linalg.norm(vec) + 1e-9)
                self.unit_vec[uid] = vec
        # 所有单元的句子向量也补齐(短单元同样需要句级)
        miss_texts, miss_keys = [], []
        for u in units:
            for si, s in enumerate(self.sents[u.uid]):
                if (u.uid, si) not in self.sent_vec:
                    miss_texts.append(s); miss_keys.append((u.uid, si))
        if miss_texts:
            mvecs = encode(miss_texts)
            for k, v in zip(miss_keys, mvecs):
                self.sent_vec[k] = v

    def sent_matrix(self, u1: Unit, u2: Unit) -> np.ndarray:
        """返回 u1句子 x u2句子 余弦矩阵"""
        s1, s2 = self.sents[u1.uid], self.sents[u2.uid]
        if not s1 or not s2:
            return np.zeros((max(len(s1), 1), max(len(s2), 1)))
        m1 = np.stack([self.sent_vec[(u1.uid, i)] for i in range(len(s1))])
        m2 = np.stack([self.sent_vec[(u2.uid, i)] for i in range(len(s2))])
        return m1 @ m2.T


_IDX = EmbIndex()

# ============================================================
# 通道打分
# ============================================================
def ch_emb_whole(d: Unit, u: Unit) -> float:
    return float(_IDX.unit_vec[d.uid] @ _IDX.unit_vec[u.uid])


def ch_sem_asym(d: Unit, u: Unit, tau: float = 0.60) -> dict:
    """不对称句级聚合: 返回特征字典"""
    S = _IDX.sent_matrix(d, u)
    peak = float(S.max()) if S.size else 0.0
    # 短侧覆盖率(方向无关)
    if len(d.content) <= len(u.content):
        short, Ss = d, S
    else:
        short, Ss = u, S.T
    b = Ss.max(axis=1) if Ss.size else np.array([])
    ss = _IDX.sents[short.uid]
    if len(b) and ss:
        lens = np.array([len(s) for s in ss], dtype=np.float32)
        cover = float((lens[b >= tau]).sum() / max(lens.sum(), 1e-9))
        mean_best = float((lens * b).sum() / max(lens.sum(), 1e-9))
    else:
        cover = mean_best = 0.0
    whole = ch_emb_whole(d, u)
    score = max(whole, 0.4 * peak + 0.6 * cover)
    return {'score': score, 'whole': whole, 'peak': peak, 'cover': cover, 'mean_best': mean_best}


def ch_bigram(d: Unit, u: Unit) -> float:
    return _char_bigram_jaccard(_normalize_for_char_compare(d.content),
                                _normalize_for_char_compare(u.content))


def ch_contain(d: Unit, u: Unit) -> float:
    """短侧bigram被长侧包含的比例"""
    a, b = _normalize_for_char_compare(d.content), _normalize_for_char_compare(u.content)
    if not a or not b:
        return 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    bg_s = {short[i:i+2] for i in range(len(short)-1)}
    bg_l = {long_[i:i+2] for i in range(len(long_)-1)}
    if not bg_s:
        return 0.0
    return len(bg_s & bg_l) / len(bg_s)


_IDF_CACHE = {}

def build_idf(units: list[Unit]):
    """对候选池统计df"""
    df = {}
    for u in units:
        for t in set(tokenize(u.content)):
            df[t] = df.get(t, 0) + 1
    N = len(units)
    _IDF_CACHE['df'] = df
    _IDF_CACHE['N'] = N
    _IDF_CACHE['rare_max'] = max(2, N // 10)


def rare_tokens(u: Unit) -> dict:
    df, rmax = _IDF_CACHE['df'], _IDF_CACHE['rare_max']
    N = _IDF_CACHE['N']
    out = {}
    for t in set(tokenize(u.content)):
        if df.get(t, 0) <= rmax:
            out[t] = math.log(N / df[t]) + 1.0
    return out


def ch_idf(d: Unit, u: Unit) -> dict:
    """IDF加权稀有词 双向落点率 -> F1"""
    rd, ru = rare_tokens(d), rare_tokens(u)
    td, tu = set(tokenize(d.content)), set(tokenize(u.content))
    def _pr(rare_a, tok_b):
        if not rare_a:
            return 0.0
        w_sum = sum(rare_a.values())
        hit = sum(w for t, w in rare_a.items() if t in tok_b)
        return hit / w_sum
    P = _pr(rd, tu)   # 下游稀有词在上游的落点率
    R = _pr(ru, td)   # 上游稀有词被下游命中率
    f1 = 2 * P * R / (P + R) if (P + R) > 0 else 0.0
    return {'f1': f1, 'P': P, 'R': R}


def ch_label(d: Unit, u: Unit) -> float:
    """稀有ASCII技术标签的Jaccard(强判别信号)"""
    df, rmax = _IDF_CACHE['df'], _IDF_CACHE['rare_max']
    def labels(x):
        return {t for t in _ASCII_TOK.findall(norm_for_tokens(x)) if df.get(t, 0) <= rmax}
    ld, lu = labels(d.content), labels(u.content)
    if not ld and not lu:
        return 0.0
    return len(ld & lu) / len(ld | lu) if (ld | lu) else 0.0


# ============================================================
# 共享: 包含图 / 非对称IDF / 基础融合分 (exp2/exp4 复用)
# ============================================================
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
    if mode == 'f2':
        return 5 * P * R / (4 * P + R) if (P + R) > 0 else 0.0
    return 2 * P * R / (P + R) if (P + R) > 0 else 0.0


def base_score(d: Unit, u: Unit, asym_mode: str, w_sem=0.5, w_con=0.2, w_idf=0.3) -> float:
    return (w_sem * ch_sem_asym(d, u)['score']
            + w_con * ch_contain(d, u)
            + w_idf * idf_asym(d, u, asym_mode))


# ============================================================
# 评估
# ============================================================
def evaluate(scores: dict, ds_units: list[Unit], up_units: list[Unit], gold: dict, topk=(1, 3, 5), verbose=False):
    """scores: {ds_uid: {up_uid: score}}; 返回 topk命中率 + MRR + 失败明细"""
    hits = {k: 0 for k in topk}
    mrr = 0.0
    n_rel = 0
    fails = []
    for d in ds_units:
        if d.key not in gold:
            continue
        sc = scores.get(d.uid, {})
        if not sc:
            continue
        order = sorted(sc.items(), key=lambda kv: -kv[1])
        uid_rank = {uid: r for r, (uid, _) in enumerate(order)}
        for ref in gold[d.key]:
            n_rel += 1
            # 金标引用可能是 (doc, ref) 元组(流水线A) 或 ref 字符串(流水线B)
            if isinstance(ref, tuple):
                ref_doc, ref_id = ref
            else:
                ref_doc, ref_id = None, ref
            # 找到gold对应单元的排名(取最靠前的)
            ranks = [uid_rank[u.uid] for u in up_units
                     if (ref_doc is None or u.doc == ref_doc)
                     and ref_matches_unit(ref_id, u) and u.uid in uid_rank]
            if not ranks:
                fails.append((d.key, ref, 'NO_UNIT', -1, []))
                continue
            r = min(ranks)
            mrr += 1.0 / (r + 1)
            for k in topk:
                if r < k:
                    hits[k] += 1
            if r >= max(topk):
                top3 = [(next(u.key for u in up_units if u.uid == uid), s) for uid, s in order[:3]]
                gold_u = next((u.key for u in up_units
                               if (ref_doc is None or u.doc == ref_doc) and ref_matches_unit(ref_id, u)), '?')
                fails.append((d.key, ref, gold_u, r, top3))
    res = {f'top{k}': (hits[k], n_rel) for k in topk}
    res['mrr'] = mrr / n_rel if n_rel else 0.0
    res['n_rel'] = n_rel
    res['fails'] = fails
    return res


def _gold_doc(ref, up_units):
    return None


def print_eval(name: str, res: dict, show_fails=True):
    line = f'{name:28s} n={res["n_rel"]:2d} MRR={res["mrr"]:.3f}'
    for k in (1, 3, 5):
        h, n = res[f'top{k}']
        line += f' | top{k}={h}/{n}({h/n:.0%})'
    print(line)
    if show_fails and res['fails']:
        for ds, ref, gold_u, r, top3 in res['fails'][:12]:
            t3 = ' '.join(f'{k[:22]}({s:.2f})' for k, s in top3)
            print(f'    MISS {ds} -> {ref} (gold_unit={gold_u}, rank={r}) top3: {t3}')
