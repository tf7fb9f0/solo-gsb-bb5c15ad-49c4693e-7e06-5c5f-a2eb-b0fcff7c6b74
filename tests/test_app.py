"""核心算法与 FastAPI 接口测试。"""
import itertools

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.mapping import AnchorError, PiecewiseMapping, build_anchors
from app.placement import (
    BOTTOM,
    TOP,
    merge_intervals,
    overlap_duration,
    plan_positions,
    render_webvtt,
)
from app.srt import BOM, SrtParseError, Cue, format_srt, parse_srt, rewrite_srt
from app.timecode import format_timecode, parse_timecode

client = TestClient(app)


# ---------- 时间码 ----------

def test_parse_and_format_timecode():
    assert parse_timecode("00:01:23,456") == 83_456
    assert parse_timecode("1:2:3.4") == 3_723_400  # 毫秒补 3 位 -> 400
    assert format_timecode(83_456) == "00:01:23,456"
    assert format_timecode(-5) == "00:00:00,000"


def test_invalid_timecode():
    with pytest.raises(ValueError):
        parse_timecode("00:60:00,000")
    with pytest.raises(ValueError):
        parse_timecode("not-a-time")
    with pytest.raises(ValueError):
        parse_timecode("")


# ---------- SRT ----------

SAMPLE = (
    "1\r\n"
    "00:00:05,000 --> 00:00:08,000\r\n"
    "你好\n\n"
    "2\n"
    "00:00:10,000 --> 00:00:12,500\n"
    "第二行上\n第二行下\n"
)


def test_parse_preserves_index_and_text():
    cues = parse_srt(SAMPLE)
    assert len(cues) == 2
    assert cues[0].index == "1"
    assert cues[0].text == "你好"
    assert cues[1].text == "第二行上\n第二行下"
    assert cues[1].end - cues[1].start == 2500


def test_parse_missing_index_auto_numbers():
    cues = parse_srt("00:00:01,000 --> 00:00:02,000\n无序号块\n")
    assert cues[0].index == "1"


def test_parse_errors():
    with pytest.raises(SrtParseError):
        parse_srt("")
    with pytest.raises(SrtParseError):
        parse_srt("1\n00:00:05,000 --> 00:00:05,000\n等长\n")
    with pytest.raises(SrtParseError):
        parse_srt("1\n00:00:05,000 --> 00:00:03,000\n倒退\n")


def test_roundtrip_srt():
    out = format_srt(parse_srt(SAMPLE))
    assert "00:00:05,000 --> 00:00:08,000" in out
    assert "第二行上\n第二行下" in out


# ---------- 无损重写 ----------

POS_SAMPLE = (
    BOM
    + "1\r\n"
    + "  00:00:05,000 --> 00:00:08,000  X1:10 X2:630 Y1:20 Y2:60\r\n"
    + "你好\r\n"
    + "\r\n"
    + "2\r\n"
    + "00:00:10,000\t-->\t00:00:12,500  Position:50%,30%,0\r\n"
    + "第二行上\r\n第二行下\r\n"
)


def test_rewrite_identity_is_byte_identical():
    # 恒等锚点：除时间码按规范重写外，其余内容（BOM/CRLF/参数/箭头空格）逐字节不动
    cues = parse_srt(POS_SAMPLE)
    out = rewrite_srt(POS_SAMPLE, [(c.start, c.end) for c in cues])
    assert out == POS_SAMPLE


def test_rewrite_only_touches_timing_timecodes():
    cues = parse_srt(POS_SAMPLE)
    out = rewrite_srt(POS_SAMPLE, [(1_000, 4_000), (6_000, 8_500)])

    # 开头 BOM 唯一保留
    assert out.startswith(BOM)
    assert BOM not in out[1:]  # 正文内没有额外 BOM

    # 原有 CRLF 换行、定位参数、箭头前后空格全部保留
    assert (
        "  00:00:01,000 --> 00:00:04,000  X1:10 X2:630 Y1:20 Y2:60\r\n"
    ) in out
    assert (
        "00:00:06,000\t-->\t00:00:08,500  Position:50%,30%,0\r\n"
    ) in out

    # 序号与正文原样
    assert "1\r\n" in out
    assert "你好\r\n" in out
    assert "第二行上\r\n第二行下\r\n" in out

    # 旧时间码不再残留
    assert "00:00:05,000" not in out
    assert "00:00:12,500" not in out


