# MEMORY.md — 项目记忆 / 交接手册

> **用途**：把这个文件带到**全新的 Session 和 workspace 文件夹**里即可无缝接续工作。
> 重点记录了 **Gmail 邮件通道的配置与全部踩坑解法**（本项目最费时的部分）。
>
> 最后更新：2026-10-07 ｜ 仓库：`https://github.com/DingBangBang/crypto_churn_prediction_project`（分支 `main`）
> 最近提交：`6094949`（看板挂载 `./docs` + 邮件标题去掉「[测试]」）

---

## 0. 这是什么

加密货币（以太坊）用户行为聚类 + 流失预测流水线：
Etherscan 抓取 → 特征工程 → HDBSCAN 聚类 → LightGBM 流失预测 + SHAP + ARIMA 多时间窗 → Streamlit **16 面板看板**，并把结果交付到 **邮件 / Lark 卡片 / 运行状态页 / macOS 通知** 四个去向。

**阶段顺序（记住它，决定「下一步该跑什么」）：**
```
① 抓取 → ② 特征 → ③ 聚类 → ④ 预测(churn_model.py) → ⑤ 交付(deliver)
```
> 跑完 `churn_model.py` 后，**下一步就是第 ⑤ 步的交付层**：`python scripts/daily_run.py --deliver-only`（复用 `churn_summary.json`，不重算）。

---

## 1. 运行环境 & 常用命令

- 本机 conda 环境：`/opt/miniconda3/envs/crypto_churn_prediction_project`（Python 3.11.16）
  - 统一用：`PY=/opt/miniconda3/envs/crypto_churn_prediction_project/bin/python`
- 容器内 Python 3.11.17；镜像 `bonnie333333333/crypto-churn-prediction:latest`

```bash
# 全流程 + 交付四去向（本机）
python scripts/run_pipeline.py --deliver
# 只做交付，复用已有预测结果（churn_model.py 之后的下一步）
python scripts/daily_run.py --deliver-only
# 一键（容器）：构建 + 跑全流程 + 交付四去向 + 开看板
./start.sh
# 只跑流水线、不推送（CI）
RUN_DELIVER=0 docker compose up -d --build
# 测试
python -m pytest tests/ -q          # 当前 42 passed
```

- 关键产物：`reports/status.html`（运行状态页）、`reports/last_run.json`、`docs/insights.md`（看板「业务洞察」面板的数据源）、`data/crypto_churn.db`（全量）/ `data/crypto_churn_test.db`（测试）。

---

## 2. 环境变量与配置

- 配置入口：根目录 **`environment.env`**（已被 `.gitignore`，**含密钥，绝不提交**）；模板 `environment.env.example`。
- 读取顺序（`src/config.py`，先命中者胜）：`environment.env` → `environment .env` → `.env` → **纯 OS 环境变量**（容器里走这条）。
- **测试模式**：`ADDRESS_LIMIT <= 200` 即 `TEST_MODE`，所有产物加 `_test` 后缀；本机全量验证口径是 `2000`。
- Etherscan Key 兜底：env 里的 `ETHERSCAN_API_KEY` 优先，其次读本地 `Etherscan_api.txt`（git-ignored）。

---

## 3. 📧 Gmail 邮件配置与全部踩坑（**本项目最核心的排障记录**）

### 3.1 结论先行

Google 已逐步下线「应用专用密码」。**本项目推荐方案 A：Gmail API + OAuth2**——走 HTTPS 443，比 SMTP:465 更容易穿透受限网络。

### 3.2 方案 A（推荐）：Gmail API + OAuth2

在 Google Cloud Console 建 OAuth 客户端（类型必须选 **桌面应用**）。需要填的变量：

```env
# environment.env
GMAIL_CLIENT_ID=xxxx.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=xxxx
GMAIL_REFRESH_TOKEN=        # 不用手填，下面命令会自动写回本文件
ALERT_EMAIL_TO=your@email.com
```

> ⚠️ **Google Cloud 的 API Key（`AIza...`）不能发信**。API Key 只标识「哪个项目」，不代表用户身份；发信是以「某个用户」的身份进行，必须用 **OAuth 客户端 ID + 客户端密钥** 换来的 refresh token。这是两套完全不同的凭证，别混用。

