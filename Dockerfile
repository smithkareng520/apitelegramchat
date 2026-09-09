# =====================================================================
# Clean Dockerfile — packaged entrypoint, no legacy root files required
# =====================================================================
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src
ENV APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data
ENV TZ=Asia/Shanghai
# Stable CJK font used by PDF generation and server-side Office rendering.
ENV APITELEGRAMCHAT_CJK_FONT=NotoSansCJKsc
ENV APITELEGRAMCHAT_CJK_FONT_FILE=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
# ReportLab needs a TrueType outline for embedding; Noto CJK uses CFF outlines.
ENV APITELEGRAMCHAT_REPORTLAB_CJK_FONT=/usr/share/fonts/truetype/arphic-gbsn00lp/gbsn00lp.ttf

# 配置系统时区为上海（CST/UTC+8）
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 沙箱用 Landlock（Linux 5.13+ 内核特性，非特权进程可用）。
# node:22-bookworm-slim 基于 Debian 12 (bookworm)，内核 5.15+，Render 上 Landlock 可用。
# 不需要 bubblewrap —— bwrap 在 Render 的非 privileged 容器里永远起不来
# （内核禁了 unprivileged userns），留着只会造成误导。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        git \
        jq \
        zip \
        unzip \
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
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
        fontconfig \
        fonts-noto-cjk \
        fonts-arphic-gbsn00lp \
    && fc-cache -f -v >/dev/null \
    && fc-match "Noto Sans CJK SC" >/dev/null \
    && test -f /usr/share/fonts/truetype/arphic-gbsn00lp/gbsn00lp.ttf \
    && tesseract --list-langs 2>/dev/null | grep -qx "chi_sim" \
    && tesseract --list-langs 2>/dev/null | grep -qx "chi_tra" \
    && rm -rf /var/lib/apt/lists/*

# 沙盒身份固定为 claude（uid/gid 仍为 2000）：
#   - whoami / id / ls -l 属主列全部真实解析为 "claude"（passwd 级一致，
#     不再出现 whoami=app 与 $USER=chat{id} 的精神分裂）；
#   - uid 保持 2000 不变：老部署在 Render disk 上已有的
#     /tmp/apitelegramchat_data 文件属主无需迁移，升级零成本；
#   - 名字可用 APITELEGRAMCHAT_SANDBOX_USER 覆盖（见 src/sandbox.py），
#     但必须与镜像内 passwd 同步修改，否则 whoami 会退回数字 uid。
RUN groupadd -g 2000 claude && useradd -u 2000 -g 2000 -m -d /home/claude -s /usr/sbin/nologin claude

WORKDIR /app

COPY requirements.txt pyproject.toml package.json ./
COPY src ./src
COPY .claude ./.claude
COPY README.md ./

RUN python3 -m pip install --break-system-packages --no-cache-dir --upgrade pip && \
    python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt && \
    python3 -m pip install --break-system-packages --no-cache-dir . && \
    npm install --omit=dev --no-audit --no-fund

RUN mkdir -p /app/workspace && chown -R claude:claude /app/workspace /app/src /home/claude

USER claude

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python3 -c "import os, urllib.request; port=os.getenv('PORT', '5000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)" || exit 1

EXPOSE 5000

CMD ["sh", "-c", "exec python3 -m quart --app app:app run --host 0.0.0.0 --port ${PORT:-5000}"]
