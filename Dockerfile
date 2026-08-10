# =====================================================================
# Clean Dockerfile — packaged entrypoint, no legacy root files required
# =====================================================================
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src
ENV APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data

# bubblewrap 提供 bwrap 沙箱（用于 bash 工具的命名空间隔离）。
# 即使装了，Render / Heroku / 非 privileged Docker 也起不来 unprivileged
# userns，所以默认让 SANDBOX_ALLOW_FALLBACK=1 —— fallback 用 bash + rlimits
# + no-new-privs + 每命令 cd 强制兜底，bwrap 可用时自动升级到全隔离模式。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libgl1 libglib2.0-0 libgomp1 \
        bubblewrap bash \
    && rm -rf /var/lib/apt/lists/*

# 默认允许 fallback；需要严格隔离时显式覆盖为 0
ENV SANDBOX_ALLOW_FALLBACK=1
# Render 容器内核不允许 unshare-net，关掉避免 bwrap 模式起不来
ENV SANDBOX_UNSHARE_NET=0

RUN groupadd -g 2000 app && useradd -u 2000 -g 2000 -m -d /home/app -s /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY src ./src
COPY README.md ./
COPY app.py ./

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

RUN mkdir -p /app/workspace && chown -R app:app /app/workspace /app/src /home/app

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os, urllib.request; port=os.getenv('PORT', '5000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)" || exit 1

EXPOSE 5000

CMD ["sh", "-c", "exec python -m quart run --app app:app --host 0.0.0.0 --port ${PORT:-5000}"]
