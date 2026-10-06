# 🪙 加密货币用户行为聚类分析与流失预测
### Crypto User Behaviour Clustering & Churn Prediction

[中文](README.md) | [English](README_en.md)

一个**批量离线**的链上用户行为分析系统：从 Etherscan 拉取以太坊地址近 6 个月的交易，
做特征工程 → HDBSCAN 聚类画像 → LightGBM/XGBoost/RandomForest 流失预测 + SHAP 可解释性 +
ARIMA 多时间窗频次预测 → Streamlit 16 面板看板，并支持**定时任务**与**邮件/Lark 预警**。

每次运行都会产出四个「看得见结果」的出口，且**都包含同一套内容**：
**运行状态 + 报错内容 · 风险等级（是否高危）· 多时间窗流失率 · 聚类结果 · 业务洞察 · 初步推进建议**。

- **数据源**：Etherscan API V2（普通 / 内部 / ERC-20 三类交易）
- **存储**：SQLite（`data/crypto_churn[_test].db`）
- **聚类**：HDBSCAN（识别变密度簇 + 噪音点）
- **分类**：LightGBM（主）/ XGBoost / RandomForest 三者对比
- **可解释性**：SHAP（全局 + 单样本）
- **时间序列**：ARIMA，**一次拟合同时输出未来 1 / 7 / 14 / 30 / 90 天**频次与流失率
- **可视化**：Streamlit + Plotly（16 panels）
- **推送**：HTML 邮件 · **Lark 交互卡片（真加粗 + 彩色标题）** · macOS 通知（**点击直达运行状态页**）
- **运行状态页**：`reports/status.html`（三方向入口 + 结果内容 + 报错详情，本地双击即可看）
- **交付**：Docker 单镜像；`docker compose up`（或 `./start.sh`）一键 = 跑全流程 + **顺手交付四个去向**（邮件/Lark/状态页/快照）+ 开看板

> ⚠️ **关于样本量（重要）**：过去 24h 的活跃节点约 **12000** 个，**本机跑不动**
> （免费 Etherscan Key 限流约 5 请求/秒，2000 个地址即需数小时）。因此本项目**默认只取 2000 个
> 地址做「链路可行性验证」**：证明 抓取 → 特征 → 聚类 → 三模型预测 → ARIMA 多窗口 →
> 邮件/Lark/看板 三通道端到端能跑通。公司内部 Etherscan API（或自建归档节点）就绪后，
> 把 `ADDRESS_LIMIT` 调到 `12000`、放宽 `MONTHS_BACK` 即可在**同一套代码**上跑全量。


## 🎯 结果口径：这个项目到底在算什么

> 看结果之前先看「尺子」。下面这张表就是本项目的度量衡 —— **换任何一个口径，结论都要重算**。

| 口径维度 | 本项目设定 | 配置项 | 具体含义 |
| --- | --- | --- | --- |
| **时间窗（回看）** | 近 **6 个月** | `MONTHS_BACK=6` | 只统计 `now-6M` 之后的链上交易，更早的交易不进特征 |
| **流失定义** | **30 天无任何交易** | `CHURN_DAYS=30` | `last_tx_days_ago > 30` ⇒ 标记为「已流失」（监督学习的标签口径） |
| **预测窗口** | 主窗 30 天 + 1/7/14/30/90 天多窗 | `FORECAST_HORIZON_DAYS=30`、`FORECAST_HORIZONS=1,7,14,30,90` | 一次 ARIMA 拟合同时给出各窗口的**人均日频次**与**累计流失率** |
| **链 / 币种** | 以太坊主网（`CHAIN_ID=1`），以 **ETH + ERC-20 代币**计量 | `CHAIN_ID`、`ETHERSCAN_API_BASE_URL` | Etherscan V2 支持多链，改 `CHAIN_ID` 即可切 BSC / Polygon / Arbitrum… |
| **用户人群** | 10 个高活跃**种子地址**（交易所热钱包 / 巨鲸 / 基础设施）→ BFS 扩展其**交易对手方** → 取前 `ADDRESS_LIMIT` 个 | `ADDRESS_LIMIT=2000`、`SEED_ADDRESSES` | 本轮 = **2000** 个地址；不是全网用户，而是「以种子为中心的活跃子网」 |
| **产品线 / 交易类型** | 普通转账 `txlist` + 内部调用 `txlistinternal` + **ERC-20 transfer** `tokentx` | 抓取阶段固定三类 | 不含 NFT / 合约内部事件 / 跨链桥跨链量（需额外 endpoint） |
| **风险口径** | 高危簇占比 + 风险等级倍数分档 | `ALERT_RISK_CLUSTER_RATIO=0.25`、`RISK_*_MULTIPLIER` | 高危簇＝被聚成「高频套利者 / 噪音·机器人」的地址；等级＝预测流失率 ÷ 阈值的倍数（0.75/1.0/1.5×） |

**这些数字意味着什么（单次运行）**

| 输出 | 含义 | 怎么用 |
| --- | --- | --- |
| 预测流失率 18.0%（30 天） | 这批 2000 个活跃地址里，模型预测未来 30 天约 18% 会从「活跃」变「沉默」 | 召回活动的规模预算（触达上限 ≈ 18% × 活跃用户数） |
| 高危地址占比 32.1% | 约 1/3 地址属于「高频套利者 / 噪音·机器人」这类高流失簇 | 风控/女巫识别优先，别把留存预算花在刷量地址上 |
| 1940 个地址 → 34 个簇 | 人群画像的分层结构（persona + 占比 + 雷达图） | 分群运营：大户守住、噪音清理、DeFi 农民加权益 |
| SHAP Top 特征 | 「为什么会流失」的可解释归因（如 `token_tx_ratio`、`eth_balance_end`） | 定召回话术与触达渠道 |
| 1/7/14/30/90 天多窗 | 短窗 vs 长窗的**加速 / 减速**信号 | 短窗 > 长窗 = 加速出逃，需要立刻止血 |

**这些数字意味着什么（数据资产化，长期）**

- 每次运行都把当日结果写进 `daily_snapshots` 表，并保留 `reports/last_run.json`；
- 累积 N 天后即可做**留存曲线 / 流失率趋势 / 模型回测**（同一口径横向对比）——
  这就是「数据资产化」：**单次运行给结论，长期运行给趋势**；
- 定时任务 `scripts/daily_run.py`（见下文「每日调度」）就是资产化的采集器。

**必须知道的边界（别误读）**

1. **样本量**：默认 / 本轮 2000 个地址，是**链路可行性验证**而非全网（全量约 12000 个活跃节点，需内部 API 或归档节点）；
2. **限流**：免费 Etherscan Key ≈ 5 req/s，2000 个地址要数小时 —— 想要更长时间窗 / 更大样本，先看下文「想要 24h 或更长时间窗的结果」；
3. **口径自定**：「活跃 / 流失 / 高危」是可配置的工程口径，与公司内部口径不同，落地前必须对齐；
4. **粒度**：地址级（address-level），不做自然人 / 机构实体归并（需要额外的实体解析）。

