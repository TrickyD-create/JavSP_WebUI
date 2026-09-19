# 开发与个人配置

## 从源码运行

需要 Python 3.10–3.12 和 Poetry：

```bash
poetry install
poetry run pip install -r web_ui/requirements.txt
poetry run python web_ui/web_server.py
```

本地运行时请在配置页面将扫描目录从 `/video` 改为实际媒体目录，并设置可写的状态库路径。

## 提交更新与隐私保护


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
