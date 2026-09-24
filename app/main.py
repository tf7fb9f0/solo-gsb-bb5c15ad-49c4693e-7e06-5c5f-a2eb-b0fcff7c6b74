"""字幕时间漂移校正与位置规划 Web 服务（FastAPI）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from .mapping import AnchorError, PiecewiseMapping, build_anchors
from .placement import (
    SIDE_NAMES,
    Interval,
    merge_intervals,
    overlap_duration,
    plan_positions,
    render_webvtt,
)
from .srt import SrtParseError, parse_srt, rewrite_srt
from .timecode import format_signed_timecode, format_timecode, parse_timecode

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="字幕时间漂移校正工具",
    description=(
        "粘贴 SRT 与多组原时间/正确时间锚点，按分段线性映射校正字幕时间；"
        "或给出画面上/下方遮挡时段，为每条字幕规划上下位置并导出 WebVTT。"
    ),
    version="1.1.0",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AnchorPair(BaseModel):
    original: str = Field(..., description="字幕上观察到的原时间，HH:MM:SS,mmm")
    corrected: str = Field(..., description="人工确认的正确时间，HH:MM:SS,mmm")


class CorrectRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    anchors: list[AnchorPair] = Field(..., min_length=2)


class CorrectedCue(BaseModel):
    index: str
    original_start: str
    original_end: str
    corrected_start: str
    corrected_end: str
    text: str


class CorrectResponse(BaseModel):
    corrected_srt: str
    cues: list[CorrectedCue]
    warnings: list[str]


class BlockedRange(BaseModel):
    start: str = Field(..., description="遮挡开始时间，HH:MM:SS,mmm")
    end: str = Field(..., description="遮挡结束时间，HH:MM:SS,mmm，必须严格晚于开始")


class PlacementRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    top_blocks: list[BlockedRange] = Field(
        default_factory=list, description="画面上方的遮挡时间段"
    )
    bottom_blocks: list[BlockedRange] = Field(
        default_factory=list, description="画面下方的遮挡时间段"
    )
    switch_cost_ms: StrictInt = Field(
        ..., description="相邻字幕由上方换到下方（或反向）的代价，单位毫秒，非负整数"
    )


class PlacementCue(BaseModel):
    index: str
    start: str
    end: str
    text: str
    position: str = Field(..., description="所选位置：top / bottom")
    top_blocked_ms: int = Field(..., description="上方遮挡时长（毫秒）")
    bottom_blocked_ms: int = Field(..., description="下方遮挡时长（毫秒）")
    chosen_blocked_ms: int = Field(
        ..., description="所选位置上仍被遮挡的时长（毫秒），0 表示完全不遮挡"
    )


class PlacementResponse(BaseModel):
    webvtt: str
    cues: list[PlacementCue]
    occluded_indices: list[str] = Field(
        ..., description="所选位置仍被遮挡（chosen_blocked_ms > 0）的字幕序号"
    )
    total_blocked_ms: int = Field(..., description="各字幕在所选位置的遮挡时长之和（毫秒）")
    total_switch_cost_ms: int = Field(..., description="相邻字幕换位代价之和（毫秒）")
    switch_count: int = Field(..., description="发生上下换位的相邻字幕对数量")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/placement")
def placement_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "placement.html")


@app.post("/api/correct", response_model=CorrectResponse)
def correct(req: CorrectRequest) -> CorrectResponse | JSONResponse:
    # 1. 解析 SRT（保留原始序号与文本；无法解析仍返回 422）
    try:
        cues = parse_srt(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 2. 校验锚点：数量、时间格式、两侧时间严格递增
    pairs = [(pair.original, pair.corrected) for pair in req.anchors]
    try:
        anchors = build_anchors(pairs)
    except AnchorError as exc:
        raise HTTPException(
            status_code=422, detail={"type": "anchors", "message": str(exc)}
        )

    mapping = PiecewiseMapping(anchors)
    warnings: list[str] = []
    new_ranges: list[tuple[int, int]] = []

    # 3. 逐条字幕做分段线性映射
    for cue in cues:
        new_start_raw = mapping.map_ms(cue.start)
        new_end_raw = mapping.map_ms(cue.end)
        new_start = max(new_start_raw, 0)
        new_end = max(new_end_raw, 0)

        if new_start_raw < 0 or new_end_raw < 0:
            warnings.append(
                f"序号 {cue.index}：校正后早于 00:00:00,000，"
                f"时间已截断到 0（计算值 {format_signed_timecode(new_start_raw)}"
                f" → {format_signed_timecode(new_end_raw)}），"
                "建议在更早位置补充锚点。"
            )
        if new_end <= new_start:
            warnings.append(
                f"序号 {cue.index}：校正后字幕时长不为正"
                f"（{format_timecode(new_start)} → {format_timecode(new_end)}），"
                "该结果已保留但可能异常，请检查锚点。"
            )

        new_ranges.append((new_start, new_end))

    # 4. 无损重写：只替换各时间轴行的起止时间码；序号、正文、箭头空格、
    #    X1:/X2: 等定位参数、空行、原有换行及开头 BOM 全部保持原样
    try:
        result_srt = rewrite_srt(req.srt, new_ranges)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    cue_views = [
        CorrectedCue(
            index=cue.index,
            original_start=format_timecode(cue.start),
            original_end=format_timecode(cue.end),
            corrected_start=format_timecode(new_start),
            corrected_end=format_timecode(new_end),
            text=cue.text,
        )
        for cue, (new_start, new_end) in zip(cues, new_ranges)
    ]

    return CorrectResponse(corrected_srt=result_srt, cues=cue_views, warnings=warnings)


def _parse_blocks(blocks: list[BlockedRange], side_label: str) -> list[Interval]:
    """把某一侧的遮挡时间段解析为毫秒区间并校验起止有效。"""
    intervals: list[Interval] = []
    for pos, block in enumerate(blocks, start=1):
        try:
            start_ms = parse_timecode(block.start)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "blocks",
                    "message": f"画面{side_label}第 {pos} 个遮挡时段的开始时间无效：{exc}",
                },
            )
        try:
            end_ms = parse_timecode(block.end)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "blocks",
                    "message": f"画面{side_label}第 {pos} 个遮挡时段的结束时间无效：{exc}",
                },
            )
        if end_ms <= start_ms:
            raise HTTPException(
                status_code=422,
                detail={
                    "type": "blocks",
                    "message": (
                        f"画面{side_label}第 {pos} 个遮挡时段"
                        f"（{block.start.strip()} → {block.end.strip()}）的结束时间"
                        "必须严格晚于开始时间。"
                    ),
                },
            )
        intervals.append((start_ms, end_ms))
    return intervals


@app.post("/api/placement", response_model=PlacementResponse)
def placement(req: PlacementRequest) -> PlacementResponse | JSONResponse:
    # 1. 复用现有 SRT 解析器（无法解析返回 422）
    try:
        cues = parse_srt(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # StrictInt 负责“非负整数”的类型校验（浮点/字符串/布尔均拒绝），
    # 这里补充语义上的非负检查
    if req.switch_cost_ms < 0:
        raise HTTPException(
            status_code=422,
            detail={
                "type": "switch_cost",
                "message": "换位代价 switch_cost_ms 必须是非负整数（单位毫秒）。",
            },
        )

    # 2. 校验遮挡时段并对同侧重叠/相接区间求并集
    top_union = merge_intervals(_parse_blocks(req.top_blocks, "上方"))
    bottom_union = merge_intervals(_parse_blocks(req.bottom_blocks, "下方"))

    # 3. 按并集区间与每条字幕的时间交集累计遮挡时长
    top_ms = [overlap_duration(c.start, c.end, top_union) for c in cues]
    bottom_ms = [overlap_duration(c.start, c.end, bottom_union) for c in cues]

    # 4. 动态规划选位：总遮挡时长 + 相邻换位代价最小，同分优先下方
    sides, chosen_ms = plan_positions(top_ms, bottom_ms, req.switch_cost_ms)

    cue_views = [
        PlacementCue(
            index=cue.index,
            start=format_timecode(cue.start),
            end=format_timecode(cue.end),
            text=cue.text,
            position=SIDE_NAMES[side],
            top_blocked_ms=top_ms[i],
            bottom_blocked_ms=bottom_ms[i],
            chosen_blocked_ms=chosen_ms[i],
        )
        for i, (cue, side) in enumerate(zip(cues, sides))
    ]
    occluded_indices = [
        cue.index for cue, blocked in zip(cues, chosen_ms) if blocked > 0
    ]
    switch_count = sum(1 for a, b in zip(sides, sides[1:]) if a != b)

    return PlacementResponse(
        webvtt=render_webvtt(cues, sides),
        cues=cue_views,
        occluded_indices=occluded_indices,
        total_blocked_ms=sum(chosen_ms),
        total_switch_cost_ms=switch_count * req.switch_cost_ms,
        switch_count=switch_count,
    )
