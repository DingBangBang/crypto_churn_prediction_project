# 开发日志 (Development Log)

本文记录本项目的**关键技术决策思考**与**踩坑记录**，与 `README.md` 互补。

---

## 1. 总体架构决策

参考 `whale-alert-system`（实时流式 + 规则引擎 + Grafana）的成功经验，本项目定位为
**批量离线建模**：一次性拉取近 6 个月数据 → 特征工程 → 无监督聚类 → 监督分类 +
可解释性 + 时间序列预测 → Streamlit 看板。

- **为什么离线批量**：流失预测本质是"回顾历史 + 预测趋势"，需要稳定、可复现的
  快照数据集，而不是每秒钟变化的实时流。离线批处理也让建模、调参、可解释性分析
  更容易复现。
- **为什么统一 SQLite**：与 Whale 项目保持一致，零外部依赖、单文件、方便挂载进容器
  与备份。所有阶段读写同一张 `crypto_churn[_test].db`，阶段间解耦、可单独重跑。

## 2. 数据抓取：Etherscan V2 + 限流 + 分页

- **必须用 V2**：Etherscan V1 已废弃。V2 端点 `https://api.etherscan.io/v2/api`，
  需要 `chainid=1`（主网）。
- **三个端点**：`account/txlist`（普通交易）、`account/txlistinternal`（内部交易）、
  `account/tokentx`（ERC-20 转账）。
- **分页策略**：免费版 `offset` 最多 10000。为了让 6 个月历史尽可能"一页装下"，
  把 `PAGE_SIZE` 默认设为 **10000**、`MAX_PAGES=2`。这样每个地址通常只需 1~3 次请求，
  大幅降低请求总量（200 地址 ≈ 600 次请求；2000 地址 ≈ 6000 次）。
- **地址扩展（BFS）**：与其手写 2000 个地址，不如从 10 个"种子大户/交易所"出发，
  抓取其交易对手方（counterparty）并加入队列，直到凑够 `ADDRESS_LIMIT`。这样数据集
  天然覆盖"大户 + 与其交互的长尾地址"，多样性更好。
- **限流**：免费版约 5 req/s、10 万/天。每次请求前 `sleep(BASE_RATE_LIMIT_DELAY)`，
  `_get()` 带指数退避重试；命中 `rate limit` 字样时退避后重试。
- **测试优先**：先 `ADDRESS_LIMIT=200` 跑通全流程（产物带 `_test` 后缀），再扩到 2000。

### 踩坑
1. **速率限制会突然出现**：即使 0.22s 间隔，连续抓取大户仍会触发 429/"rate limit"。
   指数退避 + 重试是必需的，否则会丢数据。
2. **"No transactions found" 不是错误**：`status=0` 且 `result` 是字符串时是空结果，
   要当作空列表处理，不能抛异常。
3. **ERC-20 的 `value` 是 token 原始整数**：要除以 `10^tokenDecimal` 才能还原数量；
   合约地址需小写并据此推断代币符号（内置常用稳定币/主流币映射）。
4. **`address` 维度存储**：每行记录 `address = 被观测地址`，同一笔转账可能因双方都被
   观测而存两次——这是**有意为之**，便于按地址聚合出完整行为。

## 3. 特征工程：六大类特征

| 类别 | 字段 | 计算思路 |
| --- | --- | --- |
| 交易频次 | `tx_freq_daily/weekly/monthly` | 总笔数 ÷ 活跃天数（天），再 ×7/×30 |
| 平均持仓时长 | `avg_holding_hours` | ERC-20 按合约做 **FIFO** 配对：收到时间 → 转出时间之差 |
| Gas 消耗模式 | `high_gas_ratio`, `avg_gas_price_gwei` | 高于**数据集 75 分位**的 Gas 价占比（动态基准，避免硬编码 50 gwei 不适配测试集） |
| 协议多样性 | `protocol_diversity` | 交互过的不同合约数（ERC-20 合约 + 零值调用目标） |
| 代币行为 | `unique_tokens`, `token_tx_ratio` | 不同代币数 / ERC-20 笔数占比 |
| 资产规模变化 | `eth_balance_start/end/trend` | 累计净 ETH 流向 + 对时间的**线性回归斜率**（ETH/天） |

### 决策
- **"now" 用数据集最新时间戳**而非 `time.time()`：离线建模时用真实 now 会让所有
  数据集都"过期"（全部判为流失）。用数据集内最大时间戳对齐，标签分布才有意义。
- **持仓时长用 FIFO**：真实持仓常是分批买、分批卖，FIFO 配对比"首末差分"更贴近真实
  持有成本。

