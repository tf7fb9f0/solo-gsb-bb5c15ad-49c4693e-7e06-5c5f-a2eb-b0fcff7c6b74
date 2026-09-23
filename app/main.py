"""字幕时间漂移校正 Web 服务（FastAPI）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .mapping import AnchorError, PiecewiseMapping, build_anchors
from .srt import SrtParseError, parse_srt, rewrite_srt
from .timecode import format_signed_timecode, format_timecode

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="字幕时间漂移校正工具",
    description="粘贴 SRT 与多组原时间/正确时间锚点，按分段线性映射校正字幕时间。",
    version="1.0.0",
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
