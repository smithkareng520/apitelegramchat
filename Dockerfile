# =====================================================================
# Clean Dockerfile — packaged entrypoint, no legacy root files required
# =====================================================================
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src
ENV APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data

# 沙箱用 Landlock（Linux 5.13+ 内核特性，非特权进程可用）。
# python:3.10-slim 基于 Debian 12 (bookworm)，内核 5.15+，Render 上 Landlock 可用。
# 不需要 bubblewrap —— bwrap 在 Render 的非 privileged 容器里永远起不来
# （内核禁了 unprivileged userns），留着只会造成误导。
#
# 预装 skill 依赖：
#   - nodejs / npm: docx skill 用 docx-js 生成 .docx
#   - pandoc: docx skill 读取/转换 .docx
#   - libxml2-utils: skill 脚本里的 XML 校验
#   - tesseract-ocr + poppler-utils: pdf skill 的 OCR 和 pdftoppm
#   - libgl1 / libglib2.0-0 / libgomp1: Pillow/OpenCV 运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libgl1 libglib2.0-0 libgomp1 \
        nodejs npm \
        pandoc libxml2-utils \
        tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 2000 app && useradd -u 2000 -g 2000 -m -d /home/app -s /usr/sbin/nologin app

WORKDIR /app

COPY requirements.txt pyproject.toml ./
COPY src ./src
COPY .claude ./.claude
COPY README.md ./

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

# 预装 docx skill 用的 npm 包（全局），让模型不用在运行时跑 `npm install -g docx`（12-90s 必超时）
# 用 --prefix 装到 /usr/local/lib/node_modules 保持全局可见。
RUN npm install -g docx@9.5.1 --omit=dev || npm install -g docx --omit=dev

# 测试 docx 能被 require，否则启动后模型才发现缺包会更难排查
RUN node -e "require('docx'); console.log('docx-js OK')" || echo "WARN: docx-js require failed, docx skill may not work"

RUN mkdir -p /app/workspace && chown -R app:app /app/workspace /app/src /home/app

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os, urllib.request; port=os.getenv('PORT', '5000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)" || exit 1

EXPOSE 5000

CMD ["sh", "-c", "exec python -m quart --app apitelegramchat.app:app run --host 0.0.0.0 --port ${PORT:-5000}"]
