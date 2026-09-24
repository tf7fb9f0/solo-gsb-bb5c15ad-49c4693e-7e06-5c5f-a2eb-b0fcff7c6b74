"""字幕时间漂移校正与上下位置规划 Web 服务（FastAPI）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StrictInt

from .mapping import AnchorError, PiecewiseMapping, build_anchors
from .placement import (
    Block,
    PlacementError,
    count_switches,
    export_vtt,
    parse_blocks,
    plan_positions,
)
from .srt import SrtParseError, parse_srt, rewrite_srt
from .timecode import format_signed_timecode, format_timecode

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="字幕校正与位置规划工具",
    description="粘贴 SRT 后可做时间漂移校正，或按画面遮挡时段规划字幕上下位置。",
    version="1.1.0",
)


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


class OcclusionBlockIn(BaseModel):
    start: str = Field(..., description="遮挡开始时间，HH:MM:SS,mmm")
    end: str = Field(..., description="遮挡结束时间，HH:MM:SS,mmm，须严格晚于开始")


class PlacementRequest(BaseModel):
    srt: str = Field(..., description="原始 SRT 内容")
    top_blocks: list[OcclusionBlockIn] = Field(
        default_factory=list, description="画面上方遮挡时段"
    )
    bottom_blocks: list[OcclusionBlockIn] = Field(
        default_factory=list, description="画面下方遮挡时段"
    )
    switch_cost_ms: StrictInt = Field(
        ..., ge=0, description="相邻字幕上下换位代价（非负整数毫秒）"
    )


class BlockView(BaseModel):
    start: str
    end: str


class PlacedCueView(BaseModel):
    index: str
    start: str
    end: str
    text: str
    position: str
    top_occluded_ms: int
    bottom_occluded_ms: int
    occluded_ms: int
    still_occluded: bool


class PlacementResponse(BaseModel):
    vtt: str
    cues: list[PlacedCueView]
    still_occluded_cues: list[PlacedCueView]
    merged_top_blocks: list[BlockView]
    merged_bottom_blocks: list[BlockView]
    total_occluded_ms: int
    switch_count: int


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


@app.post("/api/placement", response_model=PlacementResponse)
def placement(req: PlacementRequest) -> PlacementResponse | JSONResponse:
    # 1. 复用现有 SRT 解析器
    try:
        cues = parse_srt(req.srt)
    except SrtParseError as exc:
        raise HTTPException(status_code=422, detail={"type": "srt", "message": str(exc)})

    # 2. 校验遮挡时段并各自求并集（换位代价由 StrictInt(ge=0) 保证为非负整数）
    try:
        top_blocks: list[Block] = parse_blocks(
            [(b.start, b.end) for b in req.top_blocks], "画面上方"
        )
        bottom_blocks: list[Block] = parse_blocks(
            [(b.start, b.end) for b in req.bottom_blocks], "画面下方"
        )
    except PlacementError as exc:
        raise HTTPException(
            status_code=422, detail={"type": "blocks", "message": str(exc)}
        )

    # 3. 动态规划选择每条字幕的位置（同分优先下方）
    placements = plan_positions(
        cues, top_blocks, bottom_blocks, req.switch_cost_ms
    )

    cue_views = [
        PlacedCueView(
            index=cue.index,
            start=format_timecode(cue.start),
            end=format_timecode(cue.end),
            text=cue.text,
            position=p.position,
            top_occluded_ms=p.top_occluded_ms,
            bottom_occluded_ms=p.bottom_occluded_ms,
            occluded_ms=p.occluded_ms,
            still_occluded=p.still_occluded,
        )
        for cue, p in zip(cues, placements)
    ]

    def block_views(blocks: list[Block]) -> list[BlockView]:
        return [
            BlockView(start=format_timecode(b.start), end=format_timecode(b.end))
            for b in blocks
        ]

    return PlacementResponse(
        vtt=export_vtt(cues, placements),
        cues=cue_views,
        still_occluded_cues=[v for v in cue_views if v.still_occluded],
        merged_top_blocks=block_views(top_blocks),
        merged_bottom_blocks=block_views(bottom_blocks),
        total_occluded_ms=sum(v.occluded_ms for v in cue_views),
        switch_count=count_switches(placements),
    )
