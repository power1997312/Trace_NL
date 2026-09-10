from __future__ import annotations
"""
候选追踪发现模块

不依赖下游文档中的追踪关系表, 在全量上下游文档之间按内容发现候选追踪关系。

流水线:
    单元化(条目∪章节, 单调骨架去伪标题)
    → 多通道召回(语义/词汇/锚点/TF-IDF, 全部内容派, 编号不作证据)
    → 门控融合(anchor证据强抬升) + 章节降权 + 具体性折叠
    → 裁决(θ_accept + margin ambiguous + 一对多)

实验依据(experiments/_scratch_lab_exp1~7, 47条金标):
    - 4通道并集 top10 召回 = 100% (流水线 A/B1/B2)
    - 锚点通道单通道 B2 top1=80% MRR=0.84, 但线性融合会稀释, 必须门控接入
    - NLI 在发现层有害, 禁用(仅保留在验证层)
    - 包含图具体性折叠 + 章节降权0.85 修复章节污染
    - 非对称IDF取R向(上游稀有词被下游命中率), 识别"细化/覆盖"方向
"""
import math
import re
from dataclasses import dataclass, field

import numpy as np

from core.pdf_parser_adapter import extract_full_text
from core.requirement_extractor import (
    detect_document_type, extract_requirement_items, extract_sections,
)
from core.text_matcher import _normalize_for_char_compare
from models.embedding_model import encode

# ============================================================
# 数据结构
# ============================================================


@dataclass
class Unit:
    """追踪单元: 条目或章节"""
    doc: str
    kind: str          # 'item' | 'section'
    key: str           # 条目ID 或 "章节号 标题"
    content: str
    order: int = 0

    @property
    def uid(self) -> str:
        return f'{self.doc}#{self.key}'


@dataclass
class Candidate:
    """一条候选追踪关系"""
    unit: Unit
    score: float
    evidence: dict = field(default_factory=dict)


@dataclass
class DiscoveryDecision:
    """单个下游单元的发现裁决"""
    ds_unit: Unit
    status: str                                   # 'traced' | 'untraced'
    main: Candidate | None = None
    secondaries: list[Candidate] = field(default_factory=list)
    ambiguous: bool = False
    top_candidates: list[Candidate] = field(default_factory=list)  # UNTRACED时的参考候选


# ============================================================
# 默认参数 (来自实验5/6/7的网格搜索与裁决评估)
# ============================================================
DEFAULT_WEIGHTS = (0.5, 0.2, 0.2, 0.1)   # sem / contain / idf / tfidf
THETA_ACCEPT = 0.45                       # 接受阈值
AMBIGUOUS_MARGIN = 0.05                   # top1-top2 差值小于此值 -> ambiguous
MAX_RELATIONS = 3                         # 一对多上限
SECONDARY_RATIO = 0.85                    # 次级关系阈值: >= max(theta, 0.85*main)
ANCHOR_GATE = 0.30                        # 锚点门控阈值(固定尺度归一化后)
ANCHOR_BOOST = 0.50                       # 锚点门控加成系数
ANCHOR_SCALE = 0.10                       # 锚点固定尺度: 原始分/此值 -> [0,1]
SECTION_PENALTY = 0.85                    # 含条目章节的降权系数
FOLD_DELTA = 0.05                         # 具体性折叠容差
RECALL_K = 15                             # 每通道粗召回保留数
CLASS_MISMATCH_PENALTY = 0.3              # 分级互斥惩罚系数(对象不同, 内容再像也降权)

# 安全分级代码模式: F-SC1 / FSC1 / F-SC 1 等(归一为 F-SCN)
_CLASS_CODE_RE = re.compile(r'F[\-\s]?SC\s*([1-4])', re.IGNORECASE)


