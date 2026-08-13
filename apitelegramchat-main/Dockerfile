# =====================================================================
# Clean Dockerfile — packaged entrypoint, no legacy root files required
# =====================================================================
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src
ENV APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data

# 沙箱用 Landlock（Linux 5.13+ 内核特性，非特权进程可用）。
# node:22-bookworm-slim 基于 Debian 12 (bookworm)，内核 5.15+，Render 上 Landlock 可用。
# 不需要 bubblewrap —— bwrap 在 Render 的非 privileged 容器里永远起不来
# （内核禁了 unprivileged userns），留着只会造成误导。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-pip \
        python3-venv \
        build-essential \
        cmake \
        ccache \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libreoffice \
        poppler-utils \
        qpdf \
        pandoc \
        imagemagick \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 2000 app && useradd -u 2000 -g 2000 -m -d /home/app -s /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt pyproject.toml package.json ./
COPY src ./src
COPY .claude ./.claude
COPY README.md ./

RUN python3 -m pip install --break-system-packages --no-cache-dir --upgrade pip && \
    python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt && \
    python3 -m pip install --break-system-packages --no-cache-dir . && \
    npm install --omit=dev --no-audit --no-fund

RUN mkdir -p /app/workspace && chown -R app:app /app/workspace /app/src /home/app

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python3 -c "import os, urllib.request; port=os.getenv('PORT', '5000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)" || exit 1

EXPOSE 5000

CMD ["sh", "-c", "exec python3 -m quart --app apitelegramchat.app:app run --host 0.0.0.0 --port ${PORT:-5000}"]