## 4. 聚类：为什么是 HDBSCAN 而不是 K-Means

- 链上行为数据**密度极不均匀**：少数超级大户/交易所有海量交易，长尾地址几乎静止。
- K-Means 假设簇是**球形、等方差**且必须**每个点都归簇**，会把"机器人/一次性地址"
  硬塞进某个簇，结果不紧凑、可解释性差。
- **HDBSCAN** 能：① 处理变密度；② 自动确定簇数（无需手选 K）；③ 输出 `-1` **噪音点**
  ——正好对应"可能是机器人或一次性地址"，这在业务上是有价值的一类。
- **打标签**：对每个簇的 z-score 质心按规则打分，贪心匹配到 4 个 persona
  （高频大户/长期持有者/DeFi农民/高频套利者），噪音簇单独标为"噪音/机器人"。

### 踩坑
- **小样本下 HDBSCAN 会把所有点判为噪音**（`min_cluster_size` 过大）。解决：动态下调
  `min_cluster_size = min(配置值, n//4)`、`min_samples` 同步收缩。
- **t-SNE 的 perplexity 必须 < 样本数**，否则报错。解决：`perplexity = min(30, (n-1)//3)`，
  并在 `n<5` 时返回零坐标。

## 5. 流失预测：标签、三模型对比、SHAP、ARIMA

- **标签定义**：`last_tx_days_ago > 30` → 流失（1）。简单、可解释、可复现。
- **三模型对比**：LightGBM（主）、XGBoost、RandomForest。链上特征强非线性、交互多，
  梯度提升树通常 AUC 最高且对缺失/共线性稳健；RandomForest 更稳但 AUC 略低。
- **SHAP**：树模型用 `TreeExplainer`，输出全局 `mean(|SHAP|)` 排序与**单样本**瀑布式
  归因（如"协议多样性从 5 降到 1 → 流失概率上升"）。
- **频次 × 流失率**：把地址按 `tx_freq_daily` 分桶，统计每桶真实流失率，找出流失率
  **陡增的危险区间**。
- **ARIMA 时间序列预测**：对每个地址构建**日交易笔数**序列，拟合 `ARIMA(1,1,1)`，
  预测未来 30 天，得到人均频次下降曲线与逐日流失率。

### 踩坑
1. **ARIMA 拟合慢/易失败**：2000 地址逐个拟合代价高。解决：序列 <10 或全零时直接用
   近 14 天均值"朴素预测"；`try/except` 包裹，个别失败不影响整体。
2. **ARIMA 外推爆炸**：交易笔数序列极度稀疏+尖峰（交易所地址日峰值上千），直接对原始
   计数拟合 `ARIMA(1,1,1)` 会**指数级外推**（实测人均频次被预测到 242、第 30 天 401）。
   解决：先对 `log1p(series)` 做**方差稳定变换**再拟合，`expm1` 反变换，并把预测**截断到
   该地址历史峰值**。修复后人均 34 → 预测 19（合理下降）。
3. **单类标签**：若测试集全是一种标签，`roc_auc_score` 会报错。解决：捕获 `ValueError`
   返回 `nan`；样本过少或标签单一直接跳过训练。
4. **SHAP 版本差异**：新版 `shap_values` 对二分类可能返回 3 维
   `(n, features, classes)`，需要取 `[:, :, 1]`。
5. **重打分要用预测频次**：把 `tx_freq_daily/weekly/monthly` 替换为 ARIMA 预测值后
   再喂给分类器，才能得到"未来流失概率"，而不是复述当前状态。
6. **arm64 Docker 编译 hdbscan**：`python:3.11-slim` 在 Apple Silicon（linux/arm64）上
   没有 hdbscan 预编译 wheel，会回退到源码编译，需在镜像里装 `gcc g++ python3-dev cython3`。

## 6. 可视化与告警

- **16 个 Panel**：指标卡、簇分布饼图、雷达图、t-SNE、模型指标表、三模型对比、簇×流失率、
  流失概率分布、SHAP 全局、SHAP 单样本、频次×流失率、30 天预测双线、高危 TOP、累积快照、
  运行健康、业务洞察。
- **告警**：HTML 邮件模板（`templates/alert_email.html` 预留 `{{占位符}}`）+ Lark webhook
  + macOS 通知。阈值来自 `environment.env`（流失率 ≥ 40% / 高危占比 ≥ 25%）。
- **调度**：`scripts/daily_run.py` 每日 15:30（Cline Schedule / cron），成功与失败都有
  macOS 通知 + HTML 报告，并把当日结果写入 `daily_snapshots`（数据资产累积）。

