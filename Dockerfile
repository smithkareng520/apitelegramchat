# =====================================================================
# Clean Dockerfile — packaged entrypoint, no legacy root files required
# =====================================================================
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src
ENV APITELEGRAMCHAT_DATA_DIR=/tmp/apitelegramchat_data
# 工作空间根：agent 家目录即 /home/<userid>，bash 里 pwd 不再携带
# data_root 前缀。/home 必须对运行用户可写（见下方 chown）。
ENV APITELEGRAMCHAT_WORKSPACES_DIR=/home
ENV TZ=Asia/Shanghai
# 配置系统时区为上海（CST/UTC+8）
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 沙箱用 Landlock（Linux 5.13+ 内核特性，非特权进程可用）。
# 注意：Docker 基础镜像不决定实际内核版本/安全策略；容器共享宿主内核。
# 因此部署时必须实际探测 Landlock / prctl 能力，不能假定 Render 或任何
# 托管平台一定允许某个内核接口。
# 不需要 bubblewrap —— bwrap 依赖的 unprivileged userns 在部分托管容器中
# 被宿主策略禁用；Landlock 更适合本项目的非特权文件系统边界。
# 小体积镜像：安装轻量 Liberation/DejaVu 字体、不装 OCR；LibreOffice 只装 Writer（docx 技能的
# 转换/渲染/接受修订只需要 Writer，不装 Calc/Impress/Draw 等）。
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
        libreoffice-writer \
        fonts-liberation2 \
        fonts-dejavu-core \
        poppler-utils \
        qpdf \
        pandoc \
        imagemagick \
    && rm -rf /var/lib/apt/lists/*

# 基础镜像 node:22-bookworm-slim 自带系统用户 node（/home/node，uid/gid
# 通常为 1000），未被后续任何步骤 chown。而 workspaces_root() 默认把
# /home 当作"每个聊天 namespace 一个子目录"的父目录——namespace 只要
# sanitize 后等于字符串 "node"（例如某个 chat 的 username 恰为 node），
# workspace_root() 就会解析到这个预置的 /home/node，claude 用户对它既
# 无写权限也无所有权，导致 skills 同步等所有写操作 PermissionError(13)。
# 下方 chown /home（非递归）只处理 /home 本身，不会修复已存在的
# /home/node，必须显式删除这个预置账号与家目录，让 /home 在 chown 前
# 是真正空目录，chown 之后其下任何子目录都由 claude 按需创建、天然
# 属于 claude。
RUN deluser --remove-home node 2>/dev/null || true

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
# mcp.json —— MCP 服务器统一注册表（内 + 外）。mcp_manager 启动时从
# 项目根（/app/mcp.json）加载；缺失会导致全部 MCP 工具（含内部 stdio
# 服务器与 gaode_mcp）不可用，必须在镜像内。
COPY mcp.json ./

RUN python3 -m pip install --break-system-packages --no-cache-dir --upgrade pip && \
    python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt && \
    python3 -m pip install --break-system-packages --no-cache-dir . && \
    npm install --omit=dev --no-audit --no-fund

# 工作空间根 /home 交给运行用户（uid 2000）：非 root 进程要能在 /home
# 下创建每户家目录 /home/<chat-ns>。这里改为 -R：上面已删除基础镜像
# 预置的 node 账号家目录，但仍用递归 chown 兜底任何遗留/未来新增的
# 基础镜像用户目录，避免同类 PermissionError 以其他 namespace 名字
# 复发；/home/claude 已属 claude，递归 chown 对它是无操作。0700 收紧
# 家目录隐私边界，其他系统用户（容器内无）无法枚举用户目录。
RUN mkdir -p /home && chown -R claude:claude /home && chmod 700 /home

RUN mkdir -p /app/workspace && chown -R claude:claude /app/workspace /app/src /home/claude

USER claude

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python3 -c "import os, urllib.request; port=os.getenv('PORT', '5000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3)" || exit 1

EXPOSE 5000

CMD ["sh", "-c", "exec python3 -m quart --app app:app run --host 0.0.0.0 --port ${PORT:-5000}"]