def test_rewrite_preserves_blank_lines_and_lf():
    src = (
        "1\n00:00:05,000 --> 00:00:08,000\n第一句\n"
        "\n"
        "\n"  # 多余空行也保留
        "2\n00:00:10,000 --> 00:00:12,500 X1:1\r\n第二句\n"
    )
    cues = parse_srt(src)
    out = rewrite_srt(src, [(0, 1_000), (2_000, 3_000)])
    assert out.count("\r") == 1  # 仅第二块原有的那个 CR 保留，其余仍是 LF
    assert out.startswith("1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n\n2\n")
    assert out.endswith("00:00:02,000 --> 00:00:03,000 X1:1\r\n第二句\n")


def test_rewrite_without_bom_stays_bomless():
    src = "1\n00:00:05,000 --> 00:00:08,000\n你好\n"
    out = rewrite_srt(src, [(1_000, 2_000)])
    assert not out.startswith(BOM)
    assert out == "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


def test_rewrite_collapses_multiple_leading_boms_to_one():
    src = BOM + BOM + "1\n00:00:05,000 --> 00:00:08,000\n你好\n"
    cues = parse_srt(src)
    assert len(cues) == 1
    out = rewrite_srt(src, [(1_000, 2_000)])
    assert out == BOM + "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


def test_rewrite_invalid_srt_raises():
    with pytest.raises(SrtParseError):
        rewrite_srt("garbage without timing", [(1_000, 2_000)])


# ---------- 锚点与映射 ----------

def test_build_anchors_requires_two():
    with pytest.raises(AnchorError, match="至少需要 2 组"):
        build_anchors([("00:00:01,000", "00:00:00,500")])


def test_original_not_strictly_increasing_rejected():
    with pytest.raises(AnchorError, match="原时间未严格递增"):
        build_anchors([
            ("00:00:10,000", "00:00:08,000"),
            ("00:00:10,000", "00:00:09,000"),
        ])
    with pytest.raises(AnchorError, match="原时间未严格递增"):
        build_anchors([
            ("00:00:20,000", "00:00:08,000"),
            ("00:00:10,000", "00:00:09,000"),
        ])


def test_corrected_not_strictly_increasing_rejected():
    with pytest.raises(AnchorError, match="正确时间未严格递增"):
        build_anchors([
            ("00:00:10,000", "00:00:09,000"),
            ("00:00:20,000", "00:00:09,000"),
        ])


def test_invalid_anchor_time_rejected():
    with pytest.raises(AnchorError, match="原时间无效"):
        build_anchors([("bad", "00:00:09,000"), ("00:00:20,000", "00:00:19,000")])


def _mapping():
    # 10s->8s（-2s），100s->92s（-8s），斜率 56/60
    anchors = build_anchors([
        ("00:00:10,000", "00:00:08,000"),
        ("00:01:40,000", "00:01:32,000"),
    ])
    return PiecewiseMapping(anchors)


def test_mapping_hits_anchor_exactly():
    m = _mapping()
    assert m.map_ms(10_000) == 8_000
    assert m.map_ms(100_000) == 92_000


def test_mapping_interpolation_between_anchors():
    m = _mapping()
    # 中间点 55s -> 50s（线性中点）
    assert m.map_ms(55_000) == 50_000


def test_mapping_extrapolates_with_nearest_segment_slope():
    m = _mapping()
    # 右端外推：160s + 60s -> 92s + 56s = 148s
    assert m.map_ms(160_000) == 148_000
    # 左端外推：0 -> 8000 + (0-10000)*56/60 = 8000 - 9333.33 = -1333
    assert m.map_ms(0) == -1333


def test_three_anchor_piecewise_slopes():
    anchors = build_anchors([
        ("00:00:00,000", "00:00:00,000"),
        ("00:01:00,000", "00:00:54,000"),   # 斜率 0.9
        ("00:02:00,000", "00:02:00,000"),   # 斜率 1.1
    ])
    m = PiecewiseMapping(anchors)
    # 第一区段：30s -> 27s
    assert m.map_ms(30_000) == 27_000
    # 第二区段：90s -> 54s + (30s)*1.1 = 87s
    assert m.map_ms(90_000) == 87_000
    # 末段斜率外推：150s -> 120s + 30s*1.1 = 153s
    assert m.map_ms(150_000) == 153_000


# ---------- HTTP 接口 ----------

def request_body():
    return {
        "srt": SAMPLE,
        "anchors": [
            {"original": "00:00:10,000", "corrected": "00:00:08,000"},
            {"original": "00:01:40,000", "corrected": "00:01:32,000"},
        ],
    }