## 7. Docker 一键化

- 单镜像同时**跑流水线 + 托管 Streamlit**：`docker-entrypoint.sh` 首启跑
  `run_pipeline.py`（用 marker 保证只跑一次），随后 `exec streamlit`。
- `./start.sh` = `docker compose up -d --build` + 轮询健康检查 + 自动 `open` 浏览器，
  满足"一条命令跑完直接看结果"。
- LightGBM 运行需 `libgomp1`，Dockerfile 中显式安装。

## 8. 踩坑：预警邮件"发了但发不出去"（国内网络 + Gmail）

现象：`python -m src.notify` 只报 `邮件发送失败: Connection unexpectedly closed`，
既看不出是网络问题还是密码问题。

排查路径（可复现）：

1. `nc -vz smtp.gmail.com 465` → **success**；但 `openssl s_client -connect smtp.gmail.com:465`
   直接卡死、无任何响应。说明 TCP SYN 是本地代理/网关代答的，**TLS 握手才是被阻断的那一环**
   —— 别信 `nc` 的"端口通"。
2. 加代理再握手：`openssl s_client -proxy 127.0.0.1:7897 -connect smtp.gmail.com:465` →
   立刻成功（`CN=smtp.gmail.com` 证书链校验通过）。**结论：SMTP 必须走代理隧道**。
3. 于是给 `src/notify.py` 加了 `_proxy_tunnel()`：纯标准库发 `CONNECT` 建隧道，再用 `ssl`
   把裸 socket 包一层，塞进 `smtplib.SMTP_SSL` 的 `.sock` / `.file`。通道立刻打通：
   `220 问候` → `EHLO 250` → `TLS_AES_256_GCM_SHA384`，全程 < 1 秒。
4. 通道通了但认证仍失败，日志又是一句 `Connection unexpectedly closed`。用 `set_debuglevel(1)`
   看原始协议才发现真相：smtplib 先用 `AUTH PLAIN`，被 Gmail 回 **535 BadCredentials**，
   它**自动换 `AUTH LOGIN` 重试**，Gmail 随即掐断连接 —— 真正可读的 535 被替换成了断连异常。
   修法：`login()` 之前把 `server.esmtp_features["auth"]` 锁成 `PLAIN`（只试一种机制），
   让报错保持为清晰的 535；同时单独捕获 `SMTPAuthenticationError` 打印可操作提示。

**两条教训**
- `nc -vz` 通 ≠ 端口可用：有本地代理/透明网关时 SYN 由代理代答，必须做一次**真实 TLS 握手**才算数。
- 第三方库的"友好重试"会吃掉真正的错误信息；排查这类协议问题，`set_debuglevel(1)`
  看原始交互比读异常消息快得多。

配置要点（历史方案）：`SMTP_PASS` 需为 16 位**应用专用密码**（不是账号登录密码）；
`SMTP_PROXY=http://127.0.0.1:7897`（Clash 混合端口）用于穿透出站阻断。
> 后续 Google 逐步下线了应用专用密码，现改用 OAuth2，详见第 10 节。

## 9. 从「只会报警」到「会讲故事」：Lark 每日简报（聚类结果 + 洞察）

- 需求：机器人不能只丢一句「流失率 51.5%」，还要用**文字**讲清用户被聚成了哪几类、每类什么画像、
  以及从模型里提炼出的 insights。
- 做法：`collect_cluster_digest()` 直接从 SQLite 聚合（`address_clusters` ⋈ `address_features`
  ⋈ `churn_predictions`）→ `render_cluster_text()` 把每簇渲染成「规模 / 实际流失率 / 日均笔数 /
  持仓 / 协议数 / 闲置天数 + 一句业务解读」→ `read_insights()` 从 `docs/insights.md` 中挑出
  SHAP 关键特征、ARIMA 预测、结论三段，并把 markdown 转成纯文本（Lark 的 `text` 消息不渲染 markdown）。
- 决策：**Lark 简报每天例行推送、不受阈值门控**（`send_lark_digest()`），阈值只决定预警段是否标 ⚠️。
  否则哪天指标回落，群机器人就彻底哑了，业务侧失去连续观测；邮件仍按阈值触发，避免噪音。
- 踩坑：原实现把手机号塞进 `<at user_id="133...">` —— Lark 的 @ 只认真实 `open_id`（`ou_` 开头），
  填手机号会导致整条消息被拒。现在只在 `ou_`/`on_`/`all` 开头时才加 @。

## 10. 邮件方案变更：Google 下线「应用专用密码」→ 改用 Gmail API + OAuth2

