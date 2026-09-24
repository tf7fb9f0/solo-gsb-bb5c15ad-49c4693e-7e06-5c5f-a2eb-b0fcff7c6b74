# 字幕时间漂移校正 / 位置规划工具

基于 **Python + FastAPI** 的轻量 Web 工具，包含两个功能：

1. **时间漂移校正**：粘贴 SRT 字幕，填入多组人工确认的「原时间 / 正确时间」
   锚点，按分段线性映射校正字幕时间，在网页预览并下载校正后的 SRT。
2. **字幕位置规划**：给出画面上方/下方被遮挡的时间段与相邻字幕上下换位代价，
   为每条字幕选择上方或下方位置，在网页查看逐条结果并下载带 `line` 位置设置
   的 WebVTT。

## 功能与规则

### 时间漂移校正

- 粘贴标准 SRT，字幕**序号与文本原样保留**，仅校正开始/结束时间。
- **无损导出**：直接在原始 SRT 文本上逐条替换时间轴行的起止时间码，
  序号、字幕正文、箭头前后空格、`X1:`/`X2:`/`Y1:`/`Y2:`/`Position:` 等行尾
  定位参数、空行、原有换行（CRLF/LF 原样保留，不统一改写）以及文件开头的
  UTF-8 BOM 均保持不变；原文件无 BOM 时结果也不添加 BOM。
- 至少 **2 组**锚点（建议在片头、片中、片尾各取一组，越多越精确）。
- **相邻锚点之间做分段线性插值**；早于第一个锚点或晚于最后一个锚点的字幕，
  沿用最近一个区段（首段 / 末段）的斜率外推。
- 锚点**不自动排序**：原时间列必须严格递增，正确时间列也必须严格递增；
  任一侧出现相等或倒退时，拒绝生成并返回具体原因（指出是第几组锚点）。
- 时间格式 `HH:MM:SS,mmm`（毫秒分隔符逗号、句点均可，毫秒允许 1–3 位）。
- 校正后早于 `00:00:00,000` 的时间会截断为 0，并给出警告。
- 结果可在页面预览、逐条对比，并一键下载 `corrected.srt`：页面预览与下载
  使用接口返回的同一份 `corrected_srt`，下载文件开头**最多一个 BOM**
  （仅当原 SRT 带 BOM 时保留），为 UTF-8 编码。
- SRT 无法解析时接口返回 `422` 错误，并在 `detail.message` 中说明原因。

### 字幕位置规划

- 页面入口：<http://localhost:8000/placement>（首页顶部也有互相跳转链接）。
- 分别填写画面**上方/下方的遮挡时间段**（任一侧可留空；时间格式同样为
  `HH:MM:SS,mmm`）。同一侧重叠或首尾相接的遮挡时段会**先求并集**，
  再按并集区间与每条字幕 `[开始, 结束)` 的时间交集累计遮挡毫秒数。
- 以 **总遮挡时长 + 相邻字幕上下换位代价之和最小** 为目标，用动态规划为每条
  字幕选择位置；多个方案总分相同时**优先下方**（越靠前的字幕越优先放下方）。
- `switch_cost_ms` 是相邻两条字幕由上方换到下方（或反向）的代价（毫秒，
  **非负整数**）：越大越倾向让连续字幕待在同一侧；填 `0` 表示各条独立取遮挡
  最小的一侧。第一条字幕前面没有字幕，不计换位代价。
- 导出的 **WebVTT**（`placement.vtt`）在每条时间轴行追加 `line` 位置设置：
  上方 `line:10%`、下方 `line:90%`（百分比相对画面高度，0% 为顶边、100% 为
  底边，默认行对齐对上方为顶对齐、下方为底对齐，主流播放器/浏览器均支持）。
- 页面与接口返回逐条的上/下方遮挡时长、所选位置及该位置仍被遮挡的时长；
  所选位置仍有遮挡的字幕（`chosen_blocked_ms > 0`）会在 `occluded_indices`
  中列出并在表格中以红色标出。
