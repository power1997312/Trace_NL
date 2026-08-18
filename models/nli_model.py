"""
Erlangshen-Roberta-110M-NLI 中文自然语言推理模型封装
3分类: CONTRADICTION(0), NEUTRAL(1), ENTAILMENT(2)
"""
from __future__ import annotations

from collections import OrderedDict

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from config import NLI_MODEL_PATH, MAX_EMBEDDING_SEQ_LENGTH
from models.device_utils import load_tokenizer, resolve_device

_model = None
_tokenizer = None
_device = None
_LABELS = {0: "CONTRADICTION", 1: "NEUTRAL", 2: "ENTAILMENT"}

# (前提, 假设) -> 概率字典 的结果缓存。
# NLI 是整个流程最慢的一环, 而同一对文本会在正向/逆向矩阵中被反复判定。
_NLI_CACHE_MAX = 20000
_nli_cache: OrderedDict[tuple[str, str], dict[str, float]] = OrderedDict()


def _get_device() -> torch.device:
    """获取可用设备(按当前 PyTorch 实际支持的 GPU 架构自适应)"""
    return resolve_device()


def _load_model():
    """懒加载NLI模型"""
    global _model, _tokenizer, _device
    if _model is not None:
        return

    _device = _get_device()
    _tokenizer = load_tokenizer(AutoTokenizer, NLI_MODEL_PATH)
    _model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL_PATH)
    _model.to(_device)
    _model.eval()


def clear_nli_cache() -> None:
    """清空NLI结果缓存"""
    _nli_cache.clear()


def _cache_get(key):
    val = _nli_cache.get(key)
    if val is not None:
        _nli_cache.move_to_end(key)
    return val


def _cache_put(key, val) -> None:
    _nli_cache[key] = val
    _nli_cache.move_to_end(key)
    while len(_nli_cache) > _NLI_CACHE_MAX:
        _nli_cache.popitem(last=False)


def predict(premise: str, hypothesis: str) -> dict[str, float]:
    """
    对单对文本进行NLI推理

    Args:
        premise: 前提文本
        hypothesis: 假设文本

    Returns:
        dict: {"CONTRADICTION": float, "NEUTRAL": float, "ENTAILMENT": float}
    """
    results = predict_batch([(premise, hypothesis)])
    return results[0]


def _predict_uncached(pairs: list[tuple[str, str]], batch_size: int) -> list[dict[str, float]]:
    """对确定未命中缓存的文本对做前向传播(按长度分桶减少padding浪费)"""
    order = sorted(range(len(pairs)), key=lambda i: len(pairs[i][0]) + len(pairs[i][1]))
    results: list[dict[str, float] | None] = [None] * len(pairs)

    for start in range(0, len(order), batch_size):
        idxs = order[start:start + batch_size]
        premises = [pairs[i][0] for i in idxs]
        hypotheses = [pairs[i][1] for i in idxs]

        encoded = _tokenizer(
            premises,
            hypotheses,
            padding=True,
            truncation=True,
            max_length=MAX_EMBEDDING_SEQ_LENGTH,
            return_tensors="pt",
        )
        encoded = {k: v.to(_device) for k, v in encoded.items()}

        with torch.inference_mode():
            outputs = _model(**encoded)

        # Softmax概率
        probs = torch.softmax(outputs.logits, dim=-1).cpu().numpy()

        for pos, i in enumerate(idxs):
            prob = probs[pos]
            results[i] = {_LABELS[j]: float(prob[j]) for j in range(len(_LABELS))}

    return results  # type: ignore[return-value]


def predict_batch(pairs: list[tuple[str, str]], batch_size: int = 16) -> list[dict[str, float]]:
    """
    批量NLI推理

    Args:
        pairs: [(premise, hypothesis), ...] 文本对列表
        batch_size: 批处理大小

    Returns:
        list[dict]: 每个元素为 {"CONTRADICTION": float, "NEUTRAL": float, "ENTAILMENT": float}
    """
    _load_model()

    if not pairs:
        return []

    # 1) 查缓存, 收集未命中且互不重复的文本对
    pending: list[tuple[str, str]] = []
    pending_pos: dict[tuple[str, str], int] = {}
    cached: list[dict[str, float] | None] = []
    for p in pairs:
        key = (p[0], p[1])
        val = _cache_get(key)
        cached.append(val)
        if val is None and key not in pending_pos:
            pending_pos[key] = len(pending)
            pending.append(key)

    # 2) 仅对未命中的文本对做一次批量前向
    fresh = _predict_uncached(pending, batch_size) if pending else []
    for key, i in pending_pos.items():
        _cache_put(key, fresh[i])

    # 3) 按原始顺序组装
    return [
        val if val is not None else fresh[pending_pos[(p[0], p[1])]]
        for p, val in zip(pairs, cached)
    ]
