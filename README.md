# JavSP WebUI

基于 [Yuukiy/JavSP](https://github.com/Yuukiy/JavSP) 扩展的媒体元数据刮削与整理工具，提供 Web 界面、命令行和 Docker 部署。汇总多个站点的信息，生成 NFO、封面及剧照，方便在 Kodi、Jellyfin 等媒体库中使用。

[Docker 镜像](https://hub.docker.com/r/madenginner/javsp) · [详细使用说明](guide.md) · [API 文档](Web%20API%20Usage.md)

## 主要特性

- **多站点汇总**：支持普通番号、FC2、CID 等类型，可调整爬虫顺序，并为标题、简介、演员、剧照设置来源优先级。
- **可视化管理**：在浏览器中启动刮削、查看实时日志和任务详情、编辑配置、管理爬虫。
- **自由选择重新刮削**：对比多个站点的候选结果，逐字段选择或手工编辑，确认后再更新元数据和媒体文件。
- **元数据补全**：检查已有影片的信息完整性，按需补充缺失信息、封面和剧照。
- **任务队列与重试**：记录处理进度和失败原因，支持重试、下载文件就绪检查，以及异常爬虫的暂时停用。
- **自动化与整理**：支持定时刮削、定时优化、下载完成 Webhook，以及自定义命名、移动文件或硬链接整理。

## 快速启动

将 `/你的媒体目录` 替换为实际路径：

```bash
docker run -d --name javsp \
  -p 127.0.0.1:5000:5000 \
  -v /你的媒体目录:/video \
  --restart unless-stopped \
  madenginner/javsp:latest
```

打开 <http://localhost:5000>，在页面中配置并启动刮削。默认扫描容器内的 `/video`，对应上面挂载的媒体目录。

任务记录保存在媒体目录中的 `.javsp_state.db`。如需在重建容器后保留个人设置，请备份或通过文件挂载持久化 `/app/config.yml` 和 `/app/web_ui/web_config.yml`。

## Web 模式和 CLI 模式

两种模式使用同一套刮削逻辑，区别在于操作入口：

| 模式 | 如何使用 | 适合场景 |
| --- | --- | --- |
| **Web** | 启动常驻 Web 服务，通过浏览器操作；同时提供 HTTP API 和页面中的定时任务 | 日常管理、查看进度、人工选择元数据、对接下载软件 |
| **CLI** | 在终端执行命令，不需要启动 Web 服务；`run-once` 执行一轮后退出，`daemon` 持续循环运行 | 脚本、系统计划任务、纯命令行环境 |

上面的 Docker 命令默认启动 Web 模式。已有容器中也可以执行 CLI，例如查看任务状态：

```bash
docker exec javsp python -m javsp status
```

CLI 还提供 `run-once`（扫描并处理）、`scan`（仅扫描入队）、`work`（处理到期任务）、`daemon`（后台循环）等命令。

## API / 下载完成联动

启动 Web 服务后，可通过 HTTP API 触发刮削、查询任务或更新配置。例如让下载软件在下载完成后调用：

```bash
curl -X POST http://localhost:5000/api/trigger-scrape \
  -H "Content-Type: application/json" \
  -d '{"target_file":"/video/ABC-123.mp4","move_files":"false"}'
```

`target_file` 使用服务端可访问的路径，Docker 部署时应填写容器内路径。也可以传 `target_dir` 指定目录，或不传路径使用默认扫描目录。通过 `GET /api/tasks` 查询任务进度与结果；完整参数见 [API 文档](Web%20API%20Usage.md)。

## 详细指南

- **完整使用说明**：[guide.md](guide.md)，可直接在 GitHub 阅读配置、API 和典型场景。
- **程序内指南**：启动后点击页面中的「📖 使用说明」，或访问 <http://localhost:5000/guide>。
- **离线 HTML 指南**：[web_ui/guide.html](web_ui/guide.html)，下载后用浏览器打开。GitHub 文件页显示的是 HTML 源码。
- **配置参数**：[Config Usage.md](Config%20Usage.md)。
- **接口参考**：[Web API Usage.md](Web%20API%20Usage.md)。
- **从源码运行**：[安装与启动说明](guide.md#source-setup)。

## 致谢与许可

基于 [Yuukiy/JavSP](https://github.com/Yuukiy/JavSP)，保留原作者署名，沿用 [GPL-3.0-only](LICENSE) 许可证。此版本增加了 Web UI、Docker 部署和任务管理等功能。
