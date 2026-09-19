# Web API Usage

JavSP Web UI 提供以下 RESTful API 端点。所有返回 JSON 的端点（除非特别标注）Content-Type 为 `application/json`。

## 目录

- [任务控制](#任务控制)
- [任务管理](#任务管理)
- [运行记录](#运行记录)
- [元数据管理](#元数据管理)
- [配置管理](#配置管理)
- [爬虫管理](#爬虫管理)
- [封面与详情](#封面与详情)
- [实时状态](#实时状态)
- [SSE 实时流](#sse-实时流)

---

## 任务控制

### POST /api/run-now — 手动触发刮削

启动一次完整的扫描→入队→消费队列流程。非阻塞，立即返回。

```
POST /api/run-now
```

**响应**：
```json
// 202 — 成功
{"message": "已触发手动运行。"}

// 409 — 已有任务在运行
{"message": "另一个任务已在运行中，请稍后重试。", "status": "busy"}
```

---

### POST /api/trigger-scrape — Webhook 触发刮削

供外部下载软件（aria2、qBittorrent 等）下载完成后调用。支持 JSON / form / query 三种传参方式。

```
POST /api/trigger-scrape
```

**参数**（三者选一方式传入）：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `target_dir` | string | 否 | 目标目录，不传则使用 config.yml 默认扫描目录 |
| `target_file` | string | 否 | 仅刮削指定文件，不传则扫描整个目录 |
| `move_files` | string | 否 | 是否移动文件：`"true"`/`"1"`/`"yes"` 为移动，其他为不移动。不传则用默认配置 |

**示例**：
```bash
# JSON 方式（推荐）
curl -X POST http://localhost:5000/api/trigger-scrape \
  -H "Content-Type: application/json" \
  -d '{"target_file": "/video/MOND-306.mp4", "move_files": "false"}'

# Form 方式
curl -X POST http://localhost:5000/api/trigger-scrape \
  -d "target_file=/video/MOND-306.mp4" -d "move_files=false"

# Query 方式
curl -X POST "http://localhost:5000/api/trigger-scrape?target_file=/video/MOND-306.mp4&move_files=yes"
```

**响应**：
```json
// 202
{
  "message": "刮削任务已触发",
  "status": "accepted",
  "target_dir": "/video",
  "target_file": "/video/MOND-306.mp4",
  "move_files": false
}

// 409
{"message": "另一个任务已在运行中，请稍后重试。", "status": "busy"}
```

---

## 任务管理

### GET /api/tasks — 任务列表

```
GET /api/tasks?status=success,failed&limit=50&offset=0&order_by=updated_at&sort_dir=DESC
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `status` | string | 全部 | 逗号分隔的状态过滤，如 `"pending,deferred"` |
| `limit` | int | 200 | 每页条数 |
| `offset` | int | 0 | 分页偏移 |
| `order_by` | string | `updated_at` | 排序字段 |
| `sort_dir` | string | `DESC` | `ASC` 或 `DESC` |
| `include_results` | `"1"`/`"true"`/`"yes"` | 否 | 是否包含爬虫结果详情 |
| `check_metadata` | `"1"`/`"true"`/`"yes"` | 否 | 返回前重新检查元数据完整性 |

**响应**：
```json
{
  "tasks": [
    {
      "id": 734,
      "avid": "MOND-306",
      "status": "deferred",
      "data_src": "normal",
      "files_json": "[\"/video/MOND-306.mp4\"]",
      "save_dir": null,
      "failure_stage": "scan",
      "failure_reason": "文件大小仍在变化，等待稳定",
      "next_retry_at": 1716500000.0,
      "metadata_status": null,
      "updated_at": "2024-05-23 20:36:00"
    }
  ],
  "total": 1
}
```

**任务状态**：`pending` | `running` | `success` | `failed` | `skipped` | `deferred`

---

### GET /api/tasks/:id — 任务详情

```
GET /api/tasks/734
```

**响应**：自动包含 `configured_crawlers` 和 `crawler_results`。404 返回 `{"message": "任务不存在"}`。

---

### DELETE /api/tasks/:id — 删除任务

```
DELETE /api/tasks/734
```

删除任务记录（不删除实际影片或 NFO 文件）。404 返回 `{"message": "任务不存在"}`。

---

### POST /api/tasks/:id/retry — 重试任务

```
POST /api/tasks/734/retry
```

将 `failed` 或 `deferred` 任务重置为 `pending`。如果有待处理任务且无活跃刮削进程，**自动启动 Worker**。

**响应**：
```json
{
  "message": "任务已重新加入队列，已自动启动刮削",
  "worker_started": true
}
```
404：`{"message": "任务不存在或当前状态不允许重试"}`

---

### GET /api/tasks/:id/events — 任务事件

```
GET /api/tasks/734/events?limit=100
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 500 | 返回条数 |

---

### GET /api/tasks/:id/metadata/sources — 元数据来源

```
GET /api/tasks/734/metadata/sources
```

返回该任务的元数据来源对比信息。

---

### POST /api/tasks/batch-delete — 批量删除

```
POST /api/tasks/batch-delete
```

| JSON 参数 | 类型 | 必填 | 说明 |
|-----------|------|------|------|
| `task_ids` | int[] | 与 status 二选一 | 任务 ID 列表 |
| `status` | string | 与 task_ids 二选一 | 按状态删除，如 `"failed"` |
| `older_than_days` | int | 否 | 仅删除 N 天前的记录（配合 status） |

**示例**：
```json
// 按状态删除 7 天前的失败任务
{"status": "failed", "older_than_days": 7}

// 按 ID 删除
{"task_ids": [1, 2, 3]}
```

---

### POST /api/tasks/batch-retry — 批量重试

```
POST /api/tasks/batch-retry
{"task_ids": [1, 2, 3]}
```

重试成功后有 pending 任务且无活跃进程时自动启动 Worker。

---

### GET /api/tasks/export — 导出 CSV

```
GET /api/tasks/export
```

返回 CSV 文件下载（最多 10000 条），列：ID | 番号 | 状态 | 数据源 | 标题 | 女优 | 目录 | NFO | 更新日期 | 元数据完整性。

---

## 运行记录

### GET /api/runs/latest

返回最近一次运行记录。`Cache-Control: no-store`。

### GET /api/runs/activity

```
GET /api/runs/activity?limit=80
```

返回活动运行记录列表。

### GET /api/runs/:id

```
GET /api/runs/1?event_limit=1000
```

返回运行详情（含关联任务和事件）。404：`{"message": "运行记录不存在"}`。

### GET /api/runs/:id/events

```
GET /api/runs/1/events?task_id=42&limit=500
```

返回运行事件的过滤列表。

### GET /api/failures

```
GET /api/failures?limit=200
```

返回最近失败任务列表，用于故障面板。

---

## 元数据管理

### POST /api/metadata/check_complete

对所有 `success` 状态的任务重新判断元数据完整性。

```
POST /api/metadata/check_complete
```

**响应**：
```json
{"message": "已重新判断 42 个成功任务的完整性", "checked": 42}
```

---

### POST /api/tasks/:id/metadata/check

对单个任务重新检查元数据完整性。

```
POST /api/tasks/734/metadata/check
```

---

### POST /api/tasks/:id/metadata/refresh — 优化刷新

在后台子进程中重新爬取指定任务的元数据，完成后合并写入 NFO。

```
POST /api/tasks/734/metadata/refresh
```

202：`{"message": "任务已启动"}`  
409：`{"message": "已有任务正在运行"}`

---

### POST /api/tasks/:id/metadata/rescrape — 单片重新刮削

在后台子进程中重新爬取指定成功任务，并用本次汇总结果替换旧元数据和 NFO；会重新下载封面、生成 poster、更新剧照，不会移动影片文件。

```
POST /api/tasks/734/metadata/rescrape
```

202：`{"message": "任务已启动"}`  
409：`{"message": "已有任务正在运行"}`

---

### POST /api/metadata/refresh_incomplete — 批量优化

对 `metadata_status` 为 `"incomplete"` 的成功任务批量刷新。

```
POST /api/metadata/refresh_incomplete
```

---

### POST /api/metadata/import_legacy — 导入历史目录

```
POST /api/metadata/import_legacy
{"path": "/video/JAV/女优名/[ABC-123] 标题"}
```

| JSON 参数 | 类型 | 必填 | 说明 |
|-----------|------|------|------|
| `path` | string | 是 | 历史目录路径（相对路径以扫描目录为基准） |

**响应**：
```json
{
  "message": "历史目录导入完成：扫描 10，新增 3，更新 2，完整 6，不完整 2，失败 1",
  "summary": {
    "scanned": 10,
    "created": 3,
    "updated": 2,
    "complete": 6,
    "incomplete": 2,
    "failed": 1,
    "failures": [...]
  }
}
```

---

### POST /api/metadata/normalize_actress — 女优名归一化

```
POST /api/metadata/normalize_actress
{"path": "/video/JAV", "move": true}
```

| JSON 参数 | 类型 | 必填 | 说明 |
|-----------|------|------|------|
| `path` | string | 是 | 目标目录 |
| `move` | bool | 否 | 是否移动文件并重写 NFO |

---

### GET/POST /api/metadata_complete/config — 完整性配置

**GET**：返回 config.yml 中 `metadata_complete` 段落的 YAML 字符串。

**POST**：
```json
{"content": "enabled: true\nauto_refresh: true\nfields:\n  title:\n    required: true\n    prefer_language: zh"}
```

保存配置，如果 `metadata_complete` 段落有变化会自动批量重判。

---

## 配置管理

### GET/POST /api/general_config — 主配置

读取或保存 `config.yml` 的完整 YAML 内容。POST 保存后若 `metadata_complete` 段落变更会触发完整性批量重判。

```
GET  /api/general_config
POST /api/general_config
{"content": "scanner:\n  input_directory: /video\n  ..."}
```

### GET/POST /api/scheduler_config — 定时任务配置

读取或保存定时任务配置（APScheduler）。支持 `cron` 和 `interval` 模式。

```
GET  /api/scheduler_config
POST /api/scheduler_config
{"content": "scheduler:\n  enabled: true\n  mode: cron\n  cron:\n    time: \"02:00\""}
```

---

## 爬虫管理

### GET/POST /api/crawlers — 爬虫列表与配置

**GET**：返回可用爬虫列表、各数据源类型的爬虫选择、字段优先级配置。

```json
{
  "available": ["airav", "fanza", "javbus", "javdb", "mgstage", "prestige", ...],
  "selection": {
    "normal": ["javbus", "javdb", "airav"],
    "fc2": ["fc2", "javdb"],
    "cid": ["fanza"]
  },
  "field_priorities": {
    "title": ["javdb"],
    "plot": ["javdb"],
    "actress": ["javdb"],
    "preview_pics": ["javbus", "javdb"]
  }
}
```

**POST**：
```json
{
  "selection": {"normal": ["javdb", "javbus"]},
  "field_priorities": {"title": ["javdb"], "plot": ["javdb"], "preview_pics": ["javbus"]}
}
```

### GET /api/crawlers_by_type

```
GET /api/crawlers_by_type?type=normal
```

返回指定类型的爬虫列表：`{"crawlers": ["javbus", "javdb", ...]}`。

### GET /api/crawler_health

返回各爬虫的健康状态（成功率、熔断信息）。

### POST /api/crawlers/debug — 调试爬虫

前端调试单个爬虫的抓取结果。

```
POST /api/crawlers/debug
{"crawler_name": "javbus", "dvdid": "ABC-123"}
```

```json
{
  "success": true,
  "elapsed": 1.23,
  "dvdid": "ABC-123",
  "url": "https://www.javbus.com/ABC-123",
  "fields": {
    "title": "美丽出道",
    "plot": "...",
    "actress": ["女优A"],
    "cover": "https://...",
    "genre": ["Drama"]
  },
  "field_count": 5
}
```

错误响应：
```json
{"success": false, "error": "未找到影片", "type": "not_found", "elapsed": 0.5}
```

---

## 封面与详情

### GET /api/cover — 封面图片服务

```
GET /api/cover?path=/video/JAV/女优/[ABC-123] 标题/poster.jpg
```

**三重安全校验**：
1. `realpath()` 解析符号链接和 `../`
2. `commonpath()` 确保请求路径在扫描目录内
3. 仅放行扩展名 `.jpg` / `.jpeg` / `.png` / `.webp` / `.gif`

校验失败返回 `404 Not found`。

### GET /api/movie_detail — 影片详情

```
GET /api/movie_detail?path=/video/JAV/女优/[ABC-123] 标题&num=ABC-123
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 保存目录（相对路径以扫描目录为基准） |
| `num` | string | 否 | 番号，用于 NFO 文件定位 |

**响应**：
```json
{
  "save_dir": "/video/JAV/女优/[ABC-123] 标题",
  "num": "ABC-123",
  "title": "美丽出道",
  "original_title": null,
  "plot": "剧情简介...",
  "actress": ["女优A", "女优B"],
  "director": "导演名",
  "publisher": "发行商",
  "producer": "制作商",
  "genre": ["Drama", "Romance"],
  "premiered": "2024-01-15",
  "poster": "/api/cover?path=/video/.../poster.jpg",
  "fanart": "/api/cover?path=/video/.../fanart.jpg",
  "nfo_exists": true
}
```

---

## 实时状态

### GET /api/polling_active — 活跃状态轮询

前端轮询此接口判断是否需要显示进度条和继续查询。活跃时推荐 5 秒轮询，不活跃时可停止。

```
GET /api/polling_active
```

**响应**：
```json
{
  "active": true,
  "scraping_active": false,
  "refresh_active": true,
  "refresh_type": "refresh-incomplete",
  "refresh_running": 1,
  "refresh_starting": false,
  "tasks_pending": 3,
  "tasks_running": 0,
  "incomplete_count": 12,
  "not_checked_count": 5,
  "path_unknown_count": 2
}
```

---

### GET /api/scrape_status — SSE 刮削实时状态

```
GET /api/scrape_status
```

**响应**：
```json
{
  "total": 5,
  "completed": 2,
  "failed": 1,
  "movies": {
    "ABC-123": {
      "title": "美丽出道",
      "status": "success",
      "crawlers": {
        "javbus": {"status": "success", "fields": ["title", "cover"]},
        "javdb":  {"status": "skipped", "fields": []}
      },
      "save_dir": "/video/JAV/...",
      "num": "ABC-123",
      "data_src": "normal"
    }
  }
}
```

---

## SSE 实时流

### GET /run-app — 实时刮削 SSE 流

前端通过 EventSource 连接，服务器逐条推送结构化日志事件。

```
GET /run-app
```

**SSE 事件类型**：

| 事件 | 字段 | 说明 |
|------|------|------|
| `scan_complete` | `total` | 扫描完成，发现 N 部影片 |
| `scraping_start` | `dvdid`, `num`, `data_src`, `crawler_list` | 开始刮削某部影片 |
| `scraping_end` | `dvdid`, `status`, `save_dir` | 某部影片刮削完成 |
| `error` | `message` | 处理过程中的错误信息 |
| `finish` | `return_code`, `summary` | 全部完成，含统计摘要 |

**示例流**：
```
data: {"type": "scan_complete", "total": 3}
data: {"type": "scraping_start", "dvdid": "ABC-123", "crawler_list": ["javdb"]}
data: {"type": "scraping_end", "dvdid": "ABC-123", "status": "success", "save_dir": "/video/..."}
data: {"type": "finish", "return_code": 0, "summary": {"enqueued": 3, "deferred": 0}}
```

**JavaScript 使用**：
```javascript
const evtSource = new EventSource("/run-app");
evtSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  if (data.type === "finish") {
    evtSource.close();
  }
};
```

---

## 典型场景

### 1. 下载软件完成单个文件后触发刮削

```bash
# aria2 on-download-complete hook
curl -X POST http://localhost:5000/api/trigger-scrape \
  -H "Content-Type: application/json" \
  -d "{\"target_file\": \"$3\", \"move_files\": \"false\"}"
```

### 2. 查看所有 deferred 任务并一键重试

```bash
# 1. 列出 deferred 任务
curl "http://localhost:5000/api/tasks?status=deferred"

# 2. 找出 ID 后重试（会自动启动 Worker）
curl -X POST http://localhost:5000/api/tasks/734/retry
```

### 3. 批量清理 7 天前的失败任务

```bash
curl -X POST http://localhost:5000/api/tasks/batch-delete \
  -H "Content-Type: application/json" \
  -d '{"status": "failed", "older_than_days": 7}'
```

### 4. 定时 cron 刮削（凌晨 2 点）

```bash
curl -X POST http://localhost:5000/api/scheduler_config \
  -H "Content-Type: application/json" \
  -d '{"content": "scheduler:\n  enabled: true\n  mode: cron\n  cron:\n    time: \"02:00\""}'
```

### 5. 调试某个爬虫

```bash
curl -X POST http://localhost:5000/api/crawlers/debug \
  -H "Content-Type: application/json" \
  -d '{"crawler_name": "javbus", "dvdid": "IPX-177"}'
```

### 6. 导出任务列表

```bash
curl http://localhost:5000/api/tasks/export > javsp_tasks.csv
```
