# syntax=docker/dockerfile:1

# ---------- builder:把依赖装进独立 venv ----------
FROM python:3.12-slim-bookworm AS builder

# 依赖(cryptography / psycopg[binary] / asyncssh 等)均有 linux 预编译 wheel,无需编译器
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build
# 先只拷 pyproject,利用缓存层(依赖不变则不重装)
COPY pyproject.toml ./
COPY app ./app
RUN pip install .

# ---------- runtime:精简运行镜像 ----------
FROM python:3.12-slim-bookworm AS runtime

# Node.js + npm:stdio 方式的云 MCP 服务器(如 aliyun mcp)通过 npx 拉起需要。
# 直接从官方 node 镜像拷贝二进制,避开 deb.debian.org 镜像不稳定的问题。
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    # SQLite 落到挂载卷里,容器重建不丢数据(可用 DATABASE_URL 覆盖为 Postgres)
    DATABASE_URL=sqlite:////app/data/ops_agent.db

# 复制已装好的依赖与应用源码(web 静态资源随源码一起,index.html 按相对路径加载)
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY app ./app
COPY pyproject.toml ./

# 非 root 运行 + 准备可写数据目录
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser
VOLUME ["/app/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

# 生产模式启动(不开 --reload)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
