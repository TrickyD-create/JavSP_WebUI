# 📖 JavSP 使用说明

JavSP 是一个多站点 AV 元数据刮削器。本文档涵盖 **config.yml 配置参数** 和 **Web API 接口** 的完整参考。

这是适合在 GitHub 直接阅读的使用指南。Web 服务启动后，也可以在页面中点击「📖 使用说明」查看 HTML 版。

[返回项目首页](README.md) · [配置模板](config.example.yml) · [完整 API 文档](Web%20API%20Usage.md)

## 目录

- [scanner 扫描](#config-scanner)
- [network 网络](#config-network)
- [crawler 爬虫](#config-crawler)
- [summarizer 整理](#config-summarizer)
- [translator 翻译](#config-translator)
- [daemon 后台](#config-daemon)
- [retry_policy 重试](#config-retry)
- [metadata_complete](#config-metadata)
- [notifications](#config-notifications)
- [环境变量 / CLI](#config-env)
- [任务控制](#api-control)
- [任务管理](#api-tasks)
- [运行记录](#api-runs)
- [元数据管理](#api-metadata)
- [配置 API](#api-config)
- [爬虫 API](#api-crawlers)
- [封面与详情](#api-cover)
- [实时状态 / SSE](#api-status)
- [使用场景](#scenarios)

<a id="config-scanner"></a>

## 🔍 scanner — 扫描配置

控制影片文件的扫描范围和行为。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `input_directory` | 路径 | 无 | 扫描根目录。Docker 中通常为 `/video` |
| `filename_extensions` | string[] | `[.mp4, .mkv, ...]` | 视为影片的文件后缀 |
| `ignored_id_pattern` | string[] | 见示例 | 推测番号前忽略的正则（分辨率、水印等） |
| `ignored_folder_name_pattern` | string[] | `[^\.，^#recycle$，...]` | 忽略的目录名正则 |
| `minimum_size` | ByteSize | `100MiB` | 最小文件体积，支持 `MiB`/`GiB`/`MB` |
| `skip_nfo_dir` | bool | yes | 跳过已有 NFO 的目录 |
| `manual` | bool | no | 交互模式（Web UI 强制为 false） |

环境变量覆盖：`JAVSP_SCANNER__INPUT_DIRECTORY=/mnt/media`

<a id="config-network"></a>

## 🌐 network — 网络配置

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `proxy_server` | URL / null | `null` | 代理地址，`http://` / `socks5://` / `socks5h://` |
| `retry` | int | `3` | 网络失败重试次数 |
| `timeout` | Duration | `PT10S` | 请求超时（ISO 8601 Duration 格式） |
| `javdb_cookie` | string / null | `null` | JavDB 登录 Cookie |

```
# 示例
network:
  proxy_server: 'socks5h://127.0.0.1:1080'
  retry: 5
  timeout: PT30S
```

<a id="config-crawler"></a>

## 🕷️ crawler — 爬虫配置

按影片类型配置爬虫优先级。**列表前面的优先**。

### selection

| 子字段 | 适用场景 |
| --- | --- |
| `normal` | 普通 DVD 番号（`ABC-123`） |
| `fc2` | FC2 番号（`FC2-12345`） |
| `cid` | DMM Content ID |

可用爬虫：`airav` `avsox` `avwiki` `fanza` `fc2` `fc2fan` `fc2cmadb` `javbus` `javdb` `javlib` `javmenu` `jav321` `mgstage` `prestige` `arzon` `arzon_iv` `javrate` `javguru` `njav` `supjav` `ggjav`

### field_priorities

控制标题/剧情/女优/剧照四个字段优先从哪个爬虫取值。不设置则回退到 selection 顺序。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `hardworking` | bool | `true` | 额外翻页查询更多信息 |
| `respect_site_avid` | bool | `true` | 以网站番号为准 |
| `sleep_after_scraping` | Duration | `PT1S` | 每部影片后等待间隔 |
| `use_javdb_cover` | enum | `fallback` | `yes` / `no` / `fallback` |
| `normalize_actress_name` | bool | `true` | 统一女优艺名 |
| `required_keys` | string[] | `[cover,title,actress]` | 最少需获取的字段 |

<a id="config-summarizer"></a>

## 📂 summarizer — 整理/命名配置

### 模板变量

| 变量 | 说明 | 示例值 |
| --- | --- | --- |
| `{num}` | 番号 | `ABC-123` |
| `{title}` | 标题 | `美丽出道` |
| `{rawtitle}` | 原始标题（未处理） | `美丽出道` |
| `{actress}` | 女优名，逗号分隔 | `女优A，女优B` |
| `{score}` | 评分 | `8.5` |
| `{censor}` | 有码/无码 | `有码` |
| `{serial}` | 系列 | `超级系列` |
| `{director}` | 导演 | `某导演` |
| `{producer}` | 制作商 | `某制作商` |
| `{publisher}` | 发行商 | `某发行商` |
| `{date}` | 发行日期 | `2024-01-15` |
| `{year}` | 发行年份 | `2024` |
| `{label}` | 番号前缀 | `ABC` |
| `{genre}` | 分类，逗号分隔 | `Drama，Romance` |

### 核心字段

| 字段 | 说明 |
| --- | --- |
| `move_files` | `true` 移动文件到新目录；`false` 在原目录生成 NFO |
| `path.output_folder_pattern` | 输出目录模板，默认 `JAV/{actress}/[{num}] {title}` |
| `path.basename_pattern` | 文件名，默认 `{num}` |
| `path.length_maximum` | 路径最大长度，超长截短标题 |
| `path.hard_link` | 硬链接（节省空间，需文件系统支持） |
| `nfo.title_pattern` | NFO 显示标题，默认 `{num} {title}` |
| `cover.highres` | 是否下载高清封面 |
| `extra_fanarts.enabled` | 是否下载剧照 |

<a id="config-translator"></a>

## 🌏 translator — 翻译配置

| 引擎 | name | 需要参数 |
| --- | --- | --- |
| Google（免费） | `google` | 无 |
| 百度 | `baidu` | `app_id`、`api_key` |
| 必应 (Azure) | `bing` | `api_key` |
| Claude | `claude` | `api_key` |
| OpenAI / Groq | `openai` | `url`、`api_key`、`model` |
| 禁用 | `null` | — |

```
# Google 免费翻译
translator:
  engine:
    name: google
  fields:
    title: true
    plot: true
```

<a id="config-daemon"></a>

## ⚙️ daemon — 后台任务配置

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 是否启用常驻后台 |
| `state_db` | 路径 | `data/javsp_state.db` | SQLite 状态库。Docker 中建议 `/video/.javsp_state.db` |
| `scan_interval` | Duration | `PT30M` | 扫描间隔 |
| `max_movies_per_run` | int | `0` | 每轮最多处理数，0 不限制 |
| `worker_interval` | Duration | `PT30S` | Worker 空闲轮询间隔 |
| `download_stable_seconds` | int | `300` | 文件稳定等待时间。临时文件过期阈值为此值 ×2（默认 600s） |

Docker 部署时 `state_db` 建议放 `/video/`，重建容器后不丢失状态。

<a id="config-retry"></a>

## 🔄 retry_policy — 重试策略

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `network_retry_after` | Duration | `PT1H` | 网络失败后重试间隔 |
| `metadata_retry_after` | Duration | `P1D` | 元数据不完整后重试间隔 |
| `max_retry_count` | int | `3` | 单任务最多重试次数 |
| `crawler_circuit_break_threshold` | int | `5` | 爬虫连续失败 N 次触发熔断 |
| `crawler_circuit_break_duration` | Duration | `PT6H` | 熔断持续时长 |

<a id="config-metadata"></a>

## ✅ metadata_complete — 元数据完整性

判断成功任务是否需要后续优化刷新，**不影响刮削成功线**。

| 字段 | 配置项 | 说明 |
| --- | --- | --- |
| `title` | `required: true` | prefer_language: zh，min_cjk_ratio: 0.3 |
| `plot` | `required: true` | prefer_language: zh，min_cjk_ratio: 0.5，min_length: 20 |
| `score` | `required: false` | — |
| `genre` | `required: false` | min_items: 2 |
| `preview_pics` | `required: false` | min_items: 3 |
| `actress_pics` | `required: false` | min_items: 1 |
| `director` | `required: false` | — |
| `duration` | `required: false` | — |
| `producer` | `required: false` | — |
| `publisher` | `required: false` | — |
| `publish_date` | `required: false` | — |

<a id="config-notifications"></a>

## 🔔 notifications — 通知

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | 是否启用 |
| `webhook_url` | URL / null | `null` | Webhook 地址 |
| `notify_on_success` | bool | `true` | 成功时是否通知 |
| `notify_on_failure` | bool | `true` | 失败时是否通知 |

<a id="config-env"></a>

## 🔧 环境变量 & CLI 覆盖

```bash
# 环境变量（前缀 JAVSP_，双下划线分隔层级）
JAVSP_SCANNER__INPUT_DIRECTORY=/mnt/media
JAVSP_DAEMON__DOWNLOAD_STABLE_SECONDS=600

# CLI 覆盖
python -m javsp run-once -c config.yml \
    -o scanner.input_directory=/mnt \
    -o daemon.max_movies_per_run=10
```

<a id="api-control"></a>

## 🚀 任务控制 API

### POST /api/run-now

启动完整扫描→入队→消费队列。非阻塞，立即返回。

202：`{"message": "已触发手动运行。"}`  |  409：已在运行中

### POST /api/trigger-scrape

下载软件 Webhook 调用。支持 JSON / form / query 三种传参。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `target_dir` | string | 目标目录（不传用默认） |
| `target_file` | string | 仅刮削指定文件（不传扫描整个目录） |
| `move_files` | string | 是否移动文件：`"true"`/`"1"`/`"yes"` |

```bash
# JSON 方式（推荐）
curl -X POST http://localhost:5000/api/trigger-scrape \
  -H "Content-Type: application/json" \
  -d '{"target_file": "/video/MOND-306.mp4", "move_files": "false"}'

# Form 方式
curl -X POST http://localhost:5000/api/trigger-scrape \
  -d "target_file=/video/MOND-306.mp4" -d "move_files=false"
```

<a id="api-tasks"></a>

## 📊 任务管理 API

### GET /api/tasks

分页任务列表。参数：`status`（逗号分隔）、`limit`（200）、`offset`、`order_by`、`sort_dir`、`include_results`、`check_metadata`。

任务状态：`pending` | `running` | `success` | `failed` | `skipped` | `deferred`

### POST /api/tasks/:id/retry

将 failed/deferred 重置为 pending。**重试成功且有 pending 任务时自动启动 Worker。**

```bash
curl -X POST http://localhost:5000/api/tasks/734/retry
```

### 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/tasks/:id` | 任务详情（含爬虫结果） |
| DELETE | `/api/tasks/:id` | 删除任务记录，不删文件 |
| GET | `/api/tasks/:id/events` | 任务事件列表 |
| GET | `/api/tasks/:id/metadata/sources` | 元数据来源对比 |
| POST | `/api/tasks/:id/metadata/rescrape` | 原有自动重新刮削（保持兼容） |
| POST | `/api/tasks/:id/metadata/rescrape/interactive` | 启动自由选择候选抓取 |
| GET | `/api/rescrape-sessions/:id` | 读取或恢复自由选择会话 |
| POST | `/api/rescrape-sessions/:id/apply` | 提交最终字段并开始写入及媒体处理 |
| POST | `/api/rescrape-sessions/:id/cancel` | 取消待选择会话，不修改原文件 |
| POST | `/api/tasks/batch-delete` | 批量删除（按 ID 或状态） |
| POST | `/api/tasks/batch-retry` | 批量重试 |
| GET | `/api/tasks/export` | 导出 CSV |

<a id="api-runs"></a>

## 📈 运行记录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/runs/latest` | 最新运行记录 |
| GET | `/api/runs/activity` | 活动运行列表 |
| GET | `/api/runs/:id` | 运行详情（含任务/事件） |
| GET | `/api/failures` | 失败任务列表 |

<a id="api-metadata"></a>

## 📝 元数据管理 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/metadata/check_complete` | 批量重判完整性 |
| POST | `/api/tasks/:id/metadata/check` | 单任务重判 |
| POST | `/api/tasks/:id/metadata/refresh` | 优化刷新单个任务元数据 |
| POST | `/api/metadata/refresh_incomplete` | 批量优化不全任务 |
| POST | `/api/metadata/import_legacy` | 导入历史目录元数据 |
| POST | `/api/metadata/normalize_actress` | 女优名归一化<br>参数: `path`(string,必填) — 目标目录; `move`(bool,可选) — 是否移动文件 |

<a id="api-config"></a>

## ⚙️ 配置 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/general_config` | 读写 `config.yml` 主配置 |
| GET/POST | `/api/scheduler_config` | 读写定时任务配置（刮削+优化两套独立调度） |
| GET/POST | `/api/metadata_complete/config` | 读写元数据完整性配置 |
| GET/POST | `/api/crawlers` | 读写爬虫选择/优先级 |

<a id="api-crawlers"></a>

## 🕷️ 爬虫 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/crawlers_by_type?type=normal` | 按类型获取爬虫列表 |
| GET | `/api/crawler_health` | 爬虫健康状态（成功率/熔断） |
| POST | `/api/crawlers/debug` | **调试爬虫**：指定爬虫和番号，返回抓取结果 |

### POST /api/crawlers/debug

```
curl -X POST http://localhost:5000/api/crawlers/debug \
  -H "Content-Type: application/json" \
  -d '{"crawler_name": "javbus", "dvdid": "IPX-177"}'

// 返回
{"success": true, "elapsed": 1.23,
 "fields": {"title": "...", "actress": [...], "cover": "..."}}
```

<a id="api-cover"></a>

## 🖼️ 封面与详情

### GET /api/cover

安全封面图片服务。参数：`path`（图片路径）。

**三重安全校验**：① `realpath()` 防路径穿越 ② `commonpath()` 限扫描目录内 ③ 仅放行 `.jpg/.jpeg/.png/.webp/.gif`

### GET /api/movie_detail

从 NFO 文件读取影片详情。参数：`path`（目录）、`num`（番号）。

```
// 返回
{"title": "美丽出道", "actress": [...], "genre": [...],
 "poster": "/api/cover?path=...", "fanart": "/api/cover?path=..."}
```

<a id="api-status"></a>

## 📡 实时状态 / SSE

### GET /api/polling_active

前端轮询，返回 11 个状态字段：`active`、`scraping_active`、`refresh_active`、`tasks_pending`、`tasks_running`、`incomplete_count` 等。

### GET /api/scrape_status

返回当前 SSE 刮削的实时状态：`total`、`completed`、`failed`、`movies`。

### GET /run-app — SSE 实时流

EventSource 连接，节点推送：`scan_complete` → `scraping_start` → `scraping_end` → `finish`。

<a id="scenarios"></a>

## 🎯 典型使用场景

### 1. 下载软件完成单个文件后触发刮削

```bash
# aria2 on-download-complete hook
curl -X POST http://localhost:5000/api/trigger-scrape \
  -H "Content-Type: application/json" \
  -d "{\"target_file\": \"$3\", \"move_files\": \"false\"}"
```

### 2. 查看 deferred 任务并一键重试

```bash
# 列出 deferred 任务
curl "http://localhost:5000/api/tasks?status=deferred"

# 重试（会自动启动 Worker）
curl -X POST http://localhost:5000/api/tasks/734/retry
```

### 3. 批量清理 7 天前的失败任务

```bash
curl -X POST http://localhost:5000/api/tasks/batch-delete \
  -H "Content-Type: application/json" \
  -d '{"status": "failed", "older_than_days": 7}'
```

### 4. 设置凌晨 2 点定时刮削

```bash
curl -X POST http://localhost:5000/api/scheduler_config \
  -H "Content-Type: application/json" \
  -d '{"content": "scheduler:\n  enabled: true\n  mode: cron\n  cron:\n    time: \"02:00\""}'
```

### 5. 设置每 24 小时定时优化

```bash
curl -X POST http://localhost:5000/api/scheduler_config \
  -H "Content-Type: application/json" \
  -d '{"content": "scheduler:\n  enabled: true\n  mode: interval\n  interval:\n    hours: 6\n\nrefresh_scheduler:\n  enabled: true\n  mode: interval\n  interval:\n    hours: 24"}'
```

### 6. 调试单个爬虫

```bash
curl -X POST http://localhost:5000/api/crawlers/debug \
  -H "Content-Type: application/json" \
  -d '{"crawler_name": "javbus", "dvdid": "IPX-177"}'
```

### 7. 导出任务列表

```bash
curl http://localhost:5000/api/tasks/export > javsp_tasks.csv
```