**一条命令搞定（授权 → 自动启用 Gmail API → 发信自检）：**

```bash
python -m src.notify --enable-gmail-api
```

相关命令：

```bash
python -m src.notify --oauth-login                 # 只做授权（自己去 Console 点「启用」）
python -m src.notify --oauth-manual                # 浏览器连不上 localhost:8765 时：把地址栏整条 URL 粘回终端
python -m src.notify --oauth-exchange "<URL或code>" # 兑换已经拿到的 code（只能兑换一次、约 10 分钟有效）
python -m src.notify --only-email                  # 只重试发邮件（不打扰 Lark）
python -m src.notify --preview                     # 不发送，只渲染邮件 HTML + Lark 卡片 JSON 到 reports/
python -m src.notify --open-test                   # 验证"能不能自动唤起系统浏览器"
```
附加开关：`--no-write`（不写回 `environment.env`）。

**`send_email()` 双通道逻辑**：若配了 `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN` → 走 Gmail API；失败**自动回退 SMTP**；两条都失败才返回 `False`。

### 3.3 Gmail 踩坑清单（全部实测，含根因与解法）

| # | 现象 | 根因 | 解法 |
|---|---|---|---|
| 1 | `Connection unexpectedly closed` | Gmail 收到错误密码后**立即断连**；而 `smtplib` 会先试 `AUTH PLAIN` 失败再自动换 `AUTH LOGIN`，把真实可读的 `535 BadCredentials` **掩盖成断连异常** | 已在 `login()` 前锁死 `server.esmtp_features["auth"]="PLAIN"`（只试一种机制），并单独捕获 `SMTPAuthenticationError` 打印可操作提示 |
| 2 | `535 BadCredentials` | `SMTP_PASS` 填的是账号登录密码 | 必须用「应用专用密码」；但该方案 Google 已下线 → 换方案 A |
| 3 | `_ssl.c:999: The handshake operation timed out` | 国内网络对 `smtp.gmail.com` **TCP 能连（`nc -z` 通过）但 TLS 被黑洞**。实测**矩阵：465/587 × 代理/直连 四条路全部 8s 超时** | 结论：**SMTP 整条不可用 → 必须换走 443 的 Gmail API**。教训：`nc -vz 通 ≠ 端口可用`，有本地代理/透明网关时 SYN 由代理代答，**必须做一次真实 TLS 握手才算数** |
| 4 | OAuth 同意后浏览器跳 `http://localhost:8765/?code=...` 却 `ERR_CONNECTION_REFUSED` | ① macOS Chrome 把 `localhost` 解析成 `::1`，老代码只绑了 `127.0.0.1`；② 老代码 `handle_request()` **只服务一个连接**，浏览器预取 `/favicon.ico` 就把"唯一一次机会"吃掉 | 已修：回调服务器 **IPv4/IPv6 双栈监听**（`IPV6_V6ONLY=0`）+ **循环服务**（~120s 窗口）+ 杂包回 `204` + **先起服务器再开浏览器** + 拿到 code 自动把 `GMAIL_REFRESH_TOKEN` 写回 `environment.env`。兜底用 `--oauth-manual` / `--oauth-exchange` |
| 5 | `403: Gmail API has not been used in project … or it is disabled` | **这是 403 不是 401**：身份验证没问题，是 **Gmail API 没在项目里启用** | `--enable-gmail-api` 自愈：重授权时 scope 加 `https://www.googleapis.com/auth/cloud-platform` → 代码 POST Service Usage API 启用 → 轮询 `getProfile` 确认生效 → 发信自检。若调 Service Usage 也 403（无项目 Owner 权限），日志直接给 Console 启用链接，手点一下也行 |
| 6 | 想探测项目号却 `403 insufficient authentication scopes` | 只带 `gmail.send` scope 的 token 访问 `getProfile` 权限不足 | 改用**发信端点**探测：API 未启用时 Google 先返回 `accessNotConfigured`，正文里带 `project <号码>`（实测 `849097115397`）；空 MIME 探测**不会真发信**。教训：探测某接口前先确认手上凭证有权限访问它 |
| 7 | 浏览器"已打开"却毫无动静 | macOS 上 Python `webbrowser` 走 `osascript`（需「自动化」授权），未授权时**静默失败** | `open_browser()` 三级兜底：**`/usr/bin/open`（LaunchServices，不依赖授权）→ `open -a "Google Chrome"` → `webbrowser`**，每条都检查 returncode 并打印**实际命中的方式**。自检 `--open-test` |