---

## 🏗️ 架构图 (Mermaid)

```mermaid
flowchart TD
    A["Etherscan V2 API<br/>txlist / txlistinternal / tokentx"] -->|"分页 + 限流 + 重试"| B["src/data_fetcher.py<br/>地址BFS扩展 → raw_transactions"]
    B --> C["src/feature_engineer.py<br/>六大类特征 → address_features"]
    C --> D["src/cluster_analyzer.py<br/>StandardScaler + HDBSCAN<br/>→ address_clusters + 饼图/雷达图"]
    C --> E["src/churn_model.py<br/>LightGBM/XGBoost/RF + SHAP + ARIMA<br/>→ churn_predictions / model_metrics / churn_forecast / forecast_daily"]
    D --> F["app/dashboard.py<br/>Streamlit 16 面板 (端口 8501)"]
    E --> F
    E --> G["src/notify.py<br/>HTML邮件 + Lark 交互卡片 + macOS通知"]
    H["scripts/daily_run.py<br/>每日 15:30 (Cline Schedule)"] -->|"串起全流程 + 快照"| B
    H --> G
    H --> I["daily_snapshots<br/>累积数据资产"]
    H --> K["src/status_page.py<br/>reports/status.html<br/>①运行状态+报错 ②三方向入口 ③结果内容"]
    G -->|"terminal-notifier -open"| K
    I --> F
    J["Dockerfile + docker-compose.yml"] --> F
```

**执行顺序**：`data_fetcher` → `feature_engineer` → `cluster_analyzer` → `churn_model` →
`streamlit run app/dashboard.py` → 浏览器打开 `http://localhost:8501`。

---

## 🧰 技术栈

`requests` · `pandas` · `numpy` · `scikit-learn` · `hdbscan` · `lightgbm` · `xgboost` ·
`shap` · `statsmodels`(ARIMA) · `plotly` · `streamlit` · `python-dotenv`

---

## 📁 目录结构

```
crypto_churn_prediction_project/
├── src/
│   ├── config.py            # 集中配置 + 测试模式后缀逻辑
│   ├── db.py                # SQLite schema + 连接
│   ├── data_fetcher.py      # ① 抓取
│   ├── feature_engineer.py  # ② 特征
│   ├── cluster_analyzer.py  # ③ 聚类
│   ├── churn_model.py       # ④ 流失预测 + SHAP + ARIMA（1/7/14/30/90 天多窗口）
│   ├── notify.py            # 邮件 / Lark 卡片 / macOS 告警 + 简报/建议生成
│   ├── deliver.py           # 四个去向的统一交付（本机 daily_run 与容器 --deliver 共用）
│   └── status_page.py       # 运行状态页 reports/status.html（通知点击直达）
├── app/dashboard.py         # ⑤ Streamlit 16 面板
├── scripts/
│   ├── run_pipeline.py      # 一键串起 ①→④（容器首启加 --deliver 交付四去向）
│   └── daily_run.py         # 每日 15:30 调度 / 四去向 / 快照（--deliver-only 可只交付）
├── templates/alert_email.html  # 预警邮件 HTML（{{占位符}}）
├── reports/                 # 产物：图表 / HTML 日报 / status.html / email_preview.html / lark_card.json
├── docs/
│   ├── development-log.md   # 技术决策 + 踩坑
│   └── insights.md          # 业务洞察（自动生成）
├── tests/test_pipeline.py   # 单元测试（无网络）
├── Dockerfile / docker-compose.yml / docker-entrypoint.sh / start.sh
├── environment.env.example / requirements.txt
└── README.md
```

---

## 🚀 本地部署和运行（三种方式）

### 方式 A：直接拉取镜像（最省事，不想 build）

```bash
# 拉取已构建好的镜像
docker pull bonnie333333333/crypto-churn-prediction:latest

# 一键跑全流程并启动看板（挂载宿主 ./data 持久化数据库）
docker run -d --name crypto-churn-app -p 8501:8501 \
  -e ETHERSCAN_API_KEY=你的Key \
  -e ADDRESS_LIMIT=200 \
  -v "$PWD/data:/app/data:rw" \
  bonnie333333333/crypto-churn-prediction:latest

# 打开 http://localhost:8501
```

### 方式 B：一句命令拉起（推荐）：`./start.sh`

```bash
# 1) 准备环境变量
cp environment.env.example environment.env
#    编辑 environment.env：填入 ETHERSCAN_API_KEY（可选 ADDRESS_LIMIT / 邮件 / Lark）

# 2) 一键：构建镜像 -> 首启跑完整流水线 -> 交付四个去向 -> 起看板 -> 自动打开页面
./start.sh
#    等价于：docker compose up -d --build（带进度） + 自动打开
#            http://localhost:8501（看板） 与 ./reports/status.html（运行状态页）
```

**进度是「看得见」的**（不会闷声在后台跑）：

```
============================================================
 🪙 加密货币用户行为聚类分析与流失预测 · 流水线
步骤：① 抓取链上交易 → ② 特征工程 → ③ 行为聚类 → ④ 流失预测 → ⑤ 交付四去向
============================================================
[1/5] ▶ 抓取链上交易（Etherscan） …
已抓取 1240/2000 地址 [█████████████░░░░░░░░░░░]  62% 1240/2000 抓取地址 | 新增 715382 条 | 队列 813 | 用时 9432s
✅ [1/5] 抓取链上交易（Etherscan）完成（23401.2s，总耗时 23401s）
[2/5] ▶ 特征工程（六大类特征） …
```

- `./start.sh` 会把**容器日志实时流到你的终端**（阶段编号 + 进度条 + 百分比），跑完自动打开状态页与看板；
- 或者前台跑（日志天然可见）：`docker compose up --build`；
- 后台跑也可以随时看：`docker compose logs -f churn-app`。

`docker-entrypoint.sh` 会在**首次启动**时自动按顺序执行：

```
data_fetcher → feature_engineer → cluster_analyzer → churn_model
        → --deliver（邮件 + Lark 卡片 + reports/status.html 运行状态页 + 快照/last_run.json）
        → streamlit run app/dashboard.py
```

之后重启会直接启动看板（用 marker 判断，`FORCE_PIPELINE=1` 可强制重跑）；若此时想**补发**
四去向，容器会用 `scripts/daily_run.py --deliver-only` 复用已有的 `churn_summary.json`
重发一遍（不会重跑阶段、不会重复抓取）。

**关键点：容器与 compose 只负责「生成 + 推送」，不负责「弹通知 / 开页面」**

