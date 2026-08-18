"""L3 阅读顺序恢复（Reading Order）。

方法（研究报告 3.4.1）：
1. paint_order（内容流顺序）作为基线；
2. 几何拓扑校正：块间建立"重叠关系 DAG"（上方块→下方块；左侧块→右侧块），
   对该 DAG 拓扑排序；
3. 多栏文档按列分组，列内排序后从左到右拼接。
"""
from __future__ import annotations

from pdfparser.models import Block

COL_GAP = 30.0  # 列间距阈值


def _vertical_overlap(a: Block, b: Block) -> bool:
    return not (a.y1 < b.y0 or b.y1 < a.y0)


def _x_overlap(a: Block, b: Block, tol: float = 8.0) -> bool:
    return not (a.x1 < b.x0 - tol or b.x1 < a.x0 - tol)


class ReadingOrderAssigner:
    """阅读顺序分配。"""

    def assign(self, blocks: list[Block], columns: list[list[int]] | None = None) -> None:
        """为每页的块按阅读顺序编号（order 字段，0 起，跨页递增）。"""
        # 按页分组
        by_page: dict[int, list[Block]] = {}
        for b in blocks:
            by_page.setdefault(b.page, []).append(b)

        order = 0
        for pno in sorted(by_page):
            page_blocks = by_page[pno]
            ordered = self._order_page(page_blocks)
            for b in ordered:
                b.order = order
                order += 1

    # ------------------------------------------------------------------
    def _order_page(self, blocks: list[Block]) -> list[Block]:
        if len(blocks) <= 1:
            return list(blocks)
        # 列分组：按 x0 起点聚类
        cols = self._cluster_columns(blocks)
        result: list[Block] = []
        for col in cols:  # 列从左到右
            if len(col) == 1:
                result.append(col[0])
                continue
            topo = self._topo_sort(col)
            result.extend(topo)
        return result

    def _cluster_columns(self, blocks: list[Block]) -> list[list[Block]]:
        """按 x0 聚类分列（块数量少时不强制多列）。"""
        if len(blocks) < 8:
            return [list(blocks)]
        sorted_blk = sorted(blocks, key=lambda b: b.x0)
        cols: list[list[Block]] = [[sorted_blk[0]]]
        for b in sorted_blk[1:]:
            if b.x0 - cols[-1][-1].x0 > COL_GAP * 3:
                cols.append([b])
            else:
                cols[-1].append(b)
        # 过滤：单块列并入相邻列（避免把图片/边注当独立列）
        merged: list[list[Block]] = []
        for col in cols:
            if merged and (len(col) == 1 or len(merged[-1]) == 1):
                merged[-1].extend(col)
            else:
                merged.append(col)
        return merged

    # ------------------------------------------------------------------
    def _topo_sort(self, blocks: list[Block]) -> list[Block]:
        """块重叠 DAG 拓扑排序。tiebreak = paint_order。"""
        n = len(blocks)
        adj: list[list[int]] = [[] for _ in range(n)]
        indeg = [0] * n
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                a, b = blocks[i], blocks[j]
                # a 在 b 之前：垂直重叠时 a 在左；或 a 完全在上且水平重叠
                if _vertical_overlap(a, b):
                    if a.x1 < b.x0 - 4.0:
                        adj[i].append(j); indeg[j] += 1
                else:
                    if a.y1 <= b.y0 and _x_overlap(a, b):
                        adj[i].append(j); indeg[j] += 1
        # Kahn 拓扑排序，用堆保证稳定（tiebreak: paint_order）
        import heapq
        heap = [i for i in range(n) if indeg[i] == 0]
        heapq.heapify(heap)
        # 以 paint_order 排序作为稳定性键
        key = {i: blocks[i].paint_order for i in range(n)}
        heap = [(key[i], i) for i in heap]
        heapq.heapify(heap)
        result: list[int] = []
        while heap:
            _, i = heapq.heappop(heap)
            result.append(i)
            for j in adj[i]:
                indeg[j] -= 1
                if indeg[j] == 0:
                    heapq.heappush(heap, (key[j], j))
        # 若有环（罕见），把剩余节点按 paint_order 追加
        if len(result) < n:
            rest = sorted(set(range(n)) - set(result), key=lambda x: key[x])
            result.extend(rest)
        return [blocks[i] for i in result]
