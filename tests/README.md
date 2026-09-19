# tests/ — JavSP 单元测试

## 运行

```bash
.venv/bin/python -m pytest tests/ -v              # 全部
.venv/bin/python -m pytest tests/test_task.py -v  # 单文件
.venv/bin/python -m pytest tests/ -k "deferred"   # 按名称过滤
```

无其他依赖，pytest + tmp_path 即可，不需要服务或外部网络。

## 公共 fixture（conftest.py）

| fixture | 类型 | 用途 |
|---------|------|------|
| `task_store` | `TaskStore` | 隔离 SQLite，路径 `tmp_path/test_state.db` |

### 辅助函数

所有函数放在 `conftest.py`，测试文件直接 `from conftest import xxx` 或通过 pytest 自动发现。

**`make_cfg(**overrides)`** — 构造 mock `Cfg` 配置。返回 `SimpleNamespace`，覆盖部分字段：

```python
from conftest import make_cfg
import javsp.task as task_module

# 覆盖下载稳定时间为 0
cfg = make_cfg(daemon=SimpleNamespace(download_stable_seconds=0))
monkeypatch.setattr(task_module, "Cfg", lambda: cfg)
```

**`make_movie(avid, tmp_path, filename=None, size=...)`** — 创建含视频文件的 `Movie` 对象：

```python
from conftest import make_movie
movie = make_movie("ABC-123", tmp_path, size=1024 * 1024)
# movie.files = ["/tmp/.../ABC-123.mp4"]
# movie.save_dir = "/tmp/..."
```

**`_seed_pending_task(store, movie, trigger="test")`** — 创建 pending 状态的任务，返回 `(task_id, task_dict)`。

## 核心模式

### 模式 1：Mock Cfg（配置注入）

`Cfg()` 是单例，测试中必须 monkeypatch。任务系统所有需要配置的函数都在 `javsp.task` 模块内调用 `Cfg()`，因此 monkeypatch 目标始终是 `javsp.task.Cfg`（或 `task_module.Cfg`）：

```python
import javsp.task as task_module
from types import SimpleNamespace

def test_xxx(monkeypatch):
    monkeypatch.setattr(task_module, "Cfg", lambda: SimpleNamespace(
        daemon=SimpleNamespace(download_stable_seconds=300),
        retry_policy=SimpleNamespace(
            max_retry_count=3,
            network_retry_after=SimpleNamespace(total_seconds=lambda: 3600),
            metadata_retry_after=SimpleNamespace(total_seconds=lambda: 86400),
        ),
        scanner=SimpleNamespace(minimum_size=100 * 1024 * 1024),
    ))
```

**要点**：
- `Cfg()` 返回的是 `SimpleNamespace`，不是真正的 Pydantic 模型
- Duration 类型字段（如 `network_retry_after`）需要提供 `.total_seconds()` 方法
- monkeypatch 默认作用于整个测试函数；类级别用 `@pytest.fixture(autouse=True)` 对每个方法生效

### 模式 2：写入测试文件（`write_bytes` 不是 `write_text`）

始终用 `write_bytes` 避免平台差异：

```python
video = tmp_path / "movie.mp4"
video.write_bytes(b"0" * 1024)          # 1KB 文件
video.write_bytes(b"0" * (200 * 1024 * 1024))  # 200MB 文件（超过最小体积）
```

创建临时标记文件：
```python
marker = tmp_path / "movie.mp4.aria2"
marker.write_bytes(b"")                  # aria2 控制文件
```

控制文件时间戳（测试 stale 检测）：
```python
os.utime(marker, (time.time() - 601, time.time() - 601))  # 设为 601 秒前
```

### 模式 3：创建任务并操作状态

任务状态流转通过 `TaskStore` 方法：

```python
run_id = store.start_run("test")

# 入队
_, task_id = store.enqueue_movie(movie, run_id, TASK_PENDING)   # pending
_, task_id = store.enqueue_movie(movie, run_id, TASK_DEFERRED, "下载中")  # deferred

# 状态转换
store.mark_running(task_id, run_id)        # pending → running
store.mark_success(task_id, save_dir, movie)  # → success
store.mark_failure(task_id, "crawl", "err", retryable=True)   # → failed
store.mark_deferred(task_id, "文件变化中")   # → deferred

# 查询
task = store.get_task(task_id)             # 单任务
due = store.get_due_tasks()                # 可出队任务
store.retry_task(task_id)                  # failed/deferred → pending
```

### 模式 4：Web API 测试（Flask test client）

