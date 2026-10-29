# syntax=docker/dockerfile:1
# 单阶段镜像:零第三方依赖,只装 Python 标准库所需的最小运行时。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BRAIN_ROOT=/data

# age:备份加密用。刻意装发行版包而不是自研加密;发行版版本可能落后,需要更强保证就自己换 1.2.x+
# ca-certificates:WebDAV 目的地走 HTTPS 时要用
RUN apt-get update \
 && apt-get install -y --no-install-recommends age ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 非 root 运行:uid/gid 固定,便于宿主机目录授权
RUN groupadd -g 10001 brain \
 && useradd -u 10001 -g brain -d /data -s /usr/sbin/nologin brain \
 && mkdir -p /app /data \
 && chown -R 10001:10001 /app /data

WORKDIR /app
COPY --chown=10001:10001 brainlib/ ./brainlib/
COPY --chown=10001:10001 bin/brain ./bin/brain
RUN chmod 0755 /app/bin/brain \
 && ln -sf /app/bin/brain /usr/local/bin/brain

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8790

# 标准库探活,不引入 curl
# 备份要在容器内跑 age-keygen / age,这里声明一下依赖,免得有人以为镜像是纯 stdlib
ENV BRAIN_BACKUP_AGE=/usr/bin/age

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python3", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/v1/health', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "/app/bin/brain"]
CMD ["serve", "--bind", "0.0.0.0", "--port", "8790"]
