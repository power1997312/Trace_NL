"""
bge-small-zh 中文嵌入模型封装
使用 transformers 直接加载，避免 sentence-transformers 版本不匹配问题
"""
from __future__ import annotations

from collections import OrderedDict

import numpy as np
import torch
from transformers import AutoTokenizer, AutoModel

from config import EMBEDDER_MODEL_PATH, MAX_EMBEDDING_SEQ_LENGTH
from models.device_utils import load_tokenizer, resolve_device

_model = None
_tokenizer = None
_device = None

# 文本 -> 向量 的结果缓存。
# 追踪矩阵中同一条上游内容常被多行引用, 同一段落也会在正向/逆向流程里反复编码,
# 缓存后重复文本只做一次前向传播。
_EMB_CACHE_MAX = 20000
_emb_cache: OrderedDict[str, np.ndarray] = OrderedDict()


def _get_device() -> torch.device:
    """获取可用设备(按当前 PyTorch 实际支持的 GPU 架构自适应)"""
    return resolve_device()


def _load_model():
    """懒加载模型"""
    global _model, _tokenizer, _device
    if _model is not None:
        return

    _device = _get_device()
    _tokenizer = load_tokenizer(AutoTokenizer, EMBEDDER_MODEL_PATH)
    _model = AutoModel.from_pretrained(EMBEDDER_MODEL_PATH)
    _model.to(_device)
    _model.eval()


def clear_embedding_cache() -> None:
    """清空嵌入结果缓存"""
    _emb_cache.clear()


def _cache_get(text: str):
    vec = _emb_cache.get(text)
    if vec is not None:
        _emb_cache.move_to_end(text)
    return vec


def _cache_put(text: str, vec: np.ndarray) -> None:
    _emb_cache[text] = vec
    _emb_cache.move_to_end(text)
    while len(_emb_cache) > _EMB_CACHE_MAX:
        _emb_cache.popitem(last=False)


def _encode_uncached(texts: list[str], batch_size: int) -> np.ndarray:
    """对确定未命中缓存的文本做前向传播(内部按长度分桶以减少padding浪费)"""
    # 长度相近的文本放进同一批, 可显著减少 padding 到批内最长带来的无效计算。
    # 每条文本的向量只取决于自身(padding 位被 attention_mask 屏蔽),
    # 因此分批方式不影响结果。
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
    out = np.empty((len(texts), 0), dtype=np.float32)
    collected: list[tuple[int, np.ndarray]] = []

    for start in range(0, len(order), batch_size):
        idxs = order[start:start + batch_size]
        batch = [texts[i] for i in idxs]

        encoded = _tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=MAX_EMBEDDING_SEQ_LENGTH,
            return_tensors="pt",
        )
        encoded = {k: v.to(_device) for k, v in encoded.items()}

        with torch.inference_mode():
            outputs = _model(**encoded)

        # Mean pooling (带attention mask)
        token_embeddings = outputs.last_hidden_state  # (batch, seq_len, hidden)
        attention_mask = encoded["attention_mask"].unsqueeze(-1)  # (batch, seq_len, 1)

        sum_embeddings = (token_embeddings * attention_mask).sum(dim=1)
        sum_mask = attention_mask.sum(dim=1).clamp(min=1e-9)
        mean_embeddings = sum_embeddings / sum_mask

        # L2归一化
        mean_embeddings = torch.nn.functional.normalize(mean_embeddings, p=2, dim=1)
        arr = mean_embeddings.cpu().numpy()

        if out.shape[1] == 0:
            out = np.empty((len(texts), arr.shape[1]), dtype=arr.dtype)
        for pos, i in enumerate(idxs):
            collected.append((i, arr[pos]))

    for i, vec in collected:
        out[i] = vec
    return out


def encode(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """
    将文本列表编码为归一化嵌入向量

    Args:
        texts: 待编码的文本列表
        batch_size: 批处理大小

    Returns:
        np.ndarray: shape=(len(texts), 512), L2归一化后的嵌入向量
    """
    _load_model()

    if not texts:
        return np.array([])

    # 1) 查缓存, 收集未命中且互不重复的文本
    pending: list[str] = []
    pending_pos: dict[str, int] = {}
    cached: list[np.ndarray | None] = []
    for t in texts:
        vec = _cache_get(t)
        cached.append(vec)
        if vec is None and t not in pending_pos:
            pending_pos[t] = len(pending)
            pending.append(t)

    # 2) 仅对未命中文本做一次批量前向
    if pending:
        fresh = _encode_uncached(pending, batch_size)
        for t, i in pending_pos.items():
            _cache_put(t, fresh[i])
    else:
        fresh = None

    # 3) 按原始顺序组装
    dim = None
    for vec in cached:
        if vec is not None:
            dim = vec.shape[0]
            break
    if dim is None and fresh is not None:
        dim = fresh.shape[1]
    if dim is None:
        return np.array([])

    result = np.empty((len(texts), dim), dtype=np.float32)
    for n, (t, vec) in enumerate(zip(texts, cached)):
        result[n] = vec if vec is not None else fresh[pending_pos[t]]
    return result


def cosine_similarity_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    计算两组向量之间的余弦相似度矩阵

    Args:
        a: shape=(m, d)
        b: shape=(n, d)

    Returns:
        np.ndarray: shape=(m, n) 余弦相似度矩阵
    """
    # 向量已归一化，直接矩阵乘法
    return a @ b.T
