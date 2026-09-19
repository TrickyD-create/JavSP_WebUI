# JavSP WebUI

基于 [Yuukiy/JavSP](https://github.com/Yuukiy/JavSP) 的 Web UI 与 Docker 扩展版本，支持媒体元数据抓取、任务队列、定时任务和逐字段重新刮削。

Docker 镜像：[madenginner/javsp](https://hub.docker.com/r/madenginner/javsp)。首次公开提交的应用文件已与 Docker Hub `2.7` / `latest` 镜像（摘要 `sha256:03dca567ef0272a113f20fa75b2d746656210aa318bcf727949db6d463e8e92a`）逐字节核对。

## 快速启动

将下面的 `/你的媒体目录` 替换为实际路径：

```bash
docker run -d --name javsp \
  -p 127.0.0.1:5000:5000 \
  -v /你的媒体目录:/video \
  --restart unless-stopped \
  madenginner/javsp:2.7
```

打开 <http://localhost:5000>。配置模板为 `config.example.yml`，首次启动自动生成 `config.yml`。容器内 `/app/config.yml` 与 `/app/web_ui/web_config.yml` 的修改需自行备份或通过文件挂载持久化。

## 从源码运行

需要 Python 3.10–3.12 和 Poetry：

```bash
poetry install
poetry run pip install -r web_ui/requirements.txt
poetry run python web_ui/web_server.py
```

本地运行时请在配置页面将扫描目录从 `/video` 改为实际媒体目录，并设置可写的状态库路径。

## 项目来源与许可

原项目：[Yuukiy/JavSP](https://github.com/Yuukiy/JavSP)。保留原作者署名，本项目沿用 `GPL-3.0-only`，完整文本见 [LICENSE](LICENSE)。此仓库包含 Web UI、Docker 和任务管理相关修改。

公开版不包含个人运行配置、凭据、数据库、日志、编辑器文件或本地 Git 历史。旧自动化脚本因引用不存在的构建/测试路径，未纳入首次公开版。

---

## Docker WebUI 使用指南


默认配置会扫描容器内的 `/video`。如果你用 Docker，请把宿主机影片目录挂载到 `/video`。

### 页面

- **实时控制台**：点击启动，查看本轮刮削过程。
- **任务中心**：查看任务队列、失败任务、最近运行、爬虫健康状态，并可重试失败任务。
- **自由选择重新刮削**：先并行抓取各爬虫候选，再逐字段选择或编辑最终值；确认前不下载媒体、不修改 NFO。
- **参数配置**：编辑主配置和定时任务配置。
- **爬虫管理**：调整爬虫顺序。

点击“启动 JAVSP 主程序”时会执行：

```text
扫描 /video -> 创建任务 -> 执行队列 -> 写入运行摘要
```

如果已有任务正在运行，本次点击会被拒绝，不会插队或并发执行，避免重复写 NFO、封面或移动影片。

### 自由选择重新刮削

在成功影片上点击“重新刮削”，可以选择：

- **自动重新刮削**：保持原有行为，按当前优先级自动汇总并更新。
- **自由选择模式**：只抓取并保存文字元数据候选，进入“待人工选择”；选择来源后仍可手工编辑或主动清空字段。

自由模式的候选阶段不会下载封面、剧照、演员头像等媒体，也不会改写数据库中的最终元数据或 NFO。只有点击“确认并更新”后，才会写入最终字段并按原有自动逻辑处理媒体。关闭页面或重启容器后，可在任务中心通过“继续选择”恢复；取消会保留原有文件和元数据。

### 常用命令

容器内也可以直接运行 CLI：


可用命令：

```bash
python -m javsp run-once  # 扫描 + 入队 + 执行队列
python -m javsp scan      # 只扫描并创建任务
python -m javsp work      # 只执行队列中的到期任务
python -m javsp status    # 查看任务队列和最近运行摘要
python -m javsp daemon    # 常驻后台循环运行
```

不写子命令时，默认等价于 `run-once`。

### 状态库

后台任务状态默认写入：

```yaml
daemon:
  state_db: /video/.javsp_state.db
```

Docker 下这个文件会出现在宿主机挂载目录中，例如：

文件名为 .javsp_state.db


它用于记录任务、运行摘要、失败原因、爬虫健康状态。删除它不会删除影片，只会清空任务历史；下次运行会自动重新创建。

### 配置示例

后台任务配置：

```yaml
daemon:
  enabled: false
  state_db: /video/.javsp_state.db
  scan_interval: PT30M
  worker_interval: PT30S
  max_movies_per_run: 0
  download_stable_seconds: 300
```

字段说明：

- `enabled`：是否启用常驻 daemon 模式。
- `state_db`：SQLite 状态库路径。Docker 推荐放在 `/video` 下。
- `scan_interval`：`daemon` 模式下两轮扫描间隔，`PT30M` 表示 30 分钟。
- `worker_interval`：Worker 空闲轮询间隔。
- `max_movies_per_run`：每轮最多处理几部，`0` 表示不限制。
- `download_stable_seconds`：文件大小变化后等待多久再处理，用来避开下载中的文件。

重试和爬虫熔断：

```yaml
retry_policy:
  network_retry_after: PT1H
  metadata_retry_after: P1D
  max_retry_count: 3
  crawler_circuit_break_threshold: 5
  crawler_circuit_break_duration: PT6H
```

字段说明：

- `network_retry_after`：网络失败后多久再试。
- `metadata_retry_after`：元数据缺失时多久再试。
- `max_retry_count`：单个任务自动重试上限。
- `crawler_circuit_break_threshold`：某个爬虫连续失败多少次后熔断。
- `crawler_circuit_break_duration`：熔断持续时间。

通知配置：

```yaml
notifications:
  enabled: false
  webhook_url: null
  notify_on_success: true
  notify_on_failure: true
```


## 开发与个人配置

日常只需维护当前克隆目录，通过 `git add`、`git commit` 和 `git push` 更新 GitHub。

- `config.example.yml` 和 `web_ui/web_config.example.yml` 是可公开的模板，请勿填入真实凭据。
- `config.yml` 和 `web_ui/web_config.yml` 是本地运行配置，首次启动从模板生成，Git 和 Docker 构建均忽略它们。
- 数据库、日志、`.env`、私钥和编辑器目录也已忽略。忽略规则不等于万能的隐私检查，提交前仍应检查 `git diff --cached`。
- 可执行 `git config core.hooksPath .githooks` 启用提交与推送检查，拦截常见私密文件、个人目录路径和密钥。不要使用 `git add -f` 强行提交个人文件。

常用更新流程：

```bash
git status
git add javsp/ web_ui/ tests/ README.md
git diff --cached
git commit -m "说明本次修改"
git push
```

推送需要已配置 GitHub 身份验证；新电脑需单独登录。GitHub 源码更新不会自动更新 Docker Hub 镜像。