| 动作 | 谁做 | 说明 |
| --- | --- | --- |
| 邮件 / Lark 卡片 / 状态页生成 | **容器内**（`run_pipeline.py --deliver`） | 与本机 `daily_run.py` 共用 `src/deliver.py`，**同一口径** |
| 状态页落盘 | 容器写 `/app/reports/status.html` → 挂载到宿主机 `./reports/status.html` | 宿主机双击即可看 |
| macOS 通知 | **宿主机**（`start.sh` 结束后 / `daily_run.py`） | 容器无 GUI，`CHURN_HEADLESS=1` 时交付层只记一行日志 |
| 打开看板 / 打开状态页 | **宿主机**（`start.sh`） | 容器里 `open`/`webbrowser` 无效 |

- 只想要数据、不想推送：`RUN_DELIVER=0 ./start.sh`
- 不想自动开浏览器（CI / 远程终端）：`NO_OPEN=1 ./start.sh`
- 想要 2000 个地址的全量验证：`ADDRESS_LIMIT=2000 ./start.sh`
- 容器内若想手动补发一次：`docker compose exec churn-app python scripts/daily_run.py --deliver-only`
- 停止：`docker compose down`（数据留在 `./data`、报告留在 `./reports`）

> 浏览器：打开 <http://localhost:8501> 即直接看到看板（Streamlit 无需登录）。
> 如果 `./start.sh` 结束时代浏览器没有自动弹出，它会打印**实际使用的打开方式**与
> `file://` / `http://` 链接，复制即可访问；自检命令：`python -m src.notify --open-test`。

### 方式 C：手动一步步依次跑脚本（本地 conda，便于调试）

```bash
# 0) 激活环境 + 安装依赖
conda activate crypto_churn_prediction_project      # Python 3.11
# 没有就先： conda create -n crypto_churn_prediction_project python=3.11 -y
pip install -r requirements.txt

# 1) 配置密钥
cp environment.env.example environment.env           # 填入 ETHERSCAN_API_KEY

# 2) 按顺序执行（测试用 ADDRESS_LIMIT=200，产物带 _test 后缀；本机验证规模 2000）
python src/data_fetcher.py           # 抓取 → raw_transactions
python src/feature_engineer.py       # 特征 → address_features
python src/cluster_analyzer.py       # 聚类 → address_clusters + 图表
python src/churn_model.py            # 预测 → churn_predictions / model_metrics / churn_forecast
streamlit run app/dashboard.py       # 看板 → http://localhost:8501
```

也可以用一键脚本（等价于上面 ①→④）：

```bash
python scripts/run_pipeline.py          # 全流程（含抓取）
python scripts/run_pipeline.py --skip-fetch   # 复用已有原始数据
python scripts/daily_run.py --dry-run   # 模拟每日任务（不联网抓取）
```

> **测试模式说明**：`ADDRESS_LIMIT<=200` 时判定为测试模式，所有产物文件名自动加 `_test`
> 后缀（如 `data/crypto_churn_test.db`、`reports/cluster_pie_test.png`），避免污染全量数据集。
> 本机验证规模为 `2000`（免费 Etherscan Key 限流下约需数小时）；**全量约 12000 个节点**
> 需公司内部 Etherscan API / 归档节点，把 `ADDRESS_LIMIT` 改为 `12000` 即可。

### 🔧 想要 24h / 更长窗口 / 更大样本的结果：改 `environment.env`

所有「口径」都集中在 **`environment.env`**（模板见 `environment.env.example`）：

| 想要的效果 | 改哪个键 | 默认 | 建议值 / 说明 |
| --- | --- | --- | --- |
| 更长的**回看窗口**（更久的历史行为） | `MONTHS_BACK` | `6` | 要覆盖「24h 活跃」这类更长的观察期就按需拉长（12/18/24）；历史越长，抓取量越大、耗时线性增长 |
| 「流失」判定更严格 / 更宽松 | `CHURN_DAYS` | `30` | 日活口径可设 `7`（一周不活跃即算流失）；`30` = 月活口径 |
| 预测**更远的未来** | `FORECAST_HORIZON_DAYS` + `FORECAST_HORIZONS` | `30` / `1,7,14,30,90` | 例如加 `180` 做半年窗；主窗会自动并入集合 |
| 扩大**样本量** | `ADDRESS_LIMIT` | `200` | 本机验证用 `2000`；全量约 `12000`（需内部 API / 归档节点） |
| 切换**链** | `CHAIN_ID` | `1`（以太坊） | Etherscan V2 多链：`56`=BSC、`137`=Polygon、`42161`=Arbitrum |
| 改完口径后**强制重跑** | `FORCE_PIPELINE=1` | `0` | `FORCE_PIPELINE=1 ./start.sh` |
| 抓取走代理（容器内必须显式给值） | `FETCH_PROXY` | 空 | `http://host.docker.internal:7897`（compose 已加 `extra_hosts`） |

> ⏱️ **耗时预期**：免费 Etherscan Key ≈ 5 req/s，每个地址 3 个 endpoint ⇒ 约 **0.6~1.2 秒/地址**；
> 2000 个地址 ≈ 3~6 小时，12000 个 ≈ 20~40 小时。想要「24h 常驻 / 更长时间跨度」的结果，
> 就把 `scripts/daily_run.py` 挂到定时任务（见下节「每日调度与数据资产化」），**每天增量跑一次**，
> 让 `daily_snapshots` 连续累积 —— **趋势比单次快照更有价值**。

---

## 🔍 各阶段说明

### ① 数据抓取 `src/data_fetcher.py`
从 10 个种子地址出发，**BFS 扩展交易对手方**直到凑够 `ADDRESS_LIMIT` 个地址；对每个地址拉取
普通/内部/ERC-20 三类交易，`INSERT OR IGNORE` 幂等入库，分页 + 限流 + 指数退避重试。

### ② 特征工程 `src/feature_engineer.py`（六大类特征）
交易频次（日/周/月）、平均持仓时长（收到→转出，FIFO 配对）、Gas 消耗模式（高 Gas 占比/均价）、
协议多样性（交互合约数）、代币行为（不同代币数/占比）、资产规模变化（ETH 余额趋势斜率）。

### ③ 聚类 `src/cluster_analyzer.py`
`StandardScaler` 标准化 → **HDBSCAN** 聚类 → 按质心自动打 persona 标签
（**高频大户 / 长期持有者 / DeFi农民 / 高频套利者**，噪音点单独标记）→ 输出簇分布饼图、
簇画像雷达图、t-SNE 坐标。

> **为何用 HDBSCAN 而非 K-Means**：链上数据密度极不均匀（少数超级大户 + 长尾静止地址），
> K-Means 假设球形等方差且强制每个点归簇；HDBSCAN 能识别变密度簇并把"机器人/一次性地址"
> 判为**噪音点**，簇更紧凑、可解释性更强。

### ④ 流失预测 `src/churn_model.py`
- **标签**：`last_tx_days_ago > 30` 记流失。
- **三模型对比**：LightGBM（主）/ XGBoost / RandomForest，输出 AUC / F1 / Precision / Recall，
  并在洞察中对比"三种针对离散标签的建模方式谁更科学"。