- 事实澄清：Google Cloud 的 **API Key（`AIza...`）无法用于发信**。API Key 只标识「哪个项目」，
  而发信是以「某个用户」的身份进行，必须用 **OAuth 客户端 ID + 客户端密钥**（类型：桌面应用）
  换来的用户授权（refresh token）。这是两套完全不同的凭证，别混用。
- 实现：`send_email()` 变成双通道 —— 若已配置 `GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN`，
  就走 Gmail REST API（`gmail.googleapis.com`，443 端口，顺带绕开 SMTP:465 的 TLS 阻断）；
  否则回落到原 SMTP 路径。
- 一次性授权：`python -m src.notify --oauth-login` 会起一个本地回环 HTTP 服务接住 Google 回调，
  自动换取并打印 `GMAIL_REFRESH_TOKEN`，省去手动复制授权码。
- 出站代理统一为 `NET_PROXY`（未设置时继承 `SMTP_PROXY`）；`_post_json()` 采用「代理优先、直连兜底」，
  并在日志中标注实际走的是哪条路（本次实测 Lark 走代理推送成功：`Lark 推送已发送（经代理）`）。

## 11. Bug 修复：`docs/insights.md` 每运行一次就翻倍

- 现象：看板第 16 面板的洞察正文出现两遍，且两份数字互相矛盾（ARIMA 一份 19.27、一份 401.74）。
- 根因：`churn_model.run()` 写文件时用的是
  `header + text + existing.split("---", 1)[-1]`，本意是「保留下方人工补充段落」，但 `split`
  在**没有分隔符**时会返回原文本身 → 上一次的整篇正文被追加到本次正文之后，每跑一次长一倍。
- 修法：整篇重写（该文件本就声明由脚本自动生成），并在 `read_insights()` 侧做防御性去重
  （同名标题只取第一份）。
- 结果：`insights.md` 从 89 行（重复两遍）回到稳定 42 行，新增单测 `test_read_insights_dedupes_and_strips_noise` 守住。

## 12. 从「一句预警」到「一页可交付」：运行状态页 + 四出口同源

- 需求：macOS 通知点开之后必须能看到**①运行状态（每个阶段成功/失败）+ 报错内容
  ②三个方向（邮件 / Lark / 看板）的入口与结果 ③结果内容（是否高危、多时间窗流失率、
  聚类、洞察、初步推进建议）**。
- 设计：新增 `src/status_page.py`，把 `src/notify.py` 里已经算好的口径（`collect_run_status()` /
  `risk_level()` / `horizon_rows()` / `build_suggestions()` / `render_run_status_html()`）
  直接渲染成一张自包含的本地 HTML（`reports/status.html`），**不依赖任何服务**，双击即可看。
- 四出口同源：邮件 HTML、Lark 卡片、macOS 通知、状态页全部由同一批函数产出，避免「邮件说 51%、
  卡片说 47%」这类口径漂移。
- 运行状态的双来源：优先读 SQLite `pipeline_runs`（每个阶段 `db.record_run()` 写入，
  含 detail 与时间），再叠加 `reports/last_run.json`（`daily_run` 结束/异常时写入，
  含 ok / elapsed_s / error 原文），两者互补：前者回答「哪一步失败」，后者回答「整轮成败与耗时」。
- 决策：`daily_run` 结束时**自动在浏览器打开状态页**，并把它作为纯文本通知的 `-open` 目标 ——
  即使通知权限没开，信息也不会丢。

## 13. Lark 只能发纯文本吗？—— 不是，用消息卡片（interactive + lark_md）

- 结论：群机器人 webhook 支持三种形态：`text`（纯文本，不渲染 markdown）、
  `post`（富文本，可加粗但**没有彩色标题栏**）、`interactive`（消息卡片，内部文本走
  `lark_md` 方言，可真加粗 / 彩色 header / 分割线 / 超链接 / @）。
- 做法：`build_digest_card()` 生成卡片 JSON（`header.template` 随状态与风险变色：
  运行失败或 🔴 高危 = `red`，🟠 = `orange`，🟡 = `yellow`，🟢 = `green`），
  卡片元素用 `hr` 分段；同时保留 `build_digest_text()` 生成的纯文本作为**降级兜底**
  （`send_lark_digest()` 先试卡片，失败即回退文本）。
- 踩坑：`lark_md` 里 `<font color='red'>` 只支持 `red/green/grey` 等有限色值，别乱写十六进制。
- 自检：新增 `python -m src.notify --preview` —— **只渲染、不发送**，把
  `reports/email_preview.html` 与 `reports/lark_card.json` 落盘，方便离线核对（不消耗配额、不需要代理）。

