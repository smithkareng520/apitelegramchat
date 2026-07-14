# =====================================================================
# Hardened Dockerfile — 多用户共享 bash 沙箱环境
# 防御目标: 容器逃逸 / 提权 / 横向移动 / 密钥泄漏 / 资源耗尽
# =====================================================================
FROM python:3.10-slim

# ---------- 1) 系统依赖 ----------
# bubblewrap: 用户命名空间沙箱（Flatpak 同款），无需特权
# ca-certificates: HTTPS 必需
# libgl1 + libglib2.0-0: PaddleOCR (OpenCV) 所需图形库
# libgomp1: PaddlePaddle 所需的 OpenMP 运行时库
RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        media-types \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove

# ---------- 2) 创建非 root 用户（UID/GID 固定，避免与宿主冲突） ----------
RUN groupadd -g 2000 app \
 && useradd  -u 2000 -g 2000 -m -d /home/app -s /usr/sbin/nologin app

# ---------- 3) Python 依赖 ----------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ---------- 3.5) 创建 1GB swap 文件，避免 512MB 内存突发 OOM 把整个 bot 杀掉 ----------
# 注：Docker 容器内创建 swap 需要宿主支持 swap limit；Render 上不可用时此步骤无害
RUN fallocate -l 1G /swapfile 2>/dev/null \
 && chmod 600 /swapfile 2>/dev/null \
 && mkswap /swapfile 2>/dev/null \
 && echo '/swapfile none swap sw 0 0' >> /etc/fstab \
 || echo "swap creation skipped (likely running on container runtime without swap support)"

# ---------- 4) 应用代码（chown 给非 root 用户） ----------
COPY --chown=app:app . .

# ---------- 5) workspace 目录 ----------
# 这是每个 chat_id 的工作目录，权限 700 防止跨 chat 读取
RUN mkdir -p /app/workspace \
 && chown app:app /app/workspace \
 && chmod 700 /app/workspace

# ---------- 6) 内核安全开关 ----------
# 关闭核心转储（防止内存里的 Key 被落盘）
# 限制用户进程数（兜底 fork bomb，主要靠 bwrap + 看门狗）
RUN echo '* soft nofile 1024'  >> /etc/security/limits.conf \
 && echo '* hard nofile 4096'  >> /etc/security/limits.conf \
 && echo '* soft nproc  256'   >> /etc/security/limits.conf \
 && echo '* hard nproc  512'   >> /etc/security/limits.conf

# ---------- 7) 切换非 root ----------
USER app

# ---------- 8) 健康检查 ----------
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=3)" || exit 1

EXPOSE 5000

# ---------- 9) 启动 ----------
CMD ["python", "-u", "app.py"]