- 遮挡时段起止无法解析、结束不严格晚于开始，或换位代价不是非负整数时，
  接口返回 `422`（遮挡时段/SRT 的业务错误在 `detail.message` 给出中文原因）。

## 一条命令启动（Docker Compose）

```bash
docker compose up -d --build
```

启动后访问：

- 时间漂移校正界面：<http://localhost:8000/>
- 字幕位置规划界面：<http://localhost:8000/placement>
- 健康检查：<http://localhost:8000/health>
- 接口文档（FastAPI 自带）：<http://localhost:8000/docs>

停止：`docker compose down`

### 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 端口 | 通过环境变量 `HOST_PORT` 修改宿主机映射端口；容器内固定监听 8000 | `8000` |
| 重启策略 | `restart: unless-stopped`（见 `docker-compose.yml`） | — |

例如改用宿主机 9000 端口：

```bash
HOST_PORT=9000 docker compose up -d --build
# 访问 http://localhost:9000/ 与 http://localhost:9000/placement
```

## 不用 Docker，直接运行（Python 3.10+）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

同样访问 <http://localhost:8000/> 与 <http://localhost:8000/placement>。

## HTTP 接口

### `POST /api/correct`

```json
{
  "srt": "1\n00:00:05,000 --> 00:00:08,000\n你好\n",
  "anchors": [
    {"original": "00:00:10,000", "corrected": "00:00:08,000"},
    {"original": "00:02:40,000", "corrected": "00:02:35,000"}
  ]
}
```

成功返回 `corrected_srt`（与输入同构的完整 SRT 文本：只替换各时间轴行的起止
时间码，序号、正文、定位参数、空行、CRLF/LF 换行及 BOM 原样保留）、`cues`
（逐条前后时间对照）与 `warnings`（如时间截断等提示）。SRT 解析失败或锚点不
满足严格递增时返回 `422`，并在 `detail.message` 中给出中文原因。

### `POST /api/placement`

```json
{
  "srt": "1\n00:00:05,000 --> 00:00:09,000\n第一句\n",
  "top_blocks": [
    {"start": "00:00:05,000", "end": "00:00:08,000"},
    {"start": "00:00:06,000", "end": "00:00:09,000"}
  ],
  "bottom_blocks": [
    {"start": "00:00:11,000", "end": "00:00:19,000"}
  ],
  "switch_cost_ms": 1000
}
```

同侧重叠遮挡时段先求并集（上例上方两段并为 `05,000–09,000`），再计算每条字幕
在两侧的遮挡毫秒数，按「总遮挡时长 + 相邻换位代价」最小化选位（同分优先下方）。
成功返回：

- `webvtt`：带 `line:10%`（上方）/ `line:90%`（下方）位置设置的完整 WebVTT 文本；
- `cues`：逐条结果，含 `position`（`top`/`bottom`）、`top_blocked_ms`、
  `bottom_blocked_ms`、`chosen_blocked_ms`（所选位置仍被遮挡的毫秒数，0 为不遮挡）；
- `occluded_indices`：所选位置仍被遮挡的字幕序号列表；
- `total_blocked_ms` / `total_switch_cost_ms` / `switch_count`：遮挡合计、
  换位代价合计与换位次数。

SRT 解析失败、遮挡时段起止无效或换位代价不是非负整数时返回 `422`。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 应用、/api/correct 与 /api/placement 接口
│   ├── mapping.py       # 锚点校验与分段线性映射
│   ├── placement.py     # 遮挡区间并集、遮挡时长、选位动态规划与 WebVTT 导出
│   ├── srt.py           # SRT 解析与无损重写（仅替换时间码，保留原格式）
│   ├── timecode.py      # 时间码解析与格式化
│   └── static/
│       ├── styles.css   # 两个页面共用的样式
│       ├── index.html   # 时间漂移校正页面（原生 HTML/CSS/JS）
│       └── placement.html  # 字幕位置规划页面（原生 HTML/CSS/JS）
├── tests/               # 核心算法与接口测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