def extract_class_codes(text: str) -> set:
    """
    提取安全分级代码(F-SC1~F-SC4), 归一化为标准形, 用于"对象一致性"校验。

    设计依据: 安全分级是对象标识而非内容特征 — F-SC1级与F-SC2级章节的
    通用要求文字高度近似(模板近似), 但适用对象不同, 不构成正确溯源。
    仅提取显式声明的分级; 未声明(空集)不参与互斥判定, 避免误杀。
    """
    if not text:
        return set()
    return {f'F-SC{m}' for m in _CLASS_CODE_RE.findall(text)}


# ============================================================
# 单元化
# ============================================================
def id_core(raw_id: str) -> str:
    """ID核心归一化: <FZSDCS34-SyRS005> -> 'syrs5' (前缀/零填充容错)"""
    s = raw_id.strip().strip('<>').replace(' ', '').replace('\n', '')
    groups = re.findall(r'([A-Za-z]+?)(\d+)', s)
    if not groups:
        return s.lower()
    alpha, num = groups[-1]
    try:
        return f'{alpha.lower()}{int(num)}'
    except ValueError:
        return f'{alpha.lower()}{num}'


def ref_matches_unit(ref: str, unit: Unit) -> bool:
    """引用串(条目ID或章节号)是否指向给定单元"""
    ref = ref.strip()
    if unit.kind == 'section':
        sec_no = unit.key.split(' ')[0]
        ref_no = ref.split(' ')[0]
        return sec_no.startswith(ref_no) or ref_no.startswith(sec_no)
    return id_core(ref) == id_core(unit.key)


def _section_number_tuple(num: str) -> tuple:
    parts = []
    for p in num.rstrip('.').split('.'):
        if p.isdigit():
            parts.append(int(p))
    return tuple(parts)


def _monotonic_skeleton_filter(sections) -> list:
    """
    章节噪声过滤: 按文档序扫描章节号, 保留单调递增骨架。
    非单调的章节号(如正文中突然回跳的编号)多为表格/图注伪标题, 剔除。
    """
    kept = []
    prev = None
    for s in sections:
        num = _section_number_tuple(s.section_number)
        if not num:
            continue
        if prev is None or num > prev:
            kept.append(s)
            prev = num
    return kept


def unitize_text(text: str, doc_name: str, role: str = 'upstream') -> list[Unit]:
    """
    将文档全文单元化

    Args:
        text: 文档全文
        doc_name: 文档名
        role: 'downstream' | 'upstream'
            下游: ID型文档取条目, 章节型文档取章节
            上游: 条目 ∪ 章节(章节须内容>=30字)
    """
    units: list[Unit] = []
    doc_type = detect_document_type(text)

    want_items = role == 'upstream' or doc_type == 'id_based'
    want_sections = role == 'upstream' or doc_type == 'chapter_based'

    if want_items:
        for i, it in enumerate(extract_requirement_items(text)):
            if len(it.content.strip()) >= 8:
                units.append(Unit(doc_name, 'item', it.item_id, it.content, i))

    if want_sections:
        secs = _monotonic_skeleton_filter(extract_sections(text))
        for i, s in enumerate(secs):
            if len(s.content.strip()) >= 30:
                key = f'{s.section_number} {s.section_title}'.strip()[:60]
                units.append(Unit(doc_name, 'section', key, s.content, 1000 + i))

    return units


def unitize_pdf(pdf_path: str, doc_name: str, role: str = 'upstream') -> list[Unit]:
    return unitize_text(extract_full_text(pdf_path), doc_name, role)


# ============================================================
# 文本处理: 句子切分 / token化
# ============================================================
_SENT_SPLIT = re.compile(r'[。；;！!？?\n]+')
_ASCII_TOK = re.compile(r'[A-Za-z][A-Za-z0-9\-/\.]{1,}')
_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')
_LIST_ITEM_RE = re.compile(r'^\s*(?:[-•●]|\d+[)）]|[a-zA-Z][)）])\s*(.+)$')


def split_sents(text: str, min_len: int = 4) -> list[str]:
    out = []
    for s in _SENT_SPLIT.split(text):
        s = s.strip()
        if len(s) >= min_len:
            out.append(s)
    return out