- **SHAP**：全局特征重要性 + **单样本解释**（如"协议多样性从 5 降到 1 → 流失概率上升 40%"）。
- **频次 × 流失率相关性**：按 `tx_freq_daily` 分桶统计真实流失率，定位**危险频次区间**。
- **ARIMA 预测（多时间窗）**：对每个地址的日交易笔数序列拟合一次 `ARIMA(1,1,1)`，**拟合到最大窗口**
  （默认 90 天），再**一次性输出 1 / 7 / 14 / 30 / 90 天**每个窗口的：平均日频次、频次降低值、
  平均流失率、末日流失率。这样避免了「每个窗口各拟合一次」的 5 倍算力，并额外得到
  **短窗 vs 长窗对比结论**（短窗流失率 ≥ 长窗 ⇒ 正在「加速出逃」，需立刻干预；
  否则属于「渐进式失活」，仍有挽回窗口）。

### ⑤ 可视化 `app/dashboard.py`（16 panels）
1 核心指标卡 · 2 簇分布饼图 · 3 簇画像雷达图 · 4 t-SNE 散点 · 5 模型指标表 · 6 三模型对比 ·
7 各簇流失率 · 8 流失概率分布 · 9 SHAP 全局 · 10 SHAP 单样本 · 11 频次×流失率 · 12 未来30天预测 ·
13 高危地址 TOP20 · 14 累积数据资产 · 15 管道运行健康 · 16 业务洞察文字区。

---

## ⏰ 每日调度与数据资产化

用 **Cline Schedule** 确保每天 **15:30** 自动运行、持续累积数据资产：

```bash
# 通过 Cline 创建一个每日 15:30 的定时任务（cron: 30 15 * * *）
# 任务内容示例：
cd ~/Desktop/crypto_churn_prediction_project && \
  conda run -n crypto_churn_prediction_project python scripts/daily_run.py
```

或系统 cron：

```bash
# 0 分 15 时（即 15:30... 见下方 30 15）—— 每天 15:30
30 15 * * * cd ~/Desktop/crypto_churn_prediction_project && \
  conda run -n crypto_churn_prediction_project python scripts/daily_run.py >> logs/cron.log 2>&1
```

`scripts/daily_run.py` 每次会：跑全流程 → 写入 `daily_snapshots`（看板第 14 个面板展示日累积曲线）
→ 生成 HTML 报告到 `reports/` → 生成 **运行状态页 `reports/status.html`** 并**自动在浏览器打开** →
**成功或失败都弹 macOS 通知**（`terminal-notifier`，**点击通知直接打开状态页**）→
按阈值决定是否发预警邮件/Lark 卡片 → 把本次运行结果写入 `reports/last_run.json`。

### 📄 运行状态页 `reports/status.html`

一次运行的全部「可交付内容」都收敛到这一页（本地双击即可打开，不依赖服务）：

| 区块 | 内容 |
| --- | --- |
| 顶部 | ✅/❌ 运行状态徽章 + 🟢🟡🟠🔴 风险等级（×阈值倍数）+ 生成时间/耗时/预测窗口 |
| 一、运行状态 + 报错内容 | 每个阶段（① 数据抓取 / ② 特征工程 / ③ 聚类 / ④ 流失预测）的 ok/fail/detail/时间；失败时给出**报错原文** |
| 二、三个方向的入口与结果 | 📧 邮件（收件人 + HTML 预览链接）· 💬 Lark（卡片 JSON 原文链接）· 📊 Streamlit 看板（可点链接） |
| 三、结果内容 | 主窗口流失率 · **1/7/14/30/90 天多时间窗流失率（越线标 ⚠️）** · 高危占比 · 频次/降幅 · 样本量 |
| 四、聚类结果 | 每簇规模/实际流失率/日均笔数/持仓/闲置（与 Lark 卡片文字一致） |
| 五、业务洞察 | `docs/insights.md` 全文 |
| 六、初步推进建议 | 模板规则生成的行动 + 责任方（增长/运营、风控、客户成功…） |
| 附 | Lark 推送原文（折叠）+ 样本量说明 |

> **一次性设置（仅 macOS，可选）**：想用「点击通知直接跳转」，需允许 `terminal-notifier` 发通知：
> 系统设置 → 通知 → **Terminal Notifier** → 允许通知。可以直接打开该面板：
> `open "x-apple.systempreferences:com.apple.Notifications-Settings.extension"`。
> 本项目已把 app bundle 真实拷贝到 `~/Applications/Terminal Notifier.app` 并向 LaunchServices
> 注册（`lsregister -f`），否则 macOS 只会报 `Notifications are not allowed for this
> application` 而不会在设置里列出它。若未授权，代码会**自动回退**到 `osascript` 普通横幅
> （并在日志里给出上述开启提示），且状态页仍会被自动打开，功能不丢。