def test_health_and_index():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200


def test_api_correct_success():
    resp = client.post("/api/correct", json=request_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["cues"][0]["index"] == "1"
    assert data["cues"][0]["text"] == "你好"
    assert data["cues"][0]["corrected_start"] == format_timecode(
        _mapping().map_ms(5000)
    )
    assert "你好" in data["corrected_srt"]


def test_api_lossless_preserves_bom_crlf_and_positioning():
    src = (
        BOM
        + "1\r\n"
        + "00:00:05,000 --> 00:00:08,000 X1:10 X2:630\r\n"
        + "你好\r\n\r\n"
        + "2\r\n"
        + "00:00:10,000 --> 00:00:12,500\r\n"
        + "第二行\r\n"
    )
    body = {
        "srt": src,
        "anchors": [
            {"original": "00:00:05,000", "corrected": "00:00:05,000"},
            {"original": "00:00:10,000", "corrected": "00:00:10,000"},
        ],
    }
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 200, resp.text
    # 恒等锚点下，返回体应与输入逐字符一致（BOM/CRLF/定位参数均无损）
    assert resp.json()["corrected_srt"] == src


def test_api_bomless_input_returns_bomless_srt():
    src = "1\n00:00:05,000 --> 00:00:08,000 X1:10\n你好\n"
    body = {
        "srt": src,
        "anchors": [
            {"original": "00:00:05,000", "corrected": "00:00:06,000"},
            {"original": "00:00:10,000", "corrected": "00:00:11,000"},
        ],
    }
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 200, resp.text
    out = resp.json()["corrected_srt"]
    assert not out.startswith(BOM)
    assert out == "1\n00:00:06,000 --> 00:00:09,000 X1:10\n你好\n"


def test_api_rejects_bad_anchors_with_reason():
    body = request_body()
    body["anchors"][1]["original"] = "00:00:10,000"  # 与第一组相等
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "anchors"
    assert "原时间未严格递增" in resp.json()["detail"]["message"]


def test_api_rejects_bad_srt():
    body = request_body()
    body["srt"] = "garbage without timing"
    resp = client.post("/api/correct", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"


# ---------- 位置规划：区间并集与遮挡时长 ----------

def test_merge_intervals_overlapping_and_adjacent():
    # 重叠合并
    assert merge_intervals([(1000, 3000), (2000, 5000)]) == [(1000, 5000)]
    # 首尾相接（start == 上一段 end）也合并
    assert merge_intervals([(1000, 3000), (3000, 5000)]) == [(1000, 5000)]
    # 乱序输入先排序
    assert merge_intervals([(9000, 9500), (1000, 2000), (3000, 4000)]) == [
        (1000, 2000),
        (3000, 4000),
        (9000, 9500),
    ]
    # 包含关系不外扩
    assert merge_intervals([(1000, 9000), (2000, 3000)]) == [(1000, 9000)]
    assert merge_intervals([]) == []


def test_overlap_duration_with_union():
    union = [(1000, 3000), (5000, 8000)]
    # 字幕 [0, 9000)：2000 + 3000 = 5000
    assert overlap_duration(0, 9000, union) == 5000
    # 字幕 [2000, 6000)：1000 + 1000 = 2000
    assert overlap_duration(2000, 6000, union) == 2000
    # 完全落在遮挡内
    assert overlap_duration(5500, 7000, union) == 1500
    # 仅端点相接不算交集（半开区间）
    assert overlap_duration(3000, 5000, union) == 0
    # 完全不相交
    assert overlap_duration(9000, 10_000, union) == 0
    assert overlap_duration(0, 1000, []) == 0


# ---------- 位置规划：动态规划选位 ----------

def _brute_force_optimal(top_ms, bottom_ms, switch_cost):
    """枚举所有上下方案，返回 (最小代价, 该代价下最“下方优先”的方案元组)。"""
    n = len(top_ms)
    best_cost = None
    best_sides = None
    for sides in itertools.product((BOTTOM, TOP), repeat=n):
        cost = sum(top_ms[i] if sides[i] == TOP else bottom_ms[i] for i in range(n))
        cost += sum(switch_cost for a, b in zip(sides, sides[1:]) if a != b)
        if best_cost is None or cost < best_cost or (
            cost == best_cost and sides < best_sides
        ):
            best_cost = cost
            best_sides = sides
    return best_cost, list(best_sides)


def test_plan_prefers_bottom_on_tie():
    # 两侧遮挡完全相同：无论换位代价多少，全部待在下方且零换位最优
    top = [1000, 1000, 1000]
    bottom = [1000, 1000, 1000]
    sides, chosen = plan_positions(top, bottom, 500)
    assert sides == [BOTTOM, BOTTOM, BOTTOM]
    assert chosen == [1000, 1000, 1000]


def test_plan_no_switch_cost_picks_per_cue_minimum_bottom_tie():
    top = [5000, 100, 2000, 300]
    bottom = [100, 4000, 2000, 900]
    sides, _ = plan_positions(top, bottom, 0)
    # 第 0 条下更优；第 1 条上更优；第 2 条同分选下方；第 3 条上更优
    assert sides == [BOTTOM, TOP, BOTTOM, TOP]


def test_plan_pays_switch_only_when_occlusion_savings_exceed_it():
    # 单条：上方 100、下方 2000，换位代价 10000 也应选上方（第一条无前序换位代价）
    sides, chosen = plan_positions([100], [2000], 10_000)
    assert sides == [TOP]
    assert chosen == [100]

    # 两条：第一条下（0），第二条上只省 800 遮挡却要 1000 换位 → 留下
    sides, chosen = plan_positions([1000, 1000], [0, 1800], 1000)
    assert sides == [BOTTOM, BOTTOM]
    assert chosen == [0, 1800]

    # 换位代价降到 500：省下 800 > 500，第二条换上方
    sides, chosen = plan_positions([1000, 1000], [0, 1800], 500)
    assert sides == [BOTTOM, TOP]
    assert chosen == [0, 1000]


def test_plan_matches_brute_force_on_random_cases():
    import random

    rng = random.Random(20260924)
    for _ in range(200):
        n = rng.randint(1, 7)
        top = [rng.randint(0, 5000) for _ in range(n)]
        bottom = [rng.randint(0, 5000) for _ in range(n)]
        cost = rng.choice([0, 1, 500, 2000, 10_000])
        _, expected = _brute_force_optimal(top, bottom, cost)
        sides, _ = plan_positions(top, bottom, cost)
        assert sides == expected, (top, bottom, cost, sides, expected)


def test_plan_empty():
    assert plan_positions([], [], 100) == ([], [])


# ---------- WebVTT 导出 ----------

def test_render_webvtt_uses_line_setting():
    cues = [
        Cue(index="1", start=5000, end=8000, text="第一行"),
        Cue(index="2", start=10_000, end=12_500, text="第二行上\n第二行下"),
    ]
    vtt = render_webvtt(cues, [TOP, BOTTOM])
    lines = vtt.splitlines()
    assert lines[0] == "WEBVTT"
    assert "00:00:05.000 --> 00:00:08.000 line:10%" in vtt
    assert "00:00:10.000 --> 00:00:12.500 line:90%" in vtt
    assert vtt.endswith("\n")
    # 逗号时间分隔符只属于 SRT，WebVTT 必须是句点
    assert "00:00:05,000" not in vtt
    # 多行文本保留
    assert "第二行上\n第二行下" in vtt


# ---------- /api/placement ----------

PLACEMENT_SRT = (
    "1\n00:00:05,000 --> 00:00:09,000\n第一句\n\n"
    "2\n00:00:10,000 --> 00:00:14,000\n第二句\n\n"
    "3\n00:00:16,000 --> 00:00:20,000\n第三句\n"
)


def placement_body():
    return {
        "srt": PLACEMENT_SRT,
        # 上方 05–08 与 06–09 重叠，并集为 05–09：第 1 条上方遮挡 4000ms
        "top_blocks": [
            {"start": "00:00:05,000", "end": "00:00:08,000"},
            {"start": "00:00:06,000", "end": "00:00:09,000"},
            {"start": "00:00:11,000", "end": "00:00:13,000"},
        ],
        # 下方 11–19：第 2 条遮挡 3000，第 3 条遮挡 3000
        "bottom_blocks": [
            {"start": "00:00:11,000", "end": "00:00:19,000"},
        ],
        "switch_cost_ms": 1000,
    }


def test_placement_page_served():
    resp = client.get("/placement")
    assert resp.status_code == 200
    assert "字幕位置规划" in resp.text


def test_api_placement_success():
    resp = client.post("/api/placement", json=placement_body())
    assert resp.status_code == 200, resp.text
    data = resp.json()

    cues = {c["index"]: c for c in data["cues"]}
    assert cues["1"]["top_blocked_ms"] == 4000  # 重叠时段先并集再计交集
    assert cues["1"]["bottom_blocked_ms"] == 0
    assert cues["2"]["top_blocked_ms"] == 2000
    assert cues["2"]["bottom_blocked_ms"] == 3000
    assert cues["3"]["top_blocked_ms"] == 0
    assert cues["3"]["bottom_blocked_ms"] == 3000

    # 代价对比（第 1 条无前序换位代价）：
    # 下/下/下：0+3000+3000 = 6000
    # 下/上/上：0+2000+1000+0 = 3000（换位 1 次）← 最优
    # 下/上/下：0+2000+1000+3000+1000 = 7000
    positions = [c["position"] for c in data["cues"]]
    assert positions == ["bottom", "top", "top"]
    assert data["total_blocked_ms"] == 2000
    assert data["switch_count"] == 1
    assert data["total_switch_cost_ms"] == 1000
    # 只有第 2 条换到上方后仍被遮挡 2000ms；第 3 条上方无遮挡
    assert data["occluded_indices"] == ["2"]
    for c in data["cues"]:
        assert c["chosen_blocked_ms"] == (
            c["top_blocked_ms"] if c["position"] == "top" else c["bottom_blocked_ms"]
        )

    # WebVTT 带 line 位置设置：上方 line:10%，下方 line:90%
    assert data["webvtt"].startswith("WEBVTT\n\n")
    assert "00:00:05.000 --> 00:00:09.000 line:90%" in data["webvtt"]
    assert "00:00:10.000 --> 00:00:14.000 line:10%" in data["webvtt"]
    assert "第三句" in data["webvtt"]


def test_api_placement_bottom_wins_on_exact_tie():
    body = placement_body()
    # 换位代价 4000：下/上/上 = 2000+4000 = 6000，与全下 6000 同分 → 优先全下
    body["switch_cost_ms"] = 4000
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [c["position"] for c in data["cues"]] == [
        "bottom",
        "bottom",
        "bottom",
    ]
    assert data["switch_count"] == 0
    assert data["total_blocked_ms"] == 6000
    assert data["occluded_indices"] == ["2", "3"]

    # 代价 4001：换位不再划算，仍是全下
    body["switch_cost_ms"] = 4001
    resp = client.post("/api/placement", json=body)
    assert [c["position"] for c in resp.json()["cues"]] == [
        "bottom",
        "bottom",
        "bottom",
    ]


def test_api_placement_zero_cost_switches_freely():
    body = placement_body()
    body["switch_cost_ms"] = 0
    # 各条独立取遮挡更小的一侧：下/上/上
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [c["position"] for c in data["cues"]] == ["bottom", "top", "top"]
    assert data["switch_count"] == 1
    assert data["total_switch_cost_ms"] == 0
    assert data["total_blocked_ms"] == 2000
    assert data["occluded_indices"] == ["2"]


def test_api_placement_empty_blocks_all_bottom():
    body = {"srt": PLACEMENT_SRT, "top_blocks": [], "bottom_blocks": [], "switch_cost_ms": 0}
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [c["position"] for c in data["cues"]] == ["bottom", "bottom", "bottom"]
    assert data["occluded_indices"] == []
    assert data["total_blocked_ms"] == 0
    assert "line:90%" in data["webvtt"]


def test_api_placement_rejects_invalid_block_range():
    # 结束早于开始
    body = placement_body()
    body["top_blocks"] = [{"start": "00:00:10,000", "end": "00:00:09,000"}]
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "blocks"

    # 时间码无法解析
    body = placement_body()
    body["bottom_blocks"] = [{"start": "bad", "end": "00:00:09,000"}]
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "blocks"


@pytest.mark.parametrize("bad_cost", [-1, -100])
def test_api_placement_rejects_negative_switch_cost(bad_cost):
    body = placement_body()
    body["switch_cost_ms"] = bad_cost
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_cost", [1.5, 1000.0, "1000", True, None])
def test_api_placement_rejects_non_integer_switch_cost(bad_cost):
    body = placement_body()
    body["switch_cost_ms"] = bad_cost
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 422


def test_api_placement_zero_cost_accepted():
    body = placement_body()
    body["switch_cost_ms"] = 0
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 200, resp.text


def test_api_placement_rejects_bad_srt():
    body = placement_body()
    body["srt"] = "garbage without timing"
    resp = client.post("/api/placement", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["type"] == "srt"