def tokenize(text: str) -> list[str]:
    """ASCII技术词 + 汉字2-gram"""
    t = _normalize_for_char_compare(text).upper()
    toks = _ASCII_TOK.findall(t)
    for run in _CJK_RUN.findall(t):
        toks += [run[i:i + 2] for i in range(len(run) - 1)]
    return toks


def extract_list_items(text: str, min_len: int = 3) -> list[str]:
    """提取列表项(去编号), 普通行也作为锚点候选"""
    items = []
    for line in text.split('\n'):
        m = _LIST_ITEM_RE.match(line)
        frag = m.group(1).strip() if m else line.strip()
        frag = frag.rstrip('；;。')
        if len(frag) >= min_len:
            items.append(frag)
    return items


# ============================================================
# 向量索引 (单元级 + 句子级, 批量编码)
# ============================================================
class EmbIndex:
    def __init__(self):
        self.unit_vec: dict[str, np.ndarray] = {}
        self.sent_vec: dict[tuple[str, int], np.ndarray] = {}
        self.sents: dict[str, list[str]] = {}

    def add_units(self, units: list[Unit]):
        texts, short_uids = [], []
        sent_texts, sent_keys = [], []
        for u in units:
            ss = split_sents(u.content)
            self.sents[u.uid] = ss
            if len(u.content) <= 300 or not ss:
                texts.append(u.content)
                short_uids.append(u.uid)
            else:
                for si, s in enumerate(ss):
                    sent_texts.append(s)
                    sent_keys.append((u.uid, si))
        if texts:
            vecs = encode(texts)
            for uid, v in zip(short_uids, vecs):
                self.unit_vec[uid] = v
        if sent_texts:
            svecs = encode(sent_texts)
            by_unit: dict[str, list] = {}
            for (uid, si), v in zip(sent_keys, svecs):
                self.sent_vec[(uid, si)] = v
                by_unit.setdefault(uid, []).append((si, v))
            # 长单元向量 = 句向量的长度加权和
            for uid, lst in by_unit.items():
                ss = self.sents[uid]
                w = np.array([max(len(ss[si]), 1) for si, _ in lst], dtype=np.float32)
                mat = np.stack([v for _, v in lst])
                vec = (mat * w[:, None]).sum(0) / w.sum()
                self.unit_vec[uid] = vec / (np.linalg.norm(vec) + 1e-9)
        # 补齐短单元缺失的句向量(句级精比需要)
        miss_texts, miss_keys = [], []
        for u in units:
            for si, s in enumerate(self.sents[u.uid]):
                if (u.uid, si) not in self.sent_vec:
                    miss_texts.append(s)
                    miss_keys.append((u.uid, si))
        if miss_texts:
            mvecs = encode(miss_texts)
            for k, v in zip(miss_keys, mvecs):
                self.sent_vec[k] = v

    def sent_matrix(self, u1: Unit, u2: Unit) -> np.ndarray:
        s1, s2 = self.sents.get(u1.uid, []), self.sents.get(u2.uid, [])
        if not s1 or not s2:
            return np.zeros((max(len(s1), 1), max(len(s2), 1)))
        m1 = np.stack([self.sent_vec[(u1.uid, i)] for i in range(len(s1))])
        m2 = np.stack([self.sent_vec[(u2.uid, i)] for i in range(len(s2))])
        return m1 @ m2.T


# ============================================================
# 通道打分
# ============================================================
def ch_sem_asym(idx: EmbIndex, d: Unit, u: Unit, tau: float = 0.60) -> dict:
    """不对称句级聚合: 长侧分段, 只考核短侧覆盖率"""
    S = idx.sent_matrix(d, u)
    peak = float(S.max()) if S.size else 0.0
    if len(d.content) <= len(u.content):
        short, Ss = d, S
    else:
        short, Ss = u, S.T
    b = Ss.max(axis=1) if Ss.size else np.array([])
    ss = idx.sents.get(short.uid, [])
    if len(b) and ss:
        lens = np.array([len(s) for s in ss], dtype=np.float32)
        cover = float((lens[b >= tau]).sum() / max(lens.sum(), 1e-9))
    else:
        cover = 0.0
    whole = float(idx.unit_vec[d.uid] @ idx.unit_vec[u.uid])
    return {'score': max(whole, 0.4 * peak + 0.6 * cover), 'whole': whole,
            'peak': peak, 'cover': cover}


