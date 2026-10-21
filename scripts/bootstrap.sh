#!/bin/sh
# 从项目根目录执行；不会删除已有数据卷。
set -eu
cd "$(dirname "$0")/.."
if [ ! -f .env ]; then
  cp .env.example .env
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker 守护进程不可连接，请先启动 Docker Desktop，再重新运行本脚本。" >&2
  exit 1
fi
docker compose --profile app config --quiet
docker compose build minio
docker compose up -d --wait postgres redis neo4j minio
docker compose --profile app build
docker compose run --rm api python -m alembic upgrade head
docker compose run --rm api python -m scripts.seed
docker compose run --rm api python -m scripts.graph_seed
docker compose run --rm api python -m scripts.analytics_sync
docker compose run --rm api python -c 'from src.infra.storage import ensure_bucket_exists; ensure_bucket_exists()'
docker compose --profile app up -d api worker web