> **Docker / compose 下这一页怎么来？** 容器首启会跑
> `run_pipeline.py --deliver`（或补发时 `daily_run.py --deliver-only`），把状态页写进
> 挂载目录 → 宿主机 `./reports/status.html`；**通知与「打开页面」由宿主机 `./start.sh`
> 负责**（容器无 GUI）。详见 [方式 B](#方式-bdocker-compose-up-一句命令拉起推荐自动跑脚本--自动开网页)。

---

## 🔔 预警推送：邮件 + Lark 卡片 + macOS 通知 + 运行状态页

四个出口由 `src/deliver.py`（交付调度）+ `src/notify.py` + `src/status_page.py`
统一构建、分发，**内容同源、口径一致**：

| 出口 | 推送内容 | 触发条件 |
| --- | --- | --- |
| **邮件** | HTML 卡片（`templates/alert_email.html` 占位符填空）：**运行状态 + 风险等级 + 多时间窗 + 核心指标 + 触发原因 + 推进建议** → `ALERT_EMAIL_TO` | 预测流失率 ≥ 40% 或高危占比 ≥ 25% |
| **Lark / 飞书群机器人** | **交互卡片**（`msg_type: interactive` + `lark_md`）＝ ① **运行状态 + 报错内容** ② **风险等级（🟢🟡🟠🔴 + ×阈值 + 处置建议）** ③ **1/7/14/30/90 天多时间窗流失率（越线标 ⚠️）** ④ 一、流失预警 ⑤ 二、文字描述形式的用户行为聚类结果 ⑥ 三、自动提炼的 insights ⑦ **初步推进建议（含责任方）** ⑧ 看板链接；标题栏颜色随状态/风险变化（失败或🔴高危＝红头） | **每天例行推送**，不受阈值门控；阈值触发时预警段标 ⚠️ |
| **macOS 通知** | `terminal-notifier` 横幅（标题＝✅/❌ + 流失率 + 风险等级），**点按即打开 `reports/status.html`** | 成功与失败都推送 |
| **运行状态页** | `reports/status.html`：①运行状态+报错 ②三方向入口与结果 ③结果内容 ④聚类 ⑤洞察 ⑥建议 | 每次运行都生成并自动打开 |

> **关于「Lark 只能是 plain text 吗」**：不是。群机器人 webhook 支持 `msg_type: "interactive"`
> （消息卡片），卡片内文本用 `lark_md` 方言即可获得**真加粗**、彩色标题栏、分割线、超链接与 @；
> `msg_type: "post"`（富文本）也能加粗但没有彩色标题栏。因此简报用**卡片**发送，同时保留一段
> **纯文本兜底**（卡片发送失败时自动降级，见 `send_lark_digest()`）。

### 🧭 三步配好四个去向（照着做就行）

#### 1️⃣ 本地要提前准备好的东西

| 需要什么 | 必需？ | 在哪拿 | 填到哪 |
| --- | --- | --- | --- |
| Python 3.11（手动跑）或 Docker Desktop（一键跑） | ✅ | conda / docker.com | — |
| **Etherscan API Key**（V2，免费） | ✅（不填则不抓数，只能看已有 DB） | <https://etherscan.io/myapikey> | `ETHERSCAN_API_KEY` |
| **邮件**：Gmail OAuth 客户端（推荐） | 想要邮件就要 | Google Cloud Console → 新建项目 → **启用 Gmail API** → OAuth 客户端（类型：**桌面应用**） | `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`（`GMAIL_REFRESH_TOKEN` 由 `--enable-gmail-api` 自动写回） |
| 邮件备选：SMTP + 应用专用密码 | 部分网络/账号可用 | <https://myaccount.google.com/apppasswords> | `SMTP_USER` / `SMTP_PASS` |
| **Lark 群机器人** | 想要 Lark 卡片就要 | 群设置 → 群机器人 → 添加「自定义机器人」→ 复制 Webhook | `LARK_WEBHOOK_URL` |
| 代理（如 Clash `127.0.0.1:7897`） | 国内网络建议 | 你的机场 / 自建 | `NET_PROXY` / `SMTP_PROXY` / `FETCH_PROXY` |

> 只填一部分也能跑：缺哪个通道，交付层只**记一行警告**，其余通道照常出结果。

#### 2️⃣ 两种配置方式

**① 一键（推荐）**

```bash
cp environment.env.example environment.env   # 填上表里的值（至少 ETHERSCAN_API_KEY）
./start.sh                                   # 带进度；跑完自动打开状态页 + 看板
```

**② 手动逐步（便于调试、分步验证）**

```bash
conda activate crypto_churn_prediction_project
pip install -r requirements.txt
cp environment.env.example environment.env

# a) 先单独验证「邮件 + 浏览器唤起」两条最容易出问题的链路
python -m src.notify --enable-gmail-api   # 一次性：授权 → 自动启用 Gmail API → 发信自检
python -m src.notify --open-test          # 验证「能不能自动唤起系统浏览器」
python -m src.notify --preview            # 不发送：只渲染邮件 HTML / Lark 卡片 JSON
python -m src.notify --only-email         # 只重试发邮件（不打扰 Lark）

# b) 跑完整流水线 + 交付四个去向
python scripts/run_pipeline.py --deliver
# 或复用已有预测结果只重发一遍（不重抓数据）
python scripts/daily_run.py --deliver-only
```

#### 3️⃣ 四个去向的链接 / 入口（会自动唤起；失败请手动访问）

| 去向 | 链接 / 入口 | 会自动唤起吗？ | 唤起失败怎么办 |
| --- | --- | --- | --- |
| ① 运行状态页 | `file:///你的项目绝对路径/reports/status.html` | ✅ 交付完成后自动打开（宿主机） | 把这条 `file://` 链接复制到浏览器地址栏 |
| ② 交互看板 | <http://localhost:8501> | ✅ `./start.sh` 结束时自动打开 | 手动访问 `http://localhost:8501` |
| ③ 邮件 | 收件箱（`ALERT_EMAIL_TO`） | —（邮件无需打开） | 先看垃圾邮件；日志 `email=False` 时按其 403 提示处理 |
| ④ Lark 卡片 | 机器人所在群 | —（消息直达群里） | `python -m src.notify` 重发卡片 |
| ⑤ 当日日报 HTML | `file:///.../reports/daily_report_YYYYMMDD.html` | 不自动开 | 从状态页点进去，或直接双击文件 |

> **自动打开是怎么做的**：macOS 上优先用 `/usr/bin/open`（LaunchServices，不依赖「自动化」授权），
> 失败再退 `open -a "Google Chrome"`，最后才用 Python `webbrowser`（osascript）；每次都打印
> **实际命中的方式**，全失败则打印可手动访问的链接。自检：`python -m src.notify --open-test`。

Lark 卡片由 `build_digest_card()` 生成、纯文本由 `build_digest_text()` 生成，实测样例
（`python -m src.notify --preview` 或 `python -m src.notify`）：

```text
📮 加密用户行为聚类 & 流失预测 · 每日简报
2026-10-06 21:45 ｜ 预测时间窗 30 天 ｜ 风险等级 🟠 警戒
————————————
运行状态：✅ 成功（4/4 阶段成功）
完成时间：2026-10-06 21:44 ｜ 耗时 3421 秒
① 数据抓取：ok ｜ 2000 个地址 / 1873921 条交易
② 特征工程：ok ｜ 2000 行特征
③ 聚类：ok ｜ 4 类 + 137 个噪音点
④ 流失预测：ok ｜ 主模型 LightGBM · AUC 0.91

报错内容：无
————————————
🟠 警戒 ｜ 已越过预警线（1.17× 阈值 40%）→ 进入处置流程：高危簇触达 + 召回实验
多时间窗流失率预测（越线标 ⚠️，阈值 40%）
- 1 天：流失率 58.2% ⚠️ ｜ 人均日频 1.42（较当前降 2.21）
- 7 天：流失率 55.1% ⚠️ ｜ 人均日频 1.55（较当前降 2.08）
- 30 天：流失率 51.5% ⚠️ ｜ 人均日频 1.86（较当前降 1.77）
- 90 天：流失率 49.0% ⚠️ ｜ 人均日频 2.05（较当前降 1.58）
- 结论：短窗（1 天）流失率已不低于长窗，说明正在**加速出逃**，需立刻干预。
————————————
一、流失预警（已触发 ⚠️）
预测流失率（30 天）: 51.5%（阈值 40%）
高危流失占比: 67.0%（阈值 25%）　高危行为簇占比: 52.0%
人均日交易频次: 33.8058 → 第 30 天 19.2369（降幅 14.526）
触发原因: 预测 30 天流失率 51.5% ≥ 阈值 40%；高危地址占比 67.0% ≥ 阈值 25%
————————————
二、【用户行为聚类结果】HDBSCAN · 4 类 + 96 个噪音点 · 覆盖 200 个地址
⚠️ 噪音/机器人 · 96 个（48.0%）｜实际流失率 38.5% · 日均 67.10 笔 · …
高频大户 · 55 个（27.5%）｜实际流失率 74.5% · 日均 4.23 笔 · 持仓 8.4h · …
    └ 交易频繁、余额厚，是协议的核心用户
长期持有者 · 31 个（15.5%）｜实际流失率 12.9% · …
    └ 低频但持仓久，粘性最强、流失率最低
————————————
三、业务洞察（从聚类与模型自动提炼）
· 驱动流失的关键特征 (SHAP)
• total_tx: 平均 |SHAP| = 1.1441
· 结论
未来 30 天人均交易频次预计由 33.8058 降至 19.2369 …
————————————
初步推进建议（模板规则生成，非大模型）
- 立刻止血：短窗流失率已高于长窗，属于加速出逃，建议 48h 内上线一轮定向召回（责任方：增长/运营 + 客户成功 ｜ 依据：多时间窗对比）
- 主攻「高频套利者」（55 个 · 占 27.5%）：定向召回 + 费率/返佣实验（责任方：增长 / 运营 ｜ 依据：高频 Gas 占比高、长期闲置，对收益率与费率极敏感）
- 守住基本盘：「长期持有者」实际流失率最高（12.9%），长期激励 / 治理参与提粘性（责任方：产品 ｜ 依据：持仓久、流失率最低）
- 升级报备：当前 🟠 警戒（1.17× 阈值）→ 进入处置流程：高危簇触达 + 召回实验；建议 24h 内拉齐 增长/产品/风控/数据 开 30 分钟对齐会（责任方：业务负责人 + 数据 ｜ 依据：风险等级）
- 扩样本 + 拉长时间窗：本轮 2000 个地址是链路可行性验证（全量约 12000 个节点，本机跑不动）→ 申请公司内部 Etherscan API / 归档节点（责任方：数据/平台工程）
————————————
看板: http://localhost:8501
```

**「初步推进建议」是纯模板规则生成**（`build_suggestions()`：按运行状态 / 风险等级 / 多时间窗
趋势 / 高危簇 / 数据覆盖逐条拼装动作 + 责任方），**不调用任何大模型** —— 稳定、可审计、
每天跑零 token 成本。用 LLM 生成建议的探索方向见 [下一步优化](#-下一步优化)。

```text
📮 加密用户行为聚类 & 流失预测 · 每日简报
2026-10-06 20:22 ｜ 预测时间窗 30 天
————————————
一、流失预警（已触发 ⚠️）
预测流失率（30 天）: 51.5%（阈值 40%）
高危流失占比: 67.0%（阈值 25%）　高危行为簇占比: 52.0%
人均日交易频次: 33.8058 → 第 30 天 19.2369（降幅 14.526）
————————————
二、【用户行为聚类结果】HDBSCAN · 4 类 + 96 个噪音点 · 覆盖 200 个地址
⚠️ 噪音/机器人 · 96 个（48.0%）｜实际流失率 38.5% · 日均 67.10 笔 · …
高频大户 · 55 个（27.5%）｜实际流失率 74.5% · 日均 4.23 笔 · 持仓 8.4h · …
    └ 交易频繁、余额厚，是协议的核心用户
长期持有者 · 31 个（15.5%）｜实际流失率 12.9% · …
    └ 低频但持仓久，粘性最强、流失率最低
————————————
三、业务洞察（从聚类与模型自动提炼）
· 驱动流失的关键特征 (SHAP)
• total_tx: 平均 |SHAP| = 1.1441
· 结论
未来 30 天人均交易频次预计由 33.8058 降至 19.2369 …
————————————
看板: http://localhost:8501
```

在 `environment.env` 中配置：

```env
# —— Lark（配好即可先跑起来）——
LARK_WEBHOOK_URL=https://open.larksuite.com/open-apis/bot/v2/hook/xxxx
# @ 只能填 Lark 的 open_id（ou_ 开头）；填手机号无法 @，代码会自动忽略
LARK_AT_ID=ou_xxxxxxxxxxxxxxxx

# —— 邮件方案 A（推荐）：Gmail API + OAuth2 ——
GMAIL_CLIENT_ID=xxxx.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=xxxx
GMAIL_REFRESH_TOKEN=        # 由 python -m src.notify --enable-gmail-api 自动写回
                            # （该命令同时会：授权 → 自动启用 Gmail API → 发信自检）

# —— 邮件方案 B：SMTP + 应用专用密码（仅在账号仍可用时）——
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=dingbangchu@gmail.com
SMTP_PASS=你的Gmail应用专用密码
ALERT_EMAIL_TO=dingbangchu@gmail.com

# 出站代理（邮件 / Google API / Etherscan 抓取）：
# NET_PROXY / SMTP_PROXY 供邮件与 Google API 用；FETCH_PROXY 供 Etherscan 抓取用
# （只填 FETCH_PROXY 时邮件/Google API 会自动复用它；抓取填 direct 则三者都不走代理）
# 探测端口/线路是否通：FETCH_PROXY=direct python -m src.data_fetcher --limit 5
# （不填也能用 —— 本地 requests 会自动读系统代理；容器里没有系统代理，必须填）
NET_PROXY=http://127.0.0.1:7897
SMTP_PROXY=http://127.0.0.1:7897
FETCH_PROXY=http://127.0.0.1:7897      # 容器里请改成 http://host.docker.internal:7897

ALERT_CHURN_RATE_THRESHOLD=0.40
ALERT_RISK_CLUSTER_RATIO=0.25

# —— 多时间窗预测 + 风险等级（简报/邮件/状态页的「是否高危」口径）——
FORECAST_HORIZON_DAYS=30          # 主窗口（图表/阈值口径）
FORECAST_HORIZONS=1,7,14,30,90    # 一次 ARIMA 拟合同时输出的多时间窗
RISK_CRITICAL_MULTIPLIER=1.5      # ≥1.5× 阈值 → 🔴 高危（必须立刻重视）
RISK_ALERT_MULTIPLIER=1.0         # ≥1.0× 阈值 → 🟠 警戒
RISK_WATCH_MULTIPLIER=0.75        # ≥0.75× 阈值 → 🟡 关注，否则 🟢 正常

# —— 链接 / 入口 ——
DASHBOARD_URL=http://localhost:8501
ETHERSCAN_BASE_URL=https://api.etherscan.io/v2/api
GMAIL_API_SEND_URL=https://gmail.googleapis.com/gmail/v1/users/me/messages/send
```

> ⚠️ **常见坑**（均已实测踩过，排查过程见 [docs/development-log.md](docs/development-log.md)）：
> 1. **Google 已逐步下线「应用专用密码」**（<https://myaccount.google.com/apppasswords>），
>    因此本项目支持改用 **Gmail API + OAuth2**（走 443，比 SMTP:465 更易穿透）。
>    ⚠️ 注意：Google Cloud 里的 **API Key（`AIza...`）不能用来发信** —— 它只标识项目、
>    不代表用户身份；发信需要 **OAuth 客户端 ID + 客户端密钥**（类型：桌面应用）+
>    一次性授权换来的 **refresh token**。`python -m src.notify --oauth-login` 会帮你走完授权。
>    （`--enable-gmail-api` 更省事：授权同时把 Gmail API 一并启用并自检发信。）
> 2. `SMTP_PASS` 用账号登录密码会收到 `535 BadCredentials`，且 Gmail 在首次拒绝后立即断连。
> 3. 国内网络下 `smtp.gmail.com` 常常**TCP 能连但 TLS 握手挂死**（实测 465/587、代理/直连
>    四条路全部超时 ⇒ **SMTP 整条不可用时改用 Gmail API**）。若你的线路能过，就填
>    `NET_PROXY` / `SMTP_PROXY`（如 Clash 的 `http://127.0.0.1:7897`）走代理隧道。
> 4. `smtplib` 会在 AUTH **PLAIN 失败后自动改用 LOGIN 重试**，把真实的 `535` 掩盖成
>    「Connection unexpectedly closed」；本项目已锁死 `AUTH PLAIN` 让报错保持可读。
> 5. **OAuth 一次性授权曾卡在回调**：浏览器跳到 `http://localhost:8765/?code=...` 却
>    `ERR_CONNECTION_REFUSED`。两个真实原因已修：① macOS 上 Chrome 把 `localhost` 解析成
>    `::1`，而服务器只绑了 `127.0.0.1`；② 老实现用 `handle_request()` 只服务**一个**连接，
>    浏览器预取 `/favicon.ico` 就把回调「吃掉」了。现在回调服务器 **IPv4/IPv6 双栈监听**
>    + 循环服务 + 自动把 `GMAIL_REFRESH_TOKEN` 写回 `environment.env`。
>    兜底两条：`python -m src.notify --oauth-manual`（把地址栏整条 URL 粘回终端）、
>    `python -m src.notify --oauth-exchange "<URL或code>"`（兑换已经拿到的 code）。
> 6. **`403: Gmail API has not been used in project … or it is disabled`** —— OAuth 授权是好的，
>    只是 Gmail API 没在项目里启用。日志会直接给出 Cloud Console 启用链接；
>    想一条命令自愈就用 `python -m src.notify --enable-gmail-api`
>    （重新授权带上 `cloud-platform` → 代码调 Service Usage API 启用 → 轮询生效 → 发信自检）。
> 7. **浏览器不弹窗**：macOS 上 Python `webbrowser` 走 `osascript`（需要「自动化」授权），
>    未授权时会**静默失败**。本项目改为优先 `/usr/bin/open`（LaunchServices）→ `open -a Chrome`
>    → `webbrowser` 兜底，并打印实际命中的方式；自检：`python -m src.notify --open-test`。

手动测试推送（用**最近一次真实预测结果**，不是假数字）：

```bash
python -m src.notify                  # 发测试邮件 + Lark 简报（聚类结果 + 洞察）
python -m src.notify --only-email     # 只重试发邮件（不打扰 Lark）
python -m src.notify --open-test      # 验证「能不能自动唤起系统浏览器」
python -m src.notify --enable-gmail-api  # 授权 + 自动启用 Gmail API + 发信自检
python -m src.notify --oauth-login    # 只做一次性 Google 授权（自动写回 GMAIL_REFRESH_TOKEN）
python -m src.notify --oauth-manual   # 若浏览器连不上 localhost:8765，用这个贴回 URL
```

抓取线路也可单独指定（配了代理时先走代理，整条重试链失败后**自动回落直连**）：

```bash
FETCH_PROXY=http://127.0.0.1:7897 python -m src.data_fetcher --limit 5   # 经代理抓 5 个地址
FETCH_PROXY=direct python -m src.data_fetcher --limit 5                 # 强制直连（排障）
```


---

## 🐳 Docker 封装与推送（维护者）

```bash
# 构建镜像
docker build -t bonnie333333333/crypto-churn-prediction:latest .

# 本地冒烟测试
docker run --rm -p 8501:8501 -v "$PWD/data:/app/data:rw" \
  bonnie333333333/crypto-churn-prediction:latest

# 推送到 Docker Hub（需先 docker login）
docker push bonnie333333333/crypto-churn-prediction:latest
```

`docker-compose.yml` 关键点：Streamlit 端口 **8501**；`env_file` 读取
`environment.env`（可选，未提供也能启动）；挂载 `./data` 与 `./reports` 持久化
（**状态页 `status.html` 就靠 `./reports` 这一步带到宿主机**）；`healthcheck` 探测
`/_stcore/health`；`RUN_DELIVER=1` 决定首启/补发是否交付四去向；`CHURN_HEADLESS=1`
告诉交付层「容器内没有 GUI，别弹通知也别开浏览器」。

> 注意：邮件收件人 / Lark webhook 这类配置**只放 `environment.env`**，不要写进
> `docker-compose.yml` 的 `environment:`（会覆盖 `env_file` 注入的值）。

> **构建小贴士（Apple Silicon / linux-arm64）**：镜像基于 `python:3.11-slim`，
> 由于 arm64 上没有 `hdbscan` 预编译 wheel，Dockerfile 里预装了 `gcc g++ python3-dev cython3`
> 用于源码编译；`xgboost` 固定为 `2.1.4`（`3.x` 会在 Linux 上拉取数百 MB 的 CUDA 依赖，
> 即使只用 CPU，会让镜像膨胀）。首次 `docker build` 约需几分钟。

---

## ✨ 未来可优化点

1. **多链支持**：扩展 `chainid` 到 Polygon / Arbitrum / BSC，横向对比用户行为。
2. **图神经网络 (GNN)**：把地址-交易构图，用 GraphSAGE/GCN 学习结构特征，补充表格特征。
3. **增量特征**：每日只增量更新频次/余额类特征，避免全量重算。
4. **更好的时序模型**：用 Prophet / N-BEATS / Temporal Fusion Transformer 替代 ARIMA 提升长程预测。
5. **聚类自动命名**：用 LLM 对簇质心生成自然语言画像，替代规则打分。
6. **模型监控**：记录每次训练的 AUC 漂移与特征分布，触发再训练。
7. **成本优化**：对高频地址做请求缓存、批量 RPC，降低 Etherscan 配额消耗。
8. **实时化**：结合 Whale 项目的准实时管道，把"离线建模"与"在线打分"打通。

---
## 🧑‍💼 给业务同学的大白话解释
<details>
<summary>点击查看</summary>

把每个以太坊地址想成一位**用户**。我们去看他们过去半年在网上"做了什么、做了多少、跟谁玩"，
然后回答三个问题：

1. **他们是谁？**（聚类）——我们把用户分成几类：**高频大户**（钱多、出手频繁）、
   **长期持有者**（买进来就放着不动）、**DeFi 农民**（到处薅利息、玩各种协议）、
   **高频套利者**（快进快出赚差价），还有一类**机器人/一次性地址**（像脚本刷量，我们单独标出来）。

2. **谁要走了？**（流失预测）——如果一个地址**超过 30 天没有任何动作**，我们就认为它"流失"了。
   我们用模型提前预测"谁在接下来 30 天可能会流失"，并用 **SHAP** 解释原因，比如
   "这个地址以前玩 5 个协议，现在只剩 1 个，它流失的概率就上去了"。

3. **会走多少？**（趋势预测）——用时间序列模型预测未来 30 天大家**每天还会交易多少次**，
   预计**每天的流失比例**，帮我们提前决定要不要做活动挽回。

一句话：**这是一个"链上用户体检 + 流失预警"系统，看板就是体检报告，邮件/Lark 就是报警器。**

</details>

---

## 🔜 下一步优化

> 本节是**本次未做、明确说明**的优化方向（避免误解为已完成）。

| 方向 | 现状 | 下一步 |
| --- | --- | --- |
| **LLM 生成「初步推进建议」** | 当前为**模板规则**（`build_suggestions()`），零成本、可审计 | 把「运行状态 + 1/7/14/30/90 天预测 + 簇画像 + SHAP Top 特征」组装成结构化上下文，交给**公司内部 LLM 网关（OEM / LOOM）**或本地 Qwen/DeepSeek 生成更贴合业务的建议；保留模板结果作为 **fallback** 与对照，避免幻觉直接进业务群。已在配置里预留思路：以环境变量切换 `模板 / LLM / 两者对比` |
| **全量 12000 节点** | 本机只跑 **2000 个地址**做链路验证（免费 Etherscan Key 限流，2000 ≈ 数小时） | 接入**公司内部 Etherscan API / 自建归档节点**：`ADDRESS_LIMIT=12000`、`MONTHS_BACK=6`，同一套代码直接跑全量；再考虑按簇分层抽样以控制算力 |
| **镜像分发** | `bonnie333333333/crypto-churn-prediction:latest`（1.63 GB）**已推送成功**：`digest sha256:8f1eed0b4b6b90402d5b9412e13ef625199e1d756f6571c4274875c26b84fd8d`，且严格控制在**5 分钟硬上限**内（`timeout 300 docker push`，超时即放弃） | 受限网络下多次超时 → 现改为硬上限 + 分层续传；公司内网可改推 Harbor 更稳 |
| **多链 / 图模型 / 增量特征** | 未做 | 见上一节「未来可优化点」1–8 条 |

| 维度 | 🐋 Whale-alert-system（参考项目） | 🪙 本项目 |
| --- | --- | --- |
| 目标 | 实时发现大额 ETH 转账并预警 | 对地址做行为画像 + 流失预测 |
| 数据模式 | **实时流式**（轮询新块） | **批量离线**（近 6 个月快照） |
| 核心方法 | **规则引擎**（金额 > 阈值即巨鲸） | **无监督聚类 + 监督分类 + SHAP 可解释性** |
| 建模 | 无机器学习 | HDBSCAN + LightGBM/XGBoost/RF + SHAP + ARIMA |
| 可视化 | **Grafana**（SQLite 插件，面板 SQL） | **Streamlit + Plotly**（16 交互面板） |
| 存储 | SQLite（`whale_transfers` 等） | SQLite（`raw_transactions` / `address_features` / `address_clusters` / `churn_*` …） |
| 告警 | 终端打印 + macOS 通知 | **HTML 邮件 + Lark 交互卡片 + macOS 通知（点击直达状态页）**（带阈值/风险等级判断） |
| 调度 | 每日扫描新块 | 每日 15:30 全流程 + 数据资产快照累积 |
| 交付 | Docker Compose（checker + Grafana） | 单镜像（流水线 + 四去向交付 + Streamlit），`start.sh` 自动开看板 + 状态页 |
| 端口 | 3000 / 3001 | 8501 |

---

## 📚 文档索引

- [docs/development-log.md](docs/development-log.md) —— 技术决策思考 + 踩坑记录
- [docs/insights.md](docs/insights.md) —— 业务洞察（运行后自动生成）
- [environment.env.example](environment.env.example) —— 环境变量模板
- [README_en.md](README_en.md) —— **English README**

手动验证「四个出口」（用最近一次真实结果，不联网发送）：

```bash
python -m src.notify --preview   # 只渲染：reports/email_preview.html + reports/lark_card.json + 打印简报
python -m src.notify             # 真发送：邮件 + Lark 卡片（需要 OAuth / webhook）
python -m src.status_page        # 生成 reports/status.html 运行状态页并打印路径
python -m src.deliver --no-send --no-open   # 四去向一起走一遍，但不真的发（自检，推荐）
python -m src.deliver --simulate-failure "模拟：Etherscan 429"   # 验证「失败也要交付」的状态页
python -m src.notify --oauth-login   # 一次性 Google 授权，打印 GMAIL_REFRESH_TOKEN
```

容器/compose 侧同一条链路的自检（不重建镜像，把新代码挂进去跑 entrypoint）：

```bash
# 触发「复用已有结果补发四去向」分支（等价于 docker compose 首启跑完后的补发）
docker compose exec churn-app python scripts/daily_run.py --deliver-only --no-open
# 只看四去向是否都能生成、不发网络请求
docker compose exec churn-app python -m src.deliver --no-send --no-open
```

---

## 🇬🇧 English

📖 **Full English README → [README_en.md](README_en.md)**

**Batch off-chain analytics for Ethereum users**: fetch ~6 months of normal / internal /
ERC-20 transactions for 200 (test) or **2000 (local feasibility run)** addresses from
Etherscan V2, engineer six behavioural feature families, cluster with **HDBSCAN**
(personas: High-frequency Whale, Long-term Holder, DeFi Farmer, High-frequency
Arbitrageur, plus Noise/bots), predict churn (`last_tx_days_ago > 30`) with
**LightGBM / XGBoost / RandomForest** (compared), explain it with **SHAP**, and forecast
each address's transaction frequency for **1 / 7 / 14 / 30 / 90 days with a single ARIMA
fit**. A **Streamlit** dashboard (16 panels) visualises everything on port **8501**; a
daily 15:30 run accumulates a data asset and pushes **HTML e-mail + a Lark interactive
card + a clickable macOS notification** — all four outputs carry the same payload:
**run status & errors · risk level · multi-horizon churn rates · cluster personas ·
insights · suggested next actions**, and everything lands on the local
`reports/status.html` run-status page.

> ⚠️ **Sample size**: the real 24h-active node set is ~**12000** addresses, which is not
> runnable locally (free Etherscan key ≈ 5 req/s). This project therefore runs **2000
> addresses as a link-validation run**; point `ADDRESS_LIMIT` at `12000` once an internal
> Etherscan API / archive node is available — the code path is identical.

**Run it in three ways**: `docker pull` the image · `./start.sh` (one-command compose up +
auto-opens the browser) · or run `data_fetcher → feature_engineer → cluster_analyzer →
churn_model → streamlit run app/dashboard.py` manually in the
`crypto_churn_prediction_project` conda env (Python 3.11).

Key design note: **HDBSCAN over K-Means** because on-chain behaviour is variable-density and
contains outliers (bots / one-off addresses) that K-Means would force into arbitrary clusters.

