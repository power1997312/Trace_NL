"""
追踪系统纯函数单元测试(不加载 ML 模型, 不读 PDF)

覆盖:
- _classify_block: 分类决策树的各阶段阈值(GREEN快速通道/NLI蕴含BLUE/矛盾BLACK/回退BLUE)
- _find_green_substrings / _select_non_overlapping: 子短语GREEN扫描
- decompose_text: 微块坐标切片一致性
- ContentMap.resolve: 空引用守卫与多策略解析

运行:
    python -m unittest tests.test_matcher_unit -v
    或 python -m pytest tests/test_matcher_unit.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import MatchCategory
from core.text_matcher import (
    _classify_block, _find_green_substrings, _select_non_overlapping,
    decompose_text,
)
from core.requirement_extractor import ContentMap


def _nli(ent=0.0, neu=0.0, con=0.0):
    """NLI 桩: 返回固定概率分布(全零=不判定, 走后续回退路径)"""
    return lambda pairs: [
        {'CONTRADICTION': con, 'NEUTRAL': neu, 'ENTAILMENT': ent} for _ in pairs
    ]


def _nli_must_not_run(pairs):
    raise AssertionError('该路径不应触发 NLI 调用')


class TestClassifyBlock(unittest.TestCase):

    def test_embedding_below_floor_is_black(self):
        r = _classify_block('自动停堆功能', '设备维护周期要求', 0.30, _nli_must_not_run)
        self.assertIs(r, MatchCategory.BLACK)

    def test_identical_long_text_green(self):
        t = '系统应在自动停堆信号产生时执行停堆功能'
        r = _classify_block(t, t, 0.97, _nli_must_not_run)
        self.assertIs(r, MatchCategory.GREEN)

    def test_short_identical_fast_path_green(self):
        r = _classify_block('反应堆保护', '反应堆保护', 0.60, _nli_must_not_run)
        self.assertIs(r, MatchCategory.GREEN)

    def test_normalized_identical_green(self):
        # "功能"后缀差异: 归一化后一致 → Stage 2a-fallback GREEN
        a = '提供自动停堆功能'
        b = '提供自动停堆'
        r = _classify_block(a, b, 0.86, _nli_must_not_run)
        self.assertIs(r, MatchCategory.GREEN)

    def test_entailment_with_overlap_blue(self):
        a = '系统应提供反应堆保护系统的定期试验功能'
        b = '反应堆保护系统应提供定期试验功能'
        r = _classify_block(a, b, 0.80, _nli(ent=0.92))
        self.assertIs(r, MatchCategory.BLUE)

    def test_neutral_plus_embedding_blue(self):
        a = '系统应提供反应堆保护系统的定期试验功能'
        b = '反应堆保护系统应提供定期试验功能'
        r = _classify_block(a, b, 0.82, _nli(neu=0.85))
        self.assertIs(r, MatchCategory.BLUE)

    def test_contradiction_black(self):
        a = '系统应采用四取二的停堆逻辑'
        b = '系统应采用三取二的停堆逻辑'
        r = _classify_block(a, b, 0.80, _nli(con=0.9))
        self.assertIs(r, MatchCategory.BLACK)

    def test_stage2c_lcs_fallback_blue(self):
        # NLI 不判定 + 高嵌入 + 高LCS → Stage 2c BLUE(无需NLI结论)
        a = '自动停堆模式下系统应自动断开断路器'
        b = '自动停堆模式下系统应自动断开断路器和停堆'
        r = _classify_block(a, b, 0.86, _nli())
        self.assertIs(r, MatchCategory.BLUE)

    def test_no_rule_matched_black(self):
        a = '通信流量应满足实时性要求'
        b = '设备布置应便于维修'
        r = _classify_block(a, b, 0.60, _nli())
        self.assertIs(r, MatchCategory.BLACK)


class TestFindGreenSubstrings(unittest.TestCase):

    def test_finds_common_substring(self):
        found = _find_green_substrings(
            '系统应提供自动停堆功能',
            '当自动停堆信号出现时系统应提供保护')
        self.assertTrue(found, '应找到公共子串')
        self.assertTrue(all(e - s >= 4 for s, e, _ in found))

    def test_disjoint_texts_find_nothing(self):
        found = _find_green_substrings('甲乙丙丁戊己', '子丑寅卯辰巳')
        self.assertEqual(found, [])

    def test_long_input_truncated_not_crash(self):
        # 长文本护栏: 不崩溃且仍有正常输出
        long_a = '反应堆保护' * 100
        long_b = '反应堆保护' * 50 + '其他内容'
        found = _find_green_substrings(long_a, long_b)
        self.assertIsInstance(found, list)


class TestSelectNonOverlapping(unittest.TestCase):

    def test_overlapping_rejected(self):
        sel = _select_non_overlapping([(0, 10, 10), (5, 15, 10), (20, 25, 5)])
        self.assertEqual(sel, [(0, 10, 10), (20, 25, 5)])

    def test_empty(self):
        self.assertEqual(_select_non_overlapping([]), [])


class TestDecomposeText(unittest.TestCase):

    def test_positions_slice_back(self):
        text = '第一段落，第一短语；第二短语。\n第二段落，另一个短语。'
        blocks = decompose_text(text)
        self.assertTrue(blocks)
        for b in blocks:
            self.assertEqual(text[b.start:b.end], b.text)


class TestContentMapResolve(unittest.TestCase):

    def _map(self) -> ContentMap:
        cm = ContentMap()
        cm.add_item('<UR-001>', '内容一')
        cm.add_section('3.2', '系统功能', '系统应具备的功能描述')
        return cm

    def test_empty_reference_returns_none(self):
        cm = self._map()
        self.assertIsNone(cm.resolve(''))
        self.assertIsNone(cm.resolve('   '))
        self.assertIsNone(cm.resolve(None))

    def test_exact_id(self):
        cm = self._map()
        self.assertEqual(cm.resolve('<UR-001>'), '内容一')

    def test_exact_section(self):
        cm = self._map()
        self.assertEqual(cm.resolve('3.2'), '系统应具备的功能描述')


if __name__ == '__main__':
    unittest.main()
