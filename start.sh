#!/usr/bin/env bash
#
# 一键启动：构建镜像 -> 后台启动 -> 等待看板就绪 -> 自动打开浏览器。
#
#   ./start.sh            # 默认
#   ADDRESS_LIMIT=2000 ./start.sh   # 覆盖地址数（首次全量抓取）
#
# 等价于 `docker compose up -d --build` + 自动 `open http://localhost:8501`。
set -euo pipefail

cd "$(dirname "$0")"
URL="http://localhost:8501"

echo "==> docker compose up -d --build ..."
docker compose up -d --build

echo "==> 等待看板就绪 (${URL}) ..."
for i in $(seq 1 60); do
  if curl -sf "${URL}/_stcore/health" >/dev/null 2>&1; then
    echo "==> 看板已就绪"
    break
  fi
  sleep 3
done

echo "==> 打开浏览器：${URL}"
if command -v open >/dev/null 2>&1; then
  open "${URL}"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "${URL}"
else
  echo "请手动访问 ${URL}"
fi

echo "==> 完成。查看日志：docker compose logs -f churn-app"
