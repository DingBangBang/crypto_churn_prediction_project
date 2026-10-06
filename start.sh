#!/usr/bin/env bash
#
# 一键启动（宿主机入口）：构建镜像 -> 后台起容器 -> **实时把容器里的进度流到本终端** ->
# 等看板就绪 -> 自动打开「运行状态页 + 看板」-> 打印四去向链接清单。
#
#   ./start.sh                       # 默认（首启 ADDRESS_LIMIT=200，几分钟）
#   ADDRESS_LIMIT=2000 ./start.sh    # 全量验证（数小时；进度条会一直走）
#   RUN_DELIVER=0 ./start.sh         # 只跑流水线，不交付邮件/Lark/状态页
#   NO_OPEN=1 ./start.sh             # 不自动打开浏览器（CI / 远程终端）
#   FORCE_PIPELINE=1 ./start.sh      # 忽略 marker，强制重跑整条流水线
#
# 为什么还需要这个脚本：容器里没有 GUI —— 「macOS 通知」和「自动打开网页」只能由宿主机做；
# 同时它解决「docker compose 一跑就闷声在后台、不知道有没有生效」的问题：
# 这里把容器日志实时流到终端（阶段编号 + 进度条 + 百分比），跑完再自动开页面。
set -euo pipefail

cd "$(dirname "$0")"
URL="http://localhost:8501"
REPORTS_DIR="$(pwd)/reports"
STATUS_PAGE="${REPORTS_DIR}/status.html"
START_TS="$(date +%s)"
LOG_PID=""

opens() {
  if [ "${NO_OPEN:-0}" = "1" ]; then
    echo "   （NO_OPEN=1：未自动打开，请手动访问 $1）"
    return 0
  fi
  if command -v open >/dev/null 2>&1; then open "$1"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1"
  else echo "   请手动访问 $1"; fi
}

cleanup() {
  if [ -n "${LOG_PID}" ]; then kill "${LOG_PID}" 2>/dev/null || true; fi
}
trap cleanup EXIT

echo "============================================================"
echo " 🪙 加密货币用户行为聚类分析与流失预测 · 一键启动"
echo " 步骤：构建镜像 → 抓取 → 特征 → 聚类 → 预测 → 交付四去向 → 看板"
echo " 地址数 ADDRESS_LIMIT=${ADDRESS_LIMIT:-200}（environment.env 里的值优先）"
echo "============================================================"

echo "==> [1/4] 构建镜像并启动容器：docker compose up -d --build"
docker compose up -d --build

echo "==> [2/4] 实时进度（跟随容器日志；Ctrl-C 只停跟随，容器会继续跑）"
docker compose logs -f --tail=40 churn-app &
LOG_PID=$!
sleep 2

echo "==> [3/4] 等待看板就绪 (${URL}) ..."
for _ in $(seq 1 100); do
  if curl -sf "${URL}/_stcore/health" >/dev/null 2>&1; then
    echo "    看板已就绪"
    break
  fi
  sleep 3
done

# 状态页由容器内的交付步骤写进挂载目录；必须是**本次运行新写的**，避免打开上一次的旧页面。
if [ "${RUN_DELIVER:-1}" != "0" ]; then
  echo "==> [4/4] 等待本次运行生成的 reports/status.html（可继续看上面的进度）"
  for _ in $(seq 1 7200); do
    if [ -s "${STATUS_PAGE}" ]; then
      PAGE_TS="$(date -r "${STATUS_PAGE}" +%s 2>/dev/null || echo 0)"
      [ "${PAGE_TS}" -ge "${START_TS}" ] && break
    fi
    sleep 3
  done
else
  echo "==> [4/4] RUN_DELIVER=0：跳过状态页等待"
fi

cleanup; LOG_PID=""

LATEST_REPORT="$(ls -t "${REPORTS_DIR}"/daily_report_*.html 2>/dev/null | head -1 || true)"
echo
echo "============================================================"
echo " ✅ 一键启动完成 —— 四个去向 / 入口"
echo "------------------------------------------------------------"
echo " ① 运行状态页（报错详情 + 结果 + 建议）："
echo "      ${STATUS_PAGE}"
echo "      file://${STATUS_PAGE}"
echo " ② 交互看板（16 面板）：${URL}"
echo " ③ 当日日报 HTML：${LATEST_REPORT:-（本次未生成，需配置邮件通道）}"
echo " ④ 邮件：收件箱（ALERT_EMAIL_TO；需在 environment.env 配好 Gmail/SMTP）"
echo " ⑤ Lark 群卡片：机器人所在群（需 LARK_WEBHOOK_URL）"
echo "------------------------------------------------------------"
if [ "${NO_OPEN:-0}" = "1" ]; then
  echo " 自动打开已关闭（NO_OPEN=1）—— 请手动复制上方 ①②③ 链接访问。"
else
  echo " 正在自动打开 ① 运行状态页 与 ② 看板。"
  echo " 若浏览器没有反应，请手动复制上方链接打开（系统会打印实际使用的打开方式）。"
fi
echo " 日志：docker compose logs -f churn-app      停止：docker compose down"
echo "============================================================"

opens "${STATUS_PAGE}"
opens "${URL}"
