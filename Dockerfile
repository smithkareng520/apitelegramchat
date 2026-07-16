# =====================================================================
# Clean Dockerfile — packaged entrypoint, no legacy root files required
# =====================================================================
FROM python:3.10-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bubblewrap \
        ca-certificates \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        media-types \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove

RUN groupadd -g 2000 app \
 && useradd  -u 2000 -g 2000 -m -d /home/app -s /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

RUN fallocate -l 1G /swapfile 2>/dev/null \
 && chmod 600 /swapfile 2>/dev/null \
 && mkswap /swapfile 2>/dev/null \
 && echo '/swapfile none swap sw 0 0' >> /etc/fstab \
 || echo "swap creation skipped"

COPY --chown=app:app . .

RUN mkdir -p /app/workspace \
 && chown app:app /app/workspace \
 && chmod 700 /app/workspace

RUN echo '* soft nofile 1024'  >> /etc/security/limits.conf \
 && echo '* hard nofile 4096'  >> /etc/security/limits.conf \
 && echo '* soft nproc  256'   >> /etc/security/limits.conf \
 && echo '* hard nproc  512'   >> /etc/security/limits.conf

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/', timeout=3)" || exit 1

EXPOSE 5000

CMD ["python", "-m", "quart", "run", "--app", "apitelegramchat.app:app", "--host", "0.0.0.0", "--port", "5000"]