### 3.4 方案 B（备选）：SMTP + 应用专用密码

仅当账号仍可用、且线路未被阻断时：

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=your@gmail.com
SMTP_PASS=你的Gmail应用专用密码
```

### 3.5 代理（邮件 / Google API / Etherscan 抓取共用一条出口）

- 本机常用 Clash 混合端口：`http://127.0.0.1:7897`。
- 优先级：`FETCH_PROXY` > `HTTPS_PROXY/HTTP_PROXY` > `NET_PROXY`（= `SMTP_PROXY`）。
- **只填 `FETCH_PROXY` 时，`SMTP_PROXY` / `NET_PROXY` 自动复用它**（只维护一条出口线）。
- 特殊值 `direct` / `none` / `off` = **强制直连**（连系统代理都不读）。
- **容器里没有系统代理**：要用宿主代理必须显式填 `FETCH_PROXY=http://host.docker.internal:7897`（compose 已配 `extra_hosts`）。
- HTTP 调用统一「**代理优先、直连兜底**」，日志标注实际走哪条（`经代理` / `直连`）。

---

## 4. 交付层（第 ⑤ 步）与四个去向

- 交付实现**只有一份**：`src/deliver.py`。容器（`run_pipeline.py --deliver`）与宿主机（`scripts/daily_run.py`）**共用同一口径**。
- `deliver(...)` 关键参数：`send=`（是否发邮件/Lark）、`open=`（是否自动打开状态页）。
- 四个去向：**① 邮件 ② Lark 交互卡片 ③ `reports/status.html` 运行状态页（交付完成会**自动用默认浏览器打开**）④ 快照 + `reports/last_run.json`**。
- **Lark 简报每天例行推送、不受阈值门控**（避免指标回落时群机器人彻底哑掉）；**邮件告警受阈值门控**：
  `ALERT_CHURN_RATE_THRESHOLD=0.40`（预测流失率）/ `ALERT_RISK_CLUSTER_RATIO=0.25`（高危簇占比）。
- 多时间窗：`FORECAST_HORIZONS=1,7,14,30,90`，一次 ARIMA 拟合同时输出；`RISK_*_MULTIPLIER` 决定 🟢/🟡/🟠/🔴 等级。
- **容器内无 GUI**（`CHURN_HEADLESS=1`）：容器只负责「生成 + 推送」，**弹 macOS 通知 / 开浏览器由宿主机负责**（`./start.sh`）。

---

## 5. Docker 关键点

- `docker-compose.yml` 的 `volumes` **必须挂全三个**：`./data`、`./reports`、`./docs`。
  - **少挂 `./docs`** → 容器内 `/app/docs` 为空 → 看板「业务洞察」面板显示「暂无洞察文本，请先运行 churn_model.py」，**哪怕宿主机上早已跑完预测、`insights.md` 就在那儿**。（这正是 `6094949` 修掉的坑）
- ⚠️ **`environment:` 会覆盖 `env_file` 的同名键**。`ADDRESS_LIMIT` 的默认 `2000` 写在 `environment:` 里；若 `environment.env` 写 `2000`、这里却留 `200`，容器会**静默退化成只读测试库**（表现为「看板没更新」）。
- **邮件收件人 / Lark webhook 只放 `environment.env`**，不要写进 compose 的 `environment:`（会覆盖注入值）。
- 容器要用宿主代理：`FETCH_PROXY=http://host.docker.internal:7897`（compose 已配 `extra_hosts: host.docker.internal:host-gateway`）。
- 健康检查：`GET /_stcore/health`；端口 `8501`。
- 常用：`docker compose up -d --build`（重建）、`docker compose restart churn-app`（重启看板）、`docker compose logs -f churn-app`（看进度）。
- 镜像推送：`docker push bonnie333333333/crypto-churn-prediction:latest`（**大层在慢链路上极易超时**，可加 `timeout 300` 限制 + 分层续传重试）。

---

## 6. 高频 Bug 类型：「数据在 A，进程读 B」

