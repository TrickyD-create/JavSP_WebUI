# --- Stage 1: Builder ---
FROM python:3.12-slim AS builder

WORKDIR /app

# 【保留】完全保留您原来的 Pypi 镜像源设置
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_INDEX_URL="https://mirrors.aliyun.com/pypi/simple/" \
    PIP_TRUSTED_HOST="mirrors.aliyun.com"

# 【保留】完全保留您指定的 Poetry 安装方式
RUN pip install poetry==2.1.4

# 【保留】完全保留您原来的 Poetry 配置
RUN poetry config virtualenvs.in-project true

# 复制所有项目文件
COPY . .

RUN poetry install --no-interaction --no-ansi

RUN .venv/bin/pip install --no-cache-dir -r web_ui/requirements.txt

RUN rm -rf /app/.git


# --- Stage 2: Runner ---
# 基于您原始的、成功的 runner 阶段
FROM python:3.12-slim

WORKDIR /app


# 【保留】完全保留您原来的“精确复制”文件结构
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/javsp/ ./javsp/
COPY --from=builder /app/data/ ./data/
COPY --from=builder /app/image/ ./image/
COPY --from=builder /app/tools/ ./tools/
COPY --from=builder /app/config.example.yml .
COPY --from=builder /app/pyproject.toml .
COPY --from=builder /app/LICENSE .
COPY --from=builder /app/README.md .

# 【新增】精确复制 web_ui 目录
# 这是运行 Web UI 服务所必需的，因为它包含了 web_server.py
COPY --from=builder /app/web_ui/ ./web_ui/

# 【保留】完全保留您原来的 PATH 环境变量设置
ENV PATH="/app/.venv/bin:$PATH"

# 【新增】暴露 Web 服务器的端口
EXPOSE 5000

# 【新增】声明媒体库挂载点。状态库默认写入 /video/.javsp_state.db，便于重建容器后保留任务状态
VOLUME ["/video"]

# 【新增】使用 Python 标准库做健康检查，避免额外安装 curl/wget
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=3).read(1)"

# 【核心修改】替换启动命令以运行 Web 服务器
CMD ["python", "web_ui/web_server.py"]
