"""字幕画面位置（上方/下方）规划与 WebVTT 导出。

输入每条字幕的时间区间与画面上、下两侧各自的遮挡时间段，规划规则：

1. 同侧遮挡时间段先**求并集**：按起点排序后，重叠或首尾相接的区间合并为一段；
2. 每条字幕在某一侧的遮挡时长 = 该侧并集区间与字幕 ``[start, end)`` 的交集
   毫秒数之和；
3. 以 ``Σ 所选位置的遮挡时长 + Σ 相邻字幕上下换位代价`` 最小为目标逐条选位；
4. 多个方案总分相同时**优先下方**（字典序：越靠前的字幕越优先放在下方）。

求解用后缀动态规划：``suffix[i][prev]`` 表示第 ``i-1`` 条已在 ``prev`` 侧时，
第 ``i`` 条及之后的最小代价；再从第 0 条起贪心回溯，每步在保持全局最优的
候选里优先选下方，由此保证同分方案里下方位最靠前。
"""
from __future__ import annotations

from .srt import Cue

# 用 0/1 编码两侧：同分按字典序比较时 0 在前，天然实现“优先下方”
BOTTOM = 0
TOP = 1
SIDE_NAMES = {BOTTOM: "bottom", TOP: "top"}

# WebVTT 的 line 百分比：0% 为画面顶边、100% 为底边；往内收一点避免贴边
TOP_LINE_PERCENT = 10
BOTTOM_LINE_PERCENT = 90

Interval = tuple[int, int]


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """把 ``[start, end)`` 区间求并集：按起点排序，重叠或相接（``start <= 上一段 end``）即合并。"""
    if not intervals:
        return []

    ordered = sorted(intervals)
    merged: list[list[int]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            # 被前一段包含或首尾相接：仅当延伸更远时扩张
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def overlap_duration(start: int, end: int, intervals: list[Interval]) -> int:
    """字幕 ``[start, end)`` 与一组已求并集（有序、互不重叠）区间的交集总毫秒。"""
    total = 0
    for iv_start, iv_end in intervals:
        if iv_start >= end:
            break
        lo = max(start, iv_start)
        hi = min(end, iv_end)
        if hi > lo:
            total += hi - lo
    return total


def _suffix_costs(
    top_ms: list[int], bottom_ms: list[int], switch_cost: int
) -> list[list[int]]:
    """``suffix[i][prev]``：第 i-1 条已在 prev 侧时，第 i 条到末尾的最小代价。"""
    n = len(top_ms)
    suffix = [[0, 0] for _ in range(n + 1)]
    for i in range(n - 1, 0, -1):
        # 前一条在上方：继续留上方只计遮挡，换到下方另加换位代价
        suffix[i][TOP] = min(
            top_ms[i] + suffix[i + 1][TOP],
            bottom_ms[i] + switch_cost + suffix[i + 1][BOTTOM],
        )
        # 前一条在下方：继续留下方只计遮挡，换到上方另加换位代价
        suffix[i][BOTTOM] = min(
            top_ms[i] + switch_cost + suffix[i + 1][TOP],
            bottom_ms[i] + suffix[i + 1][BOTTOM],
        )
    return suffix


def plan_positions(
    top_ms: list[int], bottom_ms: list[int], switch_cost: int
) -> tuple[list[int], list[int]]:
    """为每条字幕选位，返回 ``(sides, chosen_ms)``。

    ``sides`` 元素为 :data:`TOP` / :data:`BOTTOM`；``chosen_ms[i]`` 为第 i 条
    在所选位置上仍被遮挡的毫秒数。第 0 条前面没有字幕，不计换位代价。
    """
    if len(top_ms) != len(bottom_ms):
        raise ValueError("上下侧遮挡时长数量必须与字幕条数一致。")
    n = len(top_ms)
    if n == 0:
        return [], []

    suffix = _suffix_costs(top_ms, bottom_ms, switch_cost)

    sides: list[int] = []
    chosen_ms: list[int] = []
    prev: int | None = None
    for i in range(n):
        cost_top = top_ms[i] + suffix[i + 1][TOP]
        cost_bottom = bottom_ms[i] + suffix[i + 1][BOTTOM]
        if i > 0 and prev is not None:
            if prev != TOP:
                cost_top += switch_cost
            if prev != BOTTOM:
                cost_bottom += switch_cost

        # 严格小于才选上方 → 总分相同一律下方（且 suffix 已是全局最优，
        # 从第 0 条起逐位取下方可得到最优方案中“下方最靠前”的字典序结果）
        side = TOP if cost_top < cost_bottom else BOTTOM
        sides.append(side)
        chosen_ms.append(top_ms[i] if side == TOP else bottom_ms[i])
        prev = side

    return sides, chosen_ms


def format_vtt_timecode(ms: int) -> str:
    """把毫秒格式化为 WebVTT 时间码 ``HH:MM:SS.mmm``（分隔符为句点）。"""
    hours, rem = divmod(max(ms, 0), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, milliseconds = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def render_webvtt(cues: list[Cue], sides: list[int]) -> str:
    """把选位结果渲染为带 ``line`` 位置设置的 WebVTT 文本。"""
    if len(cues) != len(sides):
        raise ValueError("选位结果数量与字幕条数不一致。")

    blocks: list[str] = []
    for cue, side in zip(cues, sides):
        line_pct = TOP_LINE_PERCENT if side == TOP else BOTTOM_LINE_PERCENT
        blocks.append(
            "\n".join(
                [
                    cue.index,
                    f"{format_vtt_timecode(cue.start)} --> "
                    f"{format_vtt_timecode(cue.end)} line:{line_pct}%",
                    cue.text,
                ]
            )
        )
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
