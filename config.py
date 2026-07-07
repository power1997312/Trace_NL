"""
追踪验证系统 - 全局配置模块
"""
import os
from enum import Enum

# ============================================================
# 项目路径
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(PROJECT_ROOT, "model")
EMBEDDER_MODEL_PATH = os.path.join(MODEL_DIR, "embedder")
NLI_MODEL_PATH = os.path.join(MODEL_DIR, "nli")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

# ============================================================
# 文档层级目录映射
# ============================================================
DOCUMENT_LEVELS = {
    "用户需求": {"role": "upstream", "level": 0},
    "系统需求": {"role": "midstream", "level": 1},
    "系统设计": {"role": "downstream", "level": 2},
    "软件需求": {"role": "downstream", "level": 3},
    "硬件需求": {"role": "downstream", "level": 3},
}

# ============================================================
# 条目ID通用正则
# 匹配尖括号内包含至少一个字母和一个数字的任意字符串
# 容错 < 后的空格, 如 < DCS-SyRS001>
# ============================================================
ITEM_ID_REGEX = r'<\s*[^>]*[A-Za-z][^>]*\d[^>]*>|<\s*[^>]*\d[^>]*[A-Za-z][^>]*>'

# 章节号正则 (如 3.2.2.1, 3.2.1 等)
SECTION_NUMBER_REGEX = r'^(\d+(?:\.\d+)+)\s+(.+)$'

# 页眉页脚正则 (用于清除PDF提取文本中的页眉页脚)
HEADER_FOOTER_REGEX = r'^\s*\S+\s+\S+\s+版本：\S+\s+页码：\S+\s*$'

# ============================================================
# 匹配阈值
# ============================================================
EXACT_CHAR_THRESHOLD = 0.92          # 字符级Jaccard相似度(精确匹配, 提高阈值减少假阳性)
EMBEDDING_EXACT_THRESHOLD = 0.95     # 嵌入余弦相似度(精确, 提高到0.95)
EMBEDDING_SEMANTIC_THRESHOLD = 0.70  # 嵌入余弦相似度(语义, 降低到0.70以捕获更多因分解粒度差异导致的BLUE)
EMBEDDING_UNMATCHED_THRESHOLD = 0.50 # 嵌入余弦相似度(最低门槛, 低于此直接BLACK)
NLI_ENTAILMENT_THRESHOLD = 0.85      # NLI蕴含概率阈值(提高到0.85)
NLI_NEUTRAL_EMBEDDING_THRESHOLD = 0.80  # NLI中立+嵌入联合判定阈值(提高)

# ============================================================
# 微块设置
# ============================================================
MIN_PHRASE_LENGTH = 4                # 短语最小字符数(降低以增加分解粒度)
MAX_EMBEDDING_SEQ_LENGTH = 512       # 模型最大序列长度

# ============================================================
# Excel格式常量 (来自Trace_Base.xlsx分析)
# ============================================================
HEADER_FILL = "4472C4"
COLOR_GREEN = "00B050"               # 完全一致
COLOR_BLUE = "0070C0"                # 语义匹配
COLOR_BLACK = "000000"               # 未匹配
FONT_NAME = "宋体"
HEADER_FONT_SIZE = 11
CONTENT_FONT_SIZE = 10
ID_FONT_SIZE = 11


class MatchCategory(Enum):
    """匹配类别枚举"""
    GREEN = "exact"       # 完全一致
    BLUE = "semantic"     # 语义匹配
    BLACK = "unmatched"   # 未匹配
    UNMATCHED = "unmatched"

    @property
    def color(self) -> str:
        color_map = {
            "exact": COLOR_GREEN,
            "semantic": COLOR_BLUE,
            "unmatched": COLOR_BLACK,
        }
        return color_map.get(self.value, COLOR_BLACK)

    @property
    def priority(self) -> int:
        """优先级: GREEN > BLUE > BLACK"""
        priority_map = {"exact": 3, "semantic": 2, "unmatched": 1}
        return priority_map.get(self.value, 0)