def ch_contain(norm_d: str, norm_u: str) -> float:
    """短侧bigram被长侧包含的比例(长度不对称改造)"""
    if not norm_d or not norm_u:
        return 0.0
    short, long_ = (norm_d, norm_u) if len(norm_d) <= len(norm_u) else (norm_u, norm_d)
    bg_s = {short[i:i + 2] for i in range(len(short) - 1)}
    bg_l = {long_[i:i + 2] for i in range(len(long_) - 1)}
    if not bg_s:
        return 0.0
    return len(bg_s & bg_l) / len(bg_s)


def _rare_hit_rate(rare_a: dict, tok_b: set) -> float:
    if not rare_a:
        return 0.0
    return sum(w for t, w in rare_a.items() if t in tok_b) / sum(rare_a.values())


def ch_idf_asym_r(rare_d: dict, rare_u: dict, tok_d: set, tok_u: set) -> float:
    """非对称IDF(R向): 上游稀有词被下游命中率(细化匹配方向)"""
    return _rare_hit_rate(rare_u, tok_d)


def ch_anchor(norm_d: str, up_anchors: list[tuple[str, float, str]]) -> dict:
    """
    列表项锚点: 上游列表项被下游(归一化后)包含的比例(长度加权)
    up_anchors: [(归一化锚点, 权重, 原文), ...]
    """
    if not up_anchors or not norm_d:
        return {'score': 0.0, 'hits': []}
    hits = []
    total_w = 0.0
    hit_w = 0.0
    bg_d = None
    for a_norm, w, a_raw in up_anchors:
        total_w += w
        if a_norm in norm_d:
            hit_w += w
            hits.append(a_raw)
        elif len(a_norm) >= 6:
            if bg_d is None:
                bg_d = {norm_d[i:i + 2] for i in range(len(norm_d) - 1)}
            bg_a = {a_norm[i:i + 2] for i in range(len(a_norm) - 1)}
            if bg_a and len(bg_a & bg_d) / len(bg_a) >= 0.85:
                hit_w += w * 0.8
                hits.append(a_raw + '~')
    return {'score': hit_w / total_w if total_w else 0.0, 'hits': hits}


# ============================================================
# 包含图 (章节 -> 内部条目)
# ============================================================
def _bigram_set(norm_text: str) -> set:
    return {norm_text[i:i + 2] for i in range(len(norm_text) - 1)}


def build_containment(up: list[Unit], norm: dict[str, str]) -> dict[str, list[str]]:
    """返回 section_uid -> [item_uid] (条目bigram>=70%落在章节内)"""
    items = [u for u in up if u.kind == 'item']
    secs = [u for u in up if u.kind == 'section']
    bg = {u.uid: _bigram_set(norm[u.uid]) for u in up}
    children: dict[str, list[str]] = {}
    for s in secs:
        if not bg[s.uid]:
            continue
        kids = []
        for it in items:
            if not bg[it.uid]:
                continue
            if len(bg[it.uid] & bg[s.uid]) / len(bg[it.uid]) >= 0.70:
                kids.append(it.uid)
        children[s.uid] = kids
    return children


