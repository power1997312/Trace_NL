"""
推理设备与分词器的自适应选择

原实现把可用算力硬编码为 sm_90 以内, 导致较新的显卡(如 RTX 50 系为 sm_120)
即使装了匹配的 PyTorch 也会被判定为不可用而退回 CPU。这里改为查询当前
PyTorch 二进制实际编译进去的架构列表, 装什么版本就能用什么卡, 无需再改代码。
"""
from __future__ import annotations

import os

import torch

_device: torch.device | None = None


def resolve_device() -> torch.device:
    """
    选择推理设备(结果全局缓存, 只探测一次)

    可用环境变量 TRACE_NL_DEVICE 强制指定, 取值 cpu / cuda。
    """
    global _device
    if _device is not None:
        return _device

    forced = os.environ.get("TRACE_NL_DEVICE", "").strip().lower()
    if forced in ("cpu", "cuda"):
        if forced == "cpu" or torch.cuda.is_available():
            _device = torch.device(forced)
            return _device

    _device = _probe_cuda()
    return _device


def _probe_cuda() -> torch.device:
    """
    仅当当前 PyTorch 二进制真正为本机显卡架构编译过内核时才启用 GPU。

    必须精确匹配 sm_XX, 不能依赖 PTX 前向兼容: 例如 torch cu118 的架构列表里
    有 compute_37, 但在 sm_120 的显卡上, 注意力等预编译内核会直接报
    "kernel ... was built for sm37" 并退化, 反而比纯 CPU 慢十几倍。
    宁可稳妥回落 CPU, 也不要落进这种不可用的 GPU 状态。
    """
    if not torch.cuda.is_available():
        return torch.device("cpu")

    try:
        major, minor = torch.cuda.get_device_capability()
        # 当前 torch 二进制真正编译支持的架构, 例如 ['sm_75', ..., 'sm_120']
        if f"sm_{major}{minor}" not in torch.cuda.get_arch_list():
            return torch.device("cpu")

        # 真实跑一次极小的矩阵乘, 确认驱动/运行时组合确实可用
        probe = torch.zeros(8, 8, device="cuda")
        _ = (probe @ probe).sum().item()
        return torch.device("cuda")
    except Exception:
        # 任何 CUDA 初始化/内核加载失败都安全退回 CPU
        return torch.device("cpu")


def load_tokenizer(auto_tokenizer_cls, model_path: str):
    """
    加载分词器: 优先 Rust 快速分词器, 不可用时退回纯 Python 慢速实现。

    快速分词器比慢速版快一个数量级, 对长文档批量编码影响明显。
    BERT 架构下两者切分结果一致, 不影响追踪判定。
    若运行环境缺少 tokenizers 的二进制(如 Windows 7), 会自动回退。
    """
    if os.environ.get("TRACE_NL_SLOW_TOKENIZER", "").strip() == "1":
        return auto_tokenizer_cls.from_pretrained(model_path, use_fast=False)
    try:
        return auto_tokenizer_cls.from_pretrained(model_path, use_fast=True)
    except Exception:
        return auto_tokenizer_cls.from_pretrained(model_path, use_fast=False)
