# Config Usage

JavSP 使用 `config.yml` 作为主配置文件。首次运行时会自动从 `config.example.yml` 复制生成。Pydantic v2 验证，多余字段静默忽略（`extra='ignore'`）。

格式要求见 [ISO 8601 Duration](https://en.wikipedia.org/wiki/ISO_8601#Durations)（如 `PT10S` = 10秒, `PT1H` = 1小时, `P1D` = 1天）。

## 目录

- [scanner — 扫描配置](#scanner)
- [network — 网络配置](#network)
- [crawler — 爬虫配置](#crawler)
- [summarizer — 整理/命名配置](#summarizer)
- [translator — 翻译配置](#translator)
- [other — 其他配置](#other)
- [daemon — 后台任务配置](#daemon)
- [retry_policy — 重试策略](#retry_policy)
- [metadata_complete — 元数据完整性判断](#metadata_complete)
- [notifications — 通知配置](#notifications)

---

## scanner

控制影片文件的扫描范围和行为。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `input_directory` | 路径 | 无 | 扫描根目录。Docker 中通常为 `/video` |
| `filename_extensions` | string[] | `[.mp4, .mkv, ...]` | 视为影片的文件后缀 |
| `ignored_id_pattern` | string[] | 见示例 | 推测番号前忽略的正则（如分辨率、站点水印） |
| `ignored_folder_name_pattern` | string[] | `[^\.，^#recycle$，...]` | 忽略的目录名正则 |
| `minimum_size` | ByteSize | `100MiB` | 小于此大小的文件不处理。支持 `MiB`/`GiB`/`MB` 等 |
| `skip_nfo_dir` | bool | `yes` | 是否跳过已有 NFO 的目录 |
| `manual` | bool | `no` | 是否交互式确认番号（Web UI 中强制为 false） |
| `restrict_to_files` | string[] | 无 | **Web API 专用**：限制只处理指定文件。正常使用不需要配置 |

**示例**：
```yaml
scanner:
  input_directory: /video
  filename_extensions: [.mp4, .mkv, .avi, .wmv]
  minimum_size: 200MiB
  ignored_folder_name_pattern: ['^\.', '^#recycle$', '^JAV$']
```

**环境变量覆盖**：`JAVSP_SCANNER_INPUT_DIRECTORY=/mnt/media`

---

## network

控制网络请求行为。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `proxy_server` | URL 或 null | `null` | 代理地址，支持 `http://` / `socks5://` / `socks5h://` |
| `retry` | int | `3` | 网络失败重试次数 |
| `timeout` | Duration | `PT10S` | 请求超时时间 |
| `javdb_cookie` | string 或 null | `null` | JavDB 登录 Cookie，格式 `_jdb_session=xxx; locale=zh; over18=1` |

**示例**：
```yaml
network:
  proxy_server: 'socks5h://127.0.0.1:1080'
  retry: 5
  timeout: PT30S
  javdb_cookie: '_jdb_session=xxx; over18=1'
```

---

## crawler

控制数据源爬虫的选用和细节。

### selection

按影片类型配置爬虫优先级（**前面的优先**）：

| 子字段 | 适用场景 |
|--------|---------|
| `normal` | 普通 DVD 番号（如 ABC-123） |
| `fc2` | FC2 番号（如 FC2-12345） |
| `cid` | DMM Content ID（如 cid00888） |

**可用爬虫名**：`airav` / `avsox` / `avwiki` / `fanza` / `fc2` / `fc2fan` / `fc2cmadb` / `javbus` / `javdb` / `javlib` / `javmenu` / `jav321` / `mgstage` / `prestige` / `arzon` / `arzon_iv` / `javrate` / `javguru` / `njav` / `supjav` / `ggjav`

### field_priorities

控制特定字段从哪个爬虫取（**按字段级别**）。不设置则回退到 `selection` 的整体顺序。

四个可控字段：`title`、`plot`、`actress`、`preview_pics`。

### 其他字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `required_keys` | string[] | `[cover，title，actress]` | 最少要获取到的字段 |
| `hardworking` | bool | `true` | 努力模式：额外翻页/组合查询获取更多信息 |
| `respect_site_avid` | bool | `true` | 以网站番号为准（纠正大小写等） |
| `sleep_after_scraping` | Duration | `PT1S` | 刮削每部影片后的等待间隔，设 `PT0S` 禁用 |
| `use_javdb_cover` | enum | `fallback` | JavDB 封面策略：`yes` / `no` / `fallback`（其他站拿不到才用） |
| `normalize_actress_name` | bool | `true` | 是否统一女优艺名 |
| `fc2fan_local_path` | 路径或 null | `null` | FC2Fan 本地镜像路径 |

**示例**：
```yaml
crawler:
  selection:
    normal: [javdb, javbus, airav]
    fc2: [javdb, fc2]
    cid: [fanza]
  field_priorities:
    title: [javdb]
    plot: [javdb, airav]
    actress: []
    preview_pics: [javbus, javdb]
  required_keys: [cover, title]
  hardworking: true
  normalize_actress_name: true
  sleep_after_scraping: PT2S
```

---

## summarizer

控制文件整理、命名、NFO 生成、封面下载等。**最复杂的配置段**。

### 模板变量

以下字段中可使用变量，花括号括起来（如 `{num}`、`{title}`）：

| 变量 | 说明 | 示例值 |
|------|------|--------|
| `{num}` | 番号 | `ABC-123` |
| `{title}` | 标题 | `美丽出道` |
| `{rawtitle}` | 原始标题（未处理） | `美丽出道` |
| `{actress}` | 女优名，逗号分隔 | `女优A，女优B` |
| `{score}` | 评分 | `8.5` |
| `{censor}` | 有码/无码标识 | `有码` |
| `{serial}` | 系列 | `超级系列` |
| `{director}` | 导演 | `某导演` |
| `{producer}` | 制作商 | `某制作商` |
| `{publisher}` | 发行商 | `某发行商` |
| `{date}` | 发行日期 | `2024-01-15` |
| `{year}` | 发行年份 | `2024` |
| `{label}` | 番号前缀 | `ABC` |
| `{genre}` | 分类，逗号分隔 | `Drama，Romance` |
| `{actor}` | 首个女优名 | `女优A` |

完整命名规则文档：https://github.com/Yuukiy/JavSP/wiki/NamingRule

---

### move_files

| 字段 | 类型 | 说明 |
|------|------|------|
| `move_files` | bool | `true`：移动文件到新目录；`false`：在原目录生成 NFO |

---

### path — 路径命名

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output_folder_pattern` | string | `JAV/{actress}/[{num}] {title}` | 输出目录模板 |
| `basename_pattern` | string | `{num}` | 文件名（不含扩展名） |
| `length_maximum` | int | `250` | 路径最大长度，超长自动截短标题 |
| `length_by_byte` | bool | `true` | `true`：按字节计算长度；`false`：按字符 |
| `max_actress_count` | int | `10` | 路径中 `{actress}` 最多包含几位女优 |
| `hard_link` | bool | `false` | 是否用硬链接（节省空间，需文件系统支持） |

**示例**：
```yaml
  path:
    output_folder_pattern: 'JAV/{actress}/[{num}] {title}'
    basename_pattern: '{num}'
    length_maximum: 250
    length_by_byte: true
    hard_link: false
```

---

### title — 标题处理

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `remove_trailing_actor_name` | bool | `true` | 删除标题尾部可能存在的女优名 |

---

### default — 缺失字段的默认值

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `title` | `#未知标题` | |
| `actress` | `#未知女优` | |
| `series` | `#未知系列` | |
| `director` | `#未知导演` | |
| `producer` | `#未知制作商` | |
| `publisher` | `#未知发行商` | |

---

### nfo — NFO 文件生成

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `basename_pattern` | string | `movie` | NFO 文件名（不含 `.nfo`） |
| `title_pattern` | string | `{num} {title}` | NFO 中 `<title>` 标签内容（Kodi/Jellyfin 显示标题） |
| `custom_genres_fields` | string[] | `[{genre}，{censor}]` | 写入 NFO `<genre>` 的额外字段 |
| `custom_tags_fields` | string[] | `[{genre}，{censor}]` | 写入 NFO `<tag>` 的额外字段 |
| `include_actor_tmdbid` | bool | `true` | 是否为演员生成 TMDBID |

---

### censor_options_representation

| 字段 | 类型 | 说明 |
|------|------|------|
| `censor_options_representation` | string[3] | 依次对应：已知无码 / 已知有码 / 不确定 的 `{censor}` 显示文本 |

**示例**：
```yaml
  censor_options_representation: ['无码', '有码', '打码情况未知']
```

---

### cover — 封面配置

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `basename_pattern` | string | `poster` | 封面文件名（不含扩展名） |
| `highres` | bool | `true` | 是否尝试下载高清封面 |
| `add_label` | bool | `false` | 是否在封面上添加水印标签 |

---

### fanart — 横版封面

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `basename_pattern` | string | `fanart` | 横版封面文件名 |

---

### extra_fanarts — 剧照下载

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 是否下载剧照 |
| `scrap_interval` | Duration | `PT3S` | 剧照爬取间隔 |

---

### 完整示例

```yaml
summarizer:
  move_files: true
  path:
    output_folder_pattern: 'JAV/{actress}/[{num}] {title}'
    basename_pattern: '{num}'
    length_maximum: 250
  nfo:
    title_pattern: '{num} {title}'
    custom_genres_fields: ['{genre}']
    include_actor_tmdbid: true
  cover:
    basename_pattern: "poster"
    highres: true
  extra_fanarts:
    enabled: true
    scrap_interval: PT5S
```

---

## translator

控制标题和剧情的机器翻译。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `engine` | 对象 或 null | `null` | 翻译引擎配置，`null` 禁用翻译 |

### 内置引擎

**Google**（免费，无需密钥）：
```yaml
translator:
  engine:
    name: google
  fields:
    title: true
    plot: true
```

**百度**：
```yaml
  engine:
    name: baidu
    app_id: '你的APP ID'
    api_key: '你的密钥'
```

**必应（Azure）**：
```yaml
  engine:
    name: bing
    api_key: '你的Azure密钥'
```

**Claude（Anthropic）**：
```yaml
  engine:
    name: claude
    api_key: 'sk-ant-xxx'
```

**OpenAI（兼容 Groq 等）**：
```yaml
  engine:
    name: openai
    url: 'https://api.openai.com/v1/chat/completions'
    api_key: 'sk-xxx'
    model: gpt-3.5-turbo
```

### fields — 翻译字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `title` | bool | 是否翻译标题 |
| `plot` | bool | 是否翻译剧情 |

---

## other

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `interactive` | bool | `true` | 是否允许交互式输入（CLI 模式） |

---

## daemon

控制后台常驻和任务队列行为。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 是否启用常驻后台（`javsp daemon` 命令使用） |
| `state_db` | 路径 | `data/javsp_state.db` | SQLite 状态库路径。Docker 中建议 `/video/.javsp_state.db` |
| `scan_interval` | Duration | `PT30M` | 两轮扫描的间隔时间 |
| `worker_interval` | Duration | `PT30S` | Worker 空闲轮询间隔（预留） |
| `max_movies_per_run` | int | `0` | 每轮最多处理的影片数，`0` 不限制 |
| `download_stable_seconds` | int | `300` | 文件大小稳定等待时间。临时文件过期检测阈值为此值的 2 倍（默认 600s） |

**示例**：
```yaml
daemon:
  enabled: false
  state_db: /video/.javsp_state.db
  scan_interval: PT1H
  max_movies_per_run: 50
  download_stable_seconds: 600
```

**注意**：
- Docker 部署时 `state_db` 建议放在 `/video/` 下，重建容器后任务状态不丢失
- `download_stable_seconds` 直接影响 deferred 任务的行为：入队后等待此时长才可能被出队；遗留 `.aria2` 等临时文件超过 `2×download_stable_seconds` 未修改会被视为过期残留，不再阻止刮削

---

## retry_policy

控制失败重试和爬虫熔断。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `network_retry_after` | Duration | `PT1H` | 网络失败后的重试间隔 |
| `metadata_retry_after` | Duration | `P1D` | 元数据不完整后的重试间隔 |
| `max_retry_count` | int | `3` | 单个任务最多自动重试次数 |
| `crawler_circuit_break_threshold` | int | `5` | 爬虫连续失败多少次触发熔断 |
| `crawler_circuit_break_duration` | Duration | `PT6H` | 爬虫熔断持续时长 |

**两种重试延迟的分工**：
- `network_retry_after`：爬虫连接失败、超时 → 短暂等待后重试
- `metadata_retry_after`：刮削完成但元数据不完整 → 较长时间后重爬

**示例**：
```yaml
retry_policy:
  network_retry_after: PT30M
  metadata_retry_after: P3D
  max_retry_count: 5
  crawler_circuit_break_threshold: 10
  crawler_circuit_break_duration: PT12H
```

---

## metadata_complete

控制影片元数据完整性的判断标准。**不影响刮削是否成功，只决定成功任务是否需要后续优化刷新**。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 是否启用完整性判断 |
| `auto_refresh` | bool | `true` | 是否自动优化不完整任务 |
| `refresh_after` | Duration | `P7D` | 同一任务自动优化的最短间隔 |

### fields — 字段级完整性规则

每个字段的配置项：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `required` | bool | 该字段是否必须存在 |
| `prefer_language` | `"zh"` 或 null | 偏好的语言 |
| `min_cjk_ratio` | float (0~1) | CJK 字符比例最小值 |
| `min_length` | int | 字符串最短长度 |
| `min_items` | int | 列表/字典最少元素数 |
| `reject_values` | string[] | 拒绝的内容值（如 `"暂无简介"`） |
| `allow_overwrite` | bool | 刷新时是否允许覆盖已有值 |

**默认字段规则**：

| 字段 | required | 附加规则 |
|------|----------|---------|
| `title` | true | `prefer_language: zh`，`min_cjk_ratio: 0.3`，`allow_overwrite: true` |
| `plot` | true | `prefer_language: zh`，`min_cjk_ratio: 0.5`，`min_length: 20`，`reject_values: [暂无简介，...]` |
| `score` | false | |
| `genre` | false | `min_items: 2` |
| `preview_pics` | false | `min_items: 3` |
| `director` | false | |
| `duration` | false | |
| `producer` | false | |
| `publisher` | false | |
| `publish_date` | false | |
| `actress_pics` | false | `min_items: 1` |

**示例**：把标题和剧情都设为可选，只要求有封面和女优：
```yaml
metadata_complete:
  enabled: true
  auto_refresh: false
  fields:
    title:
      required: false
    plot:
      required: false
    cover:
      required: true
    actress:
      required: true
      min_items: 2
```

---

## notifications

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 是否启用通知 |
| `webhook_url` | URL 或 null | `null` | Webhook 地址，POST `{"text": "..."，"run": {...}}` |
| `notify_on_success` | bool | `true` | 成功时是否通知 |
| `notify_on_failure` | bool | `true` | 失败时是否通知 |

---

## 环境变量覆盖

所有配置字段都可以通过环境变量覆盖，前缀 `JAVSP_`，用双下划线分隔层级：

```bash
JAVSP_SCANNER__INPUT_DIRECTORY=/mnt/media
JAVSP_NETWORK__PROXY_SERVER=socks5://127.0.0.1:1080
JAVSP_DAEMON__DOWNLOAD_STABLE_SECONDS=600
JAVSP_METADATA_COMPLETE__FIELDS__TITLE__REQUIRED=false
```

---

## CLI 覆盖

通过 `-o` 参数覆盖（仅 CLI 模式）：

```bash
python -m javsp run-once -c config.yml -o scanner.input_directory=/mnt -o daemon.max_movies_per_run=10
```

---

## 配置文件自动生成

如果 `config.yml` 不存在，程序自动从 `config.example.yml` 复制生成。`config.example.yml` 是**权威模板**，修改默认值请更新它。

## Pydantic 兼容性

- 从模型删除字段是向后兼容的——旧配置文件中的多余字段会被**静默忽略**（`extra='ignore'`）
- 新增字段需要默认值，否则旧配置文件启动会报 ValidationError
- `Cfg()` 是单例，在配置源（文件、环境变量、CLI）就绪后才能调用
- Web UI 通过临时配置文件强制 `scanner.manual = false`（子进程无 stdin）
