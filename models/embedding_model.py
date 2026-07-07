"""
bge-small-zh 中文嵌入模型封装
使用 transformers 直接加载，避免 sentence-transformers 版本不匹配问题
"""
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModel
from config import EMBEDDER_MODEL_PATH, MAX_EMBEDDING_SEQ_LENGTH

_model = None
_tokenizer = None
_device = None


def _get_device() -> torch.device:
    """获取可用设备，检查GPU实际兼容性"""
    if torch.cuda.is_available():
        cap = torch.cuda.get_device_capability()
        # PyTorch当前版本支持sm_37到sm_90
        if cap[0] * 10 + cap[1] <= 90:
            return torch.device("cuda")
    return torch.device("cpu")


def _load_model():
    """懒加载模型"""
    global _model, _tokenizer, _device
    if _model is not None:
        return

    _device = _get_device()
    _tokenizer = AutoTokenizer.from_pretrained(EMBEDDER_MODEL_PATH)
    _model = AutoModel.from_pretrained(EMBEDDER_MODEL_PATH)
    _model.to(_device)
    _model.eval()


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

    all_embeddings = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        # 截断超长文本
        encoded = _tokenizer(
            batch,
            padding=True,
            truncation=True,
            max_length=MAX_EMBEDDING_SEQ_LENGTH,
            return_tensors="pt",
        )
        encoded = {k: v.to(_device) for k, v in encoded.items()}

        with torch.no_grad():
            outputs = _model(**encoded)

        # Mean pooling (带attention mask)
        token_embeddings = outputs.last_hidden_state  # (batch, seq_len, hidden)
        attention_mask = encoded["attention_mask"].unsqueeze(-1)  # (batch, seq_len, 1)

        sum_embeddings = (token_embeddings * attention_mask).sum(dim=1)
        sum_mask = attention_mask.sum(dim=1).clamp(min=1e-9)
        mean_embeddings = sum_embeddings / sum_mask

        # L2归一化
        mean_embeddings = torch.nn.functional.normalize(mean_embeddings, p=2, dim=1)
        all_embeddings.append(mean_embeddings.cpu().numpy())

    return np.vstack(all_embeddings)


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
