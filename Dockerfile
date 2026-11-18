# outreach-scheduler server image — HTTP-side pipeline only.
# The CUA channel (browser captcha) intentionally stays on the desktop:
# this image has no Chrome/playwright. Plans in state='cua' remain queued
# and are processed by a local machine (see docs/deployment.md).
# 中文：服务器镜像只跑 HTTP 侧全链；CUA 通道留在本地真机（无 Chrome）。
FROM python:3.12-slim

WORKDIR /app

# single runtime dependency: pyyaml (everything else is stdlib)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# supercronic: cron in the container (no system cron daemon needed)
ARG SUPERCRONIC_VERSION=v0.2.30
ARG SUPERCRONIC_SHA1=054cafefb4f5b38b2e6a44ad9ec6a2f9d5afe7d4
RUN ARCH="$(dpkg --print-architecture)" \
 && curl -fsSL "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" \
      -o /usr/local/bin/supercronic \
 && echo "${SUPERCRONIC_SHA1}  /usr/local/bin/supercronic" | sha1sum -c - \
 && chmod +x /usr/local/bin/supercronic

COPY bin/ bin/
COPY src/ src/
COPY scripts/ scripts/
COPY config/global.example.yaml config/global.example.yaml

# crontab: pipeline weekly + monthly supply refresh; run daily (see fly.toml)
COPY deploy/crontab /crontab

# data lives on a fly volume mounted at /data (OSCHED_DATA_ROOT=/data)
ENV OSCHED_DATA_ROOT=/data
VOLUME /data

# secrets are injected via `fly secrets set` -> os.environ (require_env_key
# reads env first). SMTP/IMAP creds; OPENPAGERANK_API_KEY when IP-bound.

CMD ["supercronic", "/crontab"]