```python
import web_ui.web_server as web_server

monkeypatch.setattr(web_server, "get_task_store", lambda: store)
monkeypatch.setattr(web_server, "get_scan_directory", lambda: str(tmp_path))
monkeypatch.setattr(web_server, "reload_javsp_config_cache", lambda: None)

client = web_server.app.test_client()
res = client.get("/api/cover?path=xxx")
assert res.status_code == 200
```

**注意事项**：
- `trigger-scrape` 测试涉及 `process_lock`，`_setup` 中需处理锁状态，避免 `RuntimeError: release unlocked lock`
- 写入配置的测试（如 `general_config`）必须用临时 `GENERAL_CONFIG_FILE`，避免污染真实 `config.yml` 导致后续 `Cfg()` 初始化失败
- `MovieInfo` 没有 `nfo_title` 默认属性，直接调用 `write_nfo()` 前需手动设置

### 模式 5：NFO 写入测试

```python
from javsp.nfo import write_nfo
from javsp import nfo as nfo_module
from lxml import etree
from types import SimpleNamespace

monkeypatch.setattr(nfo_module, "Cfg", lambda: SimpleNamespace(
    summarizer=SimpleNamespace(
        nfo=SimpleNamespace(custom_genres_fields=[], custom_tags_fields=[], ...),
    ),
))
# nfo_title 由 prepare_info_for_nfo() 设置，直接调用 write_nfo 时需手动赋值
info.nfo_title = info.title
write_nfo(info, str(nfo_path))
root = etree.parse(str(nfo_path)).getroot()
```

### 模式 6：MovieInfo.get_info_dic 测试

```python
from javsp import datatype as datatype_module

monkeypatch.setattr(datatype_module, "Cfg", lambda: SimpleNamespace(
    summarizer=SimpleNamespace(
        default=SimpleNamespace(title="#未知标题", ...),
        censor_options_representation=["无码", "有码", "打码情况未知"],
    ),
))
info = MovieInfo("ABC-123")
info.title = "标题"
dic = info.get_info_dic()
assert dic["num"] == "ABC-123"

## 测试文件组织

| 文件 | 内容 | 用例数 |
|------|------|--------|
| `conftest.py` | 共享 fixture 和辅助函数（`task_store`、`make_cfg`、`make_movie`） | - |
| `test_task.py` | 任务系统：deferred、重试、文件校验、出队、入队 | 36 |
| `test_metadata.py` | 元数据完整性、NFO 读写、merge refresh、序列化、field_reasons | 38 (+13) |
| `test_nfo.py` | NFO XML 全字段写入、genre_norm 回退、tmdbid 生成 | 28 |
| `test_datatype.py` | MovieInfo.get_info_dic 模板字典、默认值、label 拆分 | 17 |
| `test_web_api.py` | Flask 端点：cover 安全、MovieScrapeStatus、trigger-scrape、tasks、config、batch | 30 (+19) |
| `test_web_crawlers.py` | 爬虫 HTML 解析（prestige、mgstage） | 2 |

## 新增测试时注意

1. **先检查 conftest.py** — 看是否有可复用的 fixture/helper
2. **Mock Cfg 要完整** — `mark_failure` 需要 `network_retry_after`，`validate_task_files` 需要 `scanner.minimum_size` + `daemon.download_stable_seconds`，`get_info_dic` 需要 `summarizer.default.*`，`write_nfo` 需要 `summarizer.nfo.*`
3. **文件名唯一** — 多测试共用 `tmp_path` 时，确保不同测试的视频文件名不同，避免文件大小快照冲突
4. **Duration 类型** — 使用 `SimpleNamespace(total_seconds=lambda: 3600)` 模拟
5. **不依赖外部文件** — 所有输入文件通过 `tmp_path` 创建
6. **容器兼容** — 测试不应假设特定 OS 路径格式，用 `os.path.join` 和 `str(path)`
7. **写配置文件要隔离** — 任何 POST 到 `/api/*_config` 的测试必须用临时 `GENERAL_CONFIG_FILE`，避免破坏真实 `config.yml` 导致 `Cfg()` 单例初始化崩溃
8. **`MovieInfo` 缺少 `nfo_title`** — 直接调用 `write_nfo()` 前必须 `info.nfo_title = info.title`
9. **锁安全** — 涉及 `process_lock` 的测试（trigger-scrape、run-now）需确保锁在测试间可恢复：
   ```python
   try:
       web_server.process_lock.release()
   except RuntimeError:
       pass
   ```
