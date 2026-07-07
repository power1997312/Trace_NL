"""
Erlangshen-Roberta-110M-NLI 中文自然语言推理模型封装
3分类: CONTRADICTION(0), NEUTRAL(1), ENTAILMENT(2)
"""
import torch
import numpy as np
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from config import NLI_MODEL_PATH, MAX_EMBEDDING_SEQ_LENGTH

_model = None
_tokenizer = None
_device = None
_LABELS = {0: "CONTRADICTION", 1: "NEUTRAL", 2: "ENTAILMENT"}


def _get_device() -> torch.device:
    """获取可用设备，检查GPU实际兼容性"""
    if torch.cuda.is_available():
        cap = torch.cuda.get_device_capability()
        if cap[0] * 10 + cap[1] <= 90:
            return torch.device("cuda")
    return torch.device("cpu")


def _load_model():
    """懒加载NLI模型"""
    global _model, _tokenizer, _device
    if _model is not None:
        return

    _device = _get_device()
    _tokenizer = AutoTokenizer.from_pretrained(NLI_MODEL_PATH)
    _model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL_PATH)
    _model.to(_device)
    _model.eval()


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

    all_results = []

    for i in range(0, len(pairs), batch_size):
        batch = pairs[i:i + batch_size]
        premises = [p[0] for p in batch]
        hypotheses = [p[1] for p in batch]

        encoded = _tokenizer(
            premises,
            hypotheses,
            padding=True,
            truncation=True,
            max_length=MAX_EMBEDDING_SEQ_LENGTH,
            return_tensors="pt",
        )
        encoded = {k: v.to(_device) for k, v in encoded.items()}

        with torch.no_grad():
            outputs = _model(**encoded)

        # Softmax概率
        probs = torch.softmax(outputs.logits, dim=-1).cpu().numpy()

        for prob in probs:
            result = {
                _LABELS[j]: float(prob[j])
                for j in range(len(_LABELS))
            }
            all_results.append(result)

    return all_results