# ============================================================
# 发现引擎
# ============================================================
class CandidateDiscovery:
    """候选追踪发现引擎"""

    def __init__(
        self,
        ds_units: list[Unit],
        up_units: list[Unit],
        weights: tuple[float, float, float, float] = DEFAULT_WEIGHTS,
        theta: float = THETA_ACCEPT,
        margin: float = AMBIGUOUS_MARGIN,
        max_relations: int = MAX_RELATIONS,
        anchor_gate: float = ANCHOR_GATE,
        anchor_boost: float = ANCHOR_BOOST,
        section_penalty: float = SECTION_PENALTY,
        fold_delta: float = FOLD_DELTA,
        recall_k: int = RECALL_K,
        verbose: bool = False,
    ):
        self.ds_units = ds_units
        self.up_units = up_units
        self.weights = weights
        self.theta = theta
        self.margin = margin
        self.max_relations = max_relations
        self.anchor_gate = anchor_gate
        self.anchor_boost = anchor_boost
        self.section_penalty = section_penalty
        self.fold_delta = fold_delta
        self.recall_k = recall_k
        self.verbose = verbose
        self.idx = EmbIndex()
        # uid -> 上游单元索引: 消除折叠/_mk_cand等处的重复线性扫描
        self._up_idx: dict[str, int] = {}

    # ---------- 预计算 ----------
    def _precompute(self):
        all_units = self.ds_units + self.up_units
        if self.verbose:
            print(f'    [discover] 编码 {len(all_units)} 个单元...')
        self.idx.add_units(all_units)
        # uid -> index 字典(供折叠/候选构建 O(1) 查找)
        self._up_idx = {u.uid: j for j, u in enumerate(self.up_units)}

        # 归一化文本 / token集合
        self.norm: dict[str, str] = {}
        self.toks: dict[str, set] = {}
        for u in all_units:
            self.norm[u.uid] = _normalize_for_char_compare(u.content)
            self.toks[u.uid] = set(tokenize(u.content))

        # IDF统计 (候选池 = 上游 + 下游)
        df: dict[str, int] = {}
        for u in all_units:
            for t in self.toks[u.uid]:
                df[t] = df.get(t, 0) + 1
        N = len(all_units)
        rare_max = max(2, N // 10)
        self.rare: dict[str, dict] = {}
        for u in all_units:
            self.rare[u.uid] = {
                t: math.log(N / df[t]) + 1.0
                for t in self.toks[u.uid] if df.get(t, 0) <= rare_max
            }

        # 上游锚点(列表项)预提取
        self.up_anchors: dict[str, list[tuple[str, float, str]]] = {}
        for u in self.up_units:
            anchors = []
            for a in extract_list_items(u.content):
                a_norm = _normalize_for_char_compare(a)
                if len(a_norm) >= 3:
                    anchors.append((a_norm, float(len(a_norm)), a[:30]))
            self.up_anchors[u.uid] = anchors

        # TF-IDF 稀疏->稠密矩阵
        vocab = sorted(df.keys())
        vidx = {t: i for i, t in enumerate(vocab)}

        def _tfidf_vec(u: Unit) -> np.ndarray:
            v = np.zeros(len(vocab), dtype=np.float32)
            tf: dict[str, int] = {}
            for t in tokenize(u.content):
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                i = vidx.get(t)
                if i is not None:
                    v[i] = (1 + math.log(c)) * (math.log((N + 1) / (df[t] + 1)) + 1.0)
            n = np.linalg.norm(v)
            return v / n if n > 0 else v

        self.tfidf_ds = np.stack([_tfidf_vec(u) for u in self.ds_units]) if self.ds_units else np.zeros((0, len(vocab)))
        self.tfidf_up = np.stack([_tfidf_vec(u) for u in self.up_units]) if self.up_units else np.zeros((0, len(vocab)))

        # 包含图 + 章节降权掩码
        self.children = build_containment(self.up_units, self.norm)
        self.sec_mask = np.ones(len(self.up_units))
        # 锚点加成掩码: 列表项锚点是"条目"的属性, 章节(尤其含条目的伞形章节)
        # 获得锚点加成会吸纳条目导致虚高(如B1的"48V直流电源系统紧急操作台"),
        # 故锚点加成仅作用于条目, 不作用于任何章节。
        self.anchor_boost_mask = np.ones(len(self.up_units))
        for j, u in enumerate(self.up_units):
            if u.kind == 'section':
                self.anchor_boost_mask[j] = 0.0
                if self.children.get(u.uid):
                    self.sec_mask[j] = self.section_penalty

        # 上游单元的安全分级(标题+内容中显式声明的F-SC代码)
        self._up_classes = [
            extract_class_codes(u.key) | extract_class_codes(u.content)
            for u in self.up_units
        ]

    # ---------- 通道矩阵 ----------
    def _channel_matrices(self):
        n_ds, n_up = len(self.ds_units), len(self.up_units)
        # 语义(整条) 与 TF-IDF: 矩阵乘
        ds_mat = np.stack([self.idx.unit_vec[d.uid] for d in self.ds_units])
        up_mat = np.stack([self.idx.unit_vec[u.uid] for u in self.up_units])
        whole = ds_mat @ up_mat.T
        tfidf_sim = self.tfidf_ds @ self.tfidf_up.T

        contain = np.zeros((n_ds, n_up))
        idfR = np.zeros((n_ds, n_up))
        anchor = np.zeros((n_ds, n_up))
        anchor_hits: dict[tuple[int, int], list[str]] = {}
        for i, d in enumerate(self.ds_units):
            nd = self.norm[d.uid]
            for j, u in enumerate(self.up_units):
                contain[i, j] = ch_contain(nd, self.norm[u.uid])
                idfR[i, j] = ch_idf_asym_r(
                    self.rare[d.uid], self.rare[u.uid], self.toks[d.uid], self.toks[u.uid])
                r = ch_anchor(nd, self.up_anchors[u.uid])
                anchor[i, j] = r['score']
                if r['hits']:
                    anchor_hits[(i, j)] = r['hits']
        return whole, contain, idfR, anchor, anchor_hits, tfidf_sim

    # ---------- 锚点固定尺度归一化 ----------
    def _anchor_norm(self, anchor) -> np.ndarray:
        """
        锚点原始分绝对值低但判别力强(列表项锚点通道)。
        用固定尺度(除以 ANCHOR_SCALE)归一化到 [0,1], 保留绝对量级信息,
        避免按行归一化把微小噪声锚点放大到1.0(如B1的"48V直流电源系统"章节)。
        """
        return np.clip(anchor / ANCHOR_SCALE, 0.0, 1.0)

    # ---------- 融合 ----------
    def _fuse(self, sem, contain, idfR, tfidf_sim, anchor_norm) -> np.ndarray:
        w_sem, w_con, w_idf, w_tf = self.weights
        base = w_sem * sem + w_con * contain + w_idf * idfR + w_tf * tfidf_sim
        base = base * self.sec_mask[None, :]
        # 锚点门控: 归一化锚点超过门限时强抬升(避免线性稀释)
        # 含条目章节(伞形容器)不施加锚点加成
        eff_anchor = anchor_norm * self.anchor_boost_mask[None, :]
        gate = eff_anchor >= self.anchor_gate
        fused = np.where(gate, base + self.anchor_boost * eff_anchor, base)
        return np.clip(fused, 0.0, 1.0)

    # ---------- 具体性折叠 ----------
    def _fold_order(self, order: list[tuple[str, float]]) -> list[tuple[str, float]]:
        if not order:
            return order
        top_uid, top_s = order[0]
        top_j = self._up_idx.get(top_uid)
        top_u = self.up_units[top_j] if top_j is not None else None
        if top_u is not None and top_u.kind == 'section':
            sc = dict(order)
            kids = self.children.get(top_uid, [])
            passing = [(k, sc[k]) for k in kids if sc.get(k, 0) >= top_s - self.fold_delta]
            if len(passing) == 1:
                k, s = passing[0]
                order = [(k, s)] + [(a, b) for a, b in order if a != k]
        return order

    # ---------- 预计算(昂贵): 编码 + 通道矩阵 ----------
    def prepare(self):
        """执行嵌入编码与全部通道矩阵计算, 结果缓存供多次 decide() 复用。"""
        if getattr(self, '_prepared', False):
            return
        if not self.ds_units or not self.up_units:
            self._prepared = True
            return
        self._precompute()
        whole, contain, idfR, anchor, anchor_hits, tfidf_sim = self._channel_matrices()
        self._whole = whole
        self._contain = contain
        self._idfR = idfR
        self._anchor = anchor
        self._anchor_hits = anchor_hits
        self._tfidf = tfidf_sim
        self._anchor_norm_mat = self._anchor_norm(anchor)

        # 并集召回候选集(每下游单元), 与融合权重无关, 只依赖通道矩阵
        coarse = self._fuse(whole, contain, idfR, tfidf_sim, self._anchor_norm_mat)
        self._cand_idx: list[set[int]] = []
        for i in range(len(self.ds_units)):
            cand = set()
            for mat in (whole, contain, idfR, tfidf_sim, anchor):
                row = mat[i] * self.sec_mask
                cand |= set(int(x) for x in np.argsort(-row)[:self.recall_k])
            cand |= set(int(x) for x in np.argsort(-coarse[i])[:self.recall_k * 2])
            # 折叠需要章节内条目, 补入
            for j in list(cand):
                u = self.up_units[j]
                if u.kind == 'section':
                    for k_uid in self.children.get(u.uid, []):
                        k = self._up_idx.get(k_uid)
                        if k is not None:
                            cand.add(k)
            self._cand_idx.append(cand)

        # 句级语义聚合(仅对候选集), 与融合权重无关
        self._sem_fine: list[dict[int, float]] = []
        for i, d in enumerate(self.ds_units):
            sf = {}
            for j in self._cand_idx[i]:
                sf[j] = ch_sem_asym(self.idx, d, self.up_units[j])['score']
            self._sem_fine.append(sf)

        self._prepared = True

    # ---------- 裁决(廉价): 融合 + 折叠 + 阈值 ----------
    def decide(self) -> list[DiscoveryDecision]:
        if not getattr(self, '_prepared', False):
            self.prepare()
        if not self.ds_units or not self.up_units:
            return [DiscoveryDecision(ds_unit=d, status='untraced') for d in self.ds_units]

        whole, contain, idfR = self._whole, self._contain, self._idfR
        anchor, anchor_hits, tfidf_sim = self._anchor, self._anchor_hits, self._tfidf
        anchor_norm = self._anchor_norm_mat

        w_sem, w_con, w_idf, w_tf = self.weights
        up_by_uid = {u.uid: u for u in self.up_units}
        decisions: list[DiscoveryDecision] = []

        for i, d in enumerate(self.ds_units):
            cand_idx = self._cand_idx[i]
            sem_fine = self._sem_fine[i]
            ds_cls = extract_class_codes(d.key) | extract_class_codes(d.content)

            # ---- 精排融合分 ----
            scored = []
            for j in cand_idx:
                s = (w_sem * sem_fine.get(j, float(whole[i, j]))
                     + w_con * contain[i, j]
                     + w_idf * idfR[i, j] + w_tf * tfidf_sim[i, j])
                s *= self.sec_mask[j]
                # 分级互斥: 两侧都显式声明分级且完全不相交 -> 对象不同, 强惩罚。
                # (F-SC1/F-SC2模板章节内容近似是常态, 分级代码才是判别锚)
                up_cls = self._up_classes[j]
                if ds_cls and up_cls and not (ds_cls & up_cls):
                    s *= CLASS_MISMATCH_PENALTY
                eff_a = anchor_norm[i, j] * self.anchor_boost_mask[j]
                if eff_a >= self.anchor_gate:
                    s += self.anchor_boost * eff_a
                scored.append((self.up_units[j].uid, float(min(s, 1.0))))
            scored.sort(key=lambda kv: -kv[1])
            scored = self._fold_order(scored)

            def _mk_cand(uid: str, score: float) -> Candidate:
                j = self._up_idx[uid]
                up_cls = self._up_classes[j]
                ev = {
                    'sem': sem_fine.get(j, float(whole[i, j])),
                    'contain': float(contain[i, j]),
                    'idf': float(idfR[i, j]),
                    'tfidf': float(tfidf_sim[i, j]),
                    'anchor': float(anchor[i, j]),
                    'anchor_hits': anchor_hits.get((i, j), []),
                    'cls_mismatch': bool(ds_cls and up_cls and not (ds_cls & up_cls)),
                    'ds_cls': ', '.join(sorted(ds_cls)),
                    'up_cls': ', '.join(sorted(up_cls)),
                }
                return Candidate(unit=up_by_uid[uid], score=score, evidence=ev)

            top_cands = [_mk_cand(uid, s) for uid, s in scored[:3]]

            # ---- 裁决 ----
            if not scored or scored[0][1] < self.theta:
                decisions.append(DiscoveryDecision(
                    ds_unit=d, status='untraced', top_candidates=top_cands))
                continue

            main_uid, main_s = scored[0]
            rels = [_mk_cand(main_uid, main_s)]
            for uid, s in scored[1:]:
                if len(rels) >= self.max_relations:
                    break
                if s >= max(self.theta, SECONDARY_RATIO * main_s):
                    rels.append(_mk_cand(uid, s))

            ambiguous = len(scored) >= 2 and (main_s - scored[1][1]) < self.margin
            decisions.append(DiscoveryDecision(
                ds_unit=d, status='traced', main=rels[0],
                secondaries=rels[1:], ambiguous=ambiguous,
                top_candidates=top_cands))

        return decisions

    # ---------- 主流程 ----------
    def run(self) -> list[DiscoveryDecision]:
        self.prepare()
        return self.decide()


# ============================================================
# 证据格式化
# ============================================================
def format_evidence(ev: dict) -> str:
    """通道证据 -> 人类可读摘要"""
    if not ev:
        return ''
    parts = [
        f"语义{ev.get('sem', 0):.2f}",
        f"包含{ev.get('contain', 0):.2f}",
        f"稀有词{ev.get('idf', 0):.2f}",
        f"锚点{ev.get('anchor', 0):.2f}",
    ]
    s = ' '.join(parts)
    if ev.get('cls_mismatch'):
        # 分级互斥: 对象不同的降权证据(如下游F-SC2 vs 候选F-SC1)
        s += f" | 分级不符(下游{ev.get('ds_cls') or '未声明'} vs 候选{ev.get('up_cls') or '未声明'})"
    hits = ev.get('anchor_hits') or []
    if hits:
        s += ' | 锚点: ' + '、'.join(h.strip()[:20] for h in hits[:3])
    return s


def format_untraced_evidence(dec: DiscoveryDecision) -> str:
    """UNTRACED行的证据: 参考候选top3"""
    refs = [f"{c.unit.doc}:{c.unit.key[:24]}({c.score:.2f})"
            for c in dec.top_candidates]
    return 'UNTRACED | 参考候选: ' + '; '.join(refs) if refs else 'UNTRACED'


# ============================================================
# PDF级便捷入口
# ============================================================
def discover_from_pdfs(
    downstream_pdf: str,
    upstream_pdfs: dict[str, str],
    downstream_doc_name: str | None = None,
    **kwargs,
) -> list[DiscoveryDecision]:
    """
    对一份下游PDF与多份上游PDF执行候选发现

    Args:
        downstream_pdf: 下游文档PDF路径
        upstream_pdfs: {文档名(不含.pdf): PDF路径}
        downstream_doc_name: 下游文档名(默认取文件名)

    Returns:
        每个下游单元一条 DiscoveryDecision
    """
    import os
    if downstream_doc_name is None:
        downstream_doc_name = os.path.splitext(os.path.basename(downstream_pdf))[0]

    ds_units = unitize_pdf(downstream_pdf, downstream_doc_name, role='downstream')
    up_units: list[Unit] = []
    for doc_name, pdf_path in upstream_pdfs.items():
        up_units.extend(unitize_pdf(pdf_path, doc_name, role='upstream'))

    disc = CandidateDiscovery(ds_units, up_units, **kwargs)
    return disc.run()