本项目**反复出现同一类问题**，排查时**优先怀疑它**：

1. **挂载缺失**：宿主机生成的文件在容器里不可见（`./docs` 漏挂 → 洞察面板空白）。
2. **长驻进程冻结的模块常量**：`ADDRESS_LIMIT / TEST_MODE / DB_PATH` 是**导入时求值**的模块常量；Streamlit 看板即使 `st.cache_data(ttl=60)` 过期，也只会用旧常量去读 `_test` 库 → 「**永远停在 200 节点测试版**」。**改完 env 必须重启看板**（`docker compose restart churn-app` / `pkill -f 'streamlit run app/dashboard.py'`）。看板有**口径自检红条**（`src/config.py: data_mode_drift_notice()`）；命令行自查：`python -c "from src import config; print(config.data_mode_drift_notice())"`（`None` 即一致）。
3. **后端已更新但前端未刷新**：浏览器 ⌘⇧R 硬刷新。

---

## 7. 文件地图

```
src/        config.py             # 全部配置/env 加载 + 口径自检（data_mode_drift_notice）
            data_fetcher.py       # Etherscan 抓取（显式代理 + 回落直连 + 进度条）
            feature_engineer.py   # 六大类特征
            cluster_analyzer.py   # HDBSCAN 聚类
            churn_model.py        # LightGBM 三模型对比 + SHAP + ARIMA 多时间窗 → 写 docs/insights.md
            deliver.py            # 交付层（邮件/Lark/状态页/通知），唯一一份
            notify.py             # 邮件(Gmail API/SMTP) + Lark 卡片 + macOS 通知 + 浏览器唤起 + OAuth
            status_page.py        # reports/status.html
            db.py / progress.py
app/        dashboard.py          # Streamlit 16 面板看板
scripts/    run_pipeline.py       # 全流程（--deliver 交付四去向；--no-send/--no-open 自检）
            daily_run.py          # --deliver-only 只交付
            push_blobs_resumable.py / split_site_packages.py
templates/  alert_email.html      # 预警邮件 HTML（{{占位符}}）
docs/       development-log.md    # 详细排障日志（§8/§10/§19/§21 全是邮件/代理/看板坑）
            insights.md           # 看板「业务洞察」数据源
data/       crypto_churn.db（全量） / crypto_churn_test.db（测试）
reports/    status.html / last_run.json / email_preview.html / lark_card.json ...
tests/      test_pipeline.py      # 当前 42 passed
```
- 依赖（`requirements.txt`）：`xgboost==2.1.4`（**必须锁版本**：3.x 会在 Linux 上拉 GB 级 CUDA 轮子）；其余 `requests / pandas / numpy / scikit-learn / hdbscan / lightgbm / shap / plotly / streamlit / statsmodels / kaleido / python-dotenv / pytest`。

---

## 8. 安全 / 提交纪律

- **绝不提交**（已在 `.gitignore`）：`environment.env`、`Etherscan_api.txt`、`*.db`、`reports/*.json|html|png`、`*.png`。
- 提交前若推送被拒（远端有新提交）：`git pull --rebase origin main` 再 `git push origin main`。
- 校验：改完代码跑 `python -m pytest tests/ -q`（应 42 passed）与 `python -m py_compile src/notify.py`。

---

## 9. 新 session 交接第一步（照做即可）

```bash
# 1) 备好配置（至少 ETHERSCAN_API_KEY；邮件按 §3 配）
cp environment.env.example environment.env

# 2) 先验证两条最容易出问题的链路
python -m src.notify --enable-gmail-api   # 授权 + 自动启用 Gmail API + 发信自检
python -m src.notify --open-test          # 验证能否自动唤起系统浏览器

# 3) 跑全流程 + 交付四去向（或容器一键 ./start.sh）
python scripts/run_pipeline.py --deliver

# 4) 回归测试
python -m pytest tests/ -q                 # 期望 42 passed
```

> 若换了网络环境导致邮件/抓取失败：**先只改 `FETCH_PROXY`（或 `NET_PROXY` / `SMTP_PROXY`）一个变量**，
> 代理/直连会自动互为兜底；国内网络下 SMTP 基本不可用，**邮件请始终走 Gmail API（§3.2）**。