## 14. macOS 通知「点击直达」：terminal-notifier 的权限坑与 file:// 要求

- 安装：`brew install terminal-notifier`；它支持 `-open <url>`，点按通知即可打开目标页面。
- 踩坑 1（**file:// 必需**）：`-open` 只接受合法 URL，直接传本地路径会报
  `'...' is not a valid URL. It needs a scheme, such as https://… or file:///tmp`。
  代码里已统一把本地路径规范化为 `file://<绝对路径>`。
- 踩坑 2（**通知权限**）：Homebrew 公式里的裸 binary 会一直报
  `Could not request notification permission: Notifications are not allowed for this application`，
  且**不会**在「系统设置 → 通知」里出现。改用 app bundle 内的可执行文件
  （`terminal-notifier.app/Contents/MacOS/terminal-notifier`）后 macOS 才会弹权限请求；
  本项目额外把 `~/Applications/Terminal Notifier.app` 软链过去，便于在设置里找到并授权。
- 兜底：任何失败（未授权 / 未安装 / 超时）都会自动回退到 `osascript display notification`
  普通横幅，并打 WARNING 说明原因 —— 通知功能降级但绝不静默失败。
- 踩坑 3（**必须在设置里能被看到**）：Homebrew 装的 app bundle 直接跑只会得到
  `Notifications are not allowed for this application`，且**不会出现在「系统设置 → 通知」列表**里；
  把 bundle **真实拷贝**到 `~/Applications/Terminal Notifier.app`（不要软链）并用
  `lsregister -f` 向 LaunchServices 注册后，系统才认得它并给出可操作的提示
  （`Notifications are turned off for this application … tccutil reset UserNotification
  fr.julienxx.oss.terminal-notifier`）。开启路径：
  `open "x-apple.systempreferences:com.apple.Notifications-Settings.extension"`。
  代码 `_terminal_notifier()` 的候选顺序也据此调整为「~/Applications bundle → /Applications
  bundle → Homebrew keg bundle → 裸 binary」。

## 15. 多时间窗预测：一次 ARIMA 拟合服务 1/7/14/30/90 天

- 原实现：对每个窗口分别拟合一次（30 天就拟合一次预测 30 天）→ 想做 5 个窗口要拟合 5 次，
  在 2000 个地址上就是 5 倍算力。
- 现实现：**只拟合到最大窗口**（`FORECAST_MAX_DAYS=90`），一次性拿到逐日预测序列，
  再按窗口切片聚合出「平均日频次 / 频次降低值 / 平均流失率 / 末日流失率」。
- 额外收益：能给出**短窗 vs 长窗结论** —— 短窗流失率 ≥ 长窗 ⇒「加速出逃，立刻干预」；
  否则 ⇒「渐进式失活，仍有挽回窗口」。风险等级也由预测流失率 / 阈值倍数分档
  （1.5× 高危 / 1.0× 警戒 / 0.75× 关注），保证「是否必须重视」有统一口径。

## 16. 样本量与限流实测（为什么是 2000 而不是 12000）

- 实测：免费 Etherscan Key 下，2000 个地址的抓取在 `rate-limited` 退避中约 **1900 秒才走完 190 个
  地址**（每个地址约 4 个请求：balance / txlist / txlistinternal / tokentx），全量 12000 节点
  在本机不可行。
- 因此本项目把 2000 定为**链路可行性验证规模**：证明「抓取 → 特征 → 聚类 → 三模型 → 多窗口 ARIMA →
  四出口推送」端到端能跑通；全量留给公司内部 Etherscan API / 归档节点，代码路径完全相同。

## 17. Docker Hub 推送：5 分钟硬上限 + 分层续传（本次成功）

- 背景：受限网络下 `docker push` 多次超时（1.63 GB 镜像，多阶段分层）。
- 本次做法：`timeout 300 docker push …`（硬上限 5 分钟，超时即放弃，不在网络上赌时间），
  失败后重试时 Docker 会**复用已上传的层**（日志里出现大量 `Layer already exists`），于是本次在
  上限内完成：`latest: digest sha256:8f1eed0b4b6b90402d5b9412e13ef625199e1d756f6571c4274875c26b84fd8d`。
- 结论：**分层 + 续传 + 硬上限**是弱网下最实用的组合；公司内网直接推 Harbor 更省事。

## 18. 待办 / 未来可优化点


见 README 的「未来可优化点」与「下一步优化」章节（LLM 生成建议、全量 12000 节点、
多链支持、图神经网络、增量特征、模型监控等）。
