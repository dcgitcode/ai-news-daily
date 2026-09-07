# 0 元 AI 新闻日报 V1

## 当前默认：中文日报只发 163 邮箱

本地示例已设为 `CHANNELS=email`，云端 workflow 固定只启用邮件，直接连接 `smtp.163.com:465`（SSL），无需 pushplus。旧的 `CHANNELS` 仓库变量不再控制云端渠道；下方微信介绍仅作为保留代码的参考。

1. 登录 163 网页邮箱，在设置中找到 POP3/SMTP/IMAP，开启 SMTP 服务并按页面验证取得客户端授权码。
2. GitHub Actions Secrets 填入 `SMTP_USER`（完整 163 地址）、`SMTP_PASSWORD`（客户端授权码，非登录密码）、`EMAIL_TO`（收件邮箱）。给自己发就让 SMTP_USER 和 EMAIL_TO 相同。
3. `SMTP_HOST`、`SMTP_PORT`、`SMTP_SECURITY` 可省略，云端默认 smtp.163.com、465、ssl；`EMAIL_FROM` 默认等于 SMTP_USER。如果此前已配过其他邮箱，请删除或改正旧 SMTP Secrets。
4. 手动运行 workflow，取消 dry_run 进行首次实发。授权码只填本地 .env 或 GitHub Secrets，不要发到聊天里。

微信 ClawBot 可通过腾讯插件直接接入，但需扫码绑定、维护会话上下文；维护者仓库有无交互时定时发送因令牌过期而失败的反馈，暂不作为本项目无人值守日报的默认渠道。pushplus 的免费额度也有实名认证条件，不能笼统保证零门槛免费。

依据：[pushplus 额度表](https://pushplus.plus/doc/guide/use.html)、[腾讯微信插件](https://github.com/Tencent/openclaw-weixin)、[定时发送问题反馈](https://github.com/Tencent/openclaw-weixin/issues/225)。尚未配置个人凭据或真实发送。

独立于 Codex 的 Python 项目：抓取 RSS、arXiv、GitHub，按关键词与规则选出值得阅读的内容，生成 HTML/纯文本日报，通过 SMTP 邮件和 pushplus 微信公众号推送。**没有大模型调用、API 付费入口或自动翻译**。默认只收录中文标题和中文摘要，摘要直接摘录中文来源，不依赖翻译服务。

## 项目结构

```text
ai-news-daily/
├── news_digest/
│   ├── __main__.py       # 命令行与处理流程
│   ├── core.py           # 清洗、过滤、评分、去重、HTML 与状态
│   ├── sources.py        # RSS / arXiv / GitHub
│   └── delivery.py       # SMTP / pushplus
├── cloudstudio_checkin.py # Cloud Studio 每日签到
├── notify_failure.py      # Actions 失败时的 SMTP 告警
├── cloudflare-scheduler/  # 外部定时调度器（Workers Cron → repository_dispatch）
├── tests/test_digest.py
├── .github/workflows/daily.yml
├── .github/workflows/cloudstudio-checkin.yml
├── .env.example
├── .gitignore
├── config.json
├── requirements.txt
└── README.md
```

运行后生成 `reports/latest.html`、`reports/latest.txt`、`reports/status.json`；真实推送受理后生成 `state/delivery.json`。

## 1. 本地运行

使用 Python 3.10+，推荐 3.12。在项目根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m news_digest --demo
```

macOS/Linux 激活和复制命令改为：

```bash
source .venv/bin/activate
cp .env.example .env
```

三种运行方式：

```bash
# 离线演示，无网络、无需密钥、不推送、不更新去重记录
python -m news_digest --demo
# 真实抓取，但不推送、不更新去重记录
python -m news_digest --dry-run
# 按 .env 指定的渠道推送
python -m news_digest
# 核心行为与失败重试测试
python -m unittest discover -s tests -v
```

请从项目根目录运行。系统环境变量优先于 `.env`。`--dry-run` 的 HTML 展示当次候选内容，真实推送还会按渠道排除已发送条目，因此数量可能不同。不要同时运行多个本地推送进程。

## 2. 配置邮件与微信

`.env` 中 `CHANNELS=email` 表示只发邮件，`CHANNELS=pushplus` 表示只发微信，`CHANNELS=email,pushplus` 表示两者都发。

| 环境变量 / Secret | 用途 |
| --- | --- |
| SMTP_HOST | SMTP 服务器，例如 smtp.qq.com；按邮箱服务商设置 |
| SMTP_PORT | 默认 465；STARTTLS 通常使用 587 |
| SMTP_SECURITY | ssl（默认）或 starttls；始终启用 TLS 证书校验 |
| SMTP_USER | SMTP 登录账户 |
| SMTP_PASSWORD | 邮箱 SMTP 授权码或应用密码，按服务商要求 |
| EMAIL_FROM | 发件地址，留空使用 SMTP_USER |
| EMAIL_TO | 收件地址；多个用英文逗号隔开 |
| PUSHPLUS_TOKEN | pushplus 用户或消息 token |
| GITHUB_TOKEN | 本地可选，提高 GitHub 搜索限额；Actions 自动提供 |

邮件：先在邮箱设置中开启 SMTP，取得授权码，填写 `.env`。微信：在 [pushplus](https://www.pushplus.plus/) 注册并按平台指引绑定微信公众号、获取 token；填写 `PUSHPLUS_TOKEN`。本项目只请求 `wechat` 免费渠道，不请求短信、语音等收费渠道。实际免费额度、实名认证、关注或交互要求以账户页面为准。

pushplus 的 HTTP 200 与业务 `code=200` 只代表**平台已接收并排队**，不代表微信实际送达；请结合平台记录与 `reports/status.json` 的流水号排查。SMTP 接收也不代表一定进入收件箱。首次启用建议先只填自己的一个收件地址，检查垃圾邮件箱。

## 3. 部署与云端定时

### 3.1 为什么不用 GitHub 自带的 schedule

初版用 workflow 里的 `schedule` 定时，实际不可靠：

| 问题 | 后果 |
| --- | --- |
| 公共仓库连续 60 天无活动，`schedule` 被自动停用 | 跑着跑着就停了，且不报错 |
| 高负载时延迟，整点/半点最严重，队列任务可能被丢弃 | 时间飘、甚至当天不跑 |
| 原 Cloud Studio 任务在 runner 上 `sleep` 43–282 分钟做随机延迟 | 长时间空转占用，违反使用限制，易中途被回收 |

现在两个 workflow **只保留 `repository_dispatch` 和 `workflow_dispatch`**，定时交给外部调度器。没有 `schedule`，就不会被 60 天规则停用。

### 3.2 部署步骤

1. 新建 GitHub 仓库，将本目录的内容放在仓库根目录，**包括隐藏的 `.github` 目录**。不要上传 `.env`、`.venv`、`reports`、`state`。
2. 在 `Settings → Secrets and variables → Actions → Secrets` 中添加所需 Secrets。仅配置启用渠道需要的项；无需自己创建 `GITHUB_TOKEN`。
3. 当前 workflow 固定 `CHANNELS: email`，无需新增渠道变量，也无需 PUSHPLUS_TOKEN。
4. 确保仓库允许 Actions 写入内容。workflow 声明了 `contents: write`，用于保存去重记录；组织政策或分支规则若禁止创建/更新 `digest-state`，需要调整规则。
5. 在默认分支的 `Actions → AI news daily → Run workflow` 保持 `dry_run=true`，运行真实抓取预览，下载 `ai-news-report-...` artifact 查看 HTML 与来源状态。
6. 填好凭据后再次手动运行，关闭 `dry_run` 才会真实推送。外部调度器触发时固定 `dry_run=false`。
7. 在 GitHub `Settings → Notifications` 中确认开启 Actions 失败邮件。workflow 内含 `Alert on failure` 步骤，会用同一套 SMTP 再发一封告警。**Cloud Studio 签到失败最常见的原因是 Cookie 过期**，告警邮件会直接提示你更新 `CLOUDSTUDIO_COOKIE`。

### 3.3 选择外部调度器

三者打的是同一个接口，可随时替换：

```http
POST https://api.github.com/repos/dcgitcode/ai-news-daily/dispatches
Authorization: Bearer <你的 PAT>
Content-Type: application/json

{"event_type": "daily-digest", "client_payload": {"dry_run": false}}
```

Cloud Studio 签到把 `event_type` 换成 `cloudstudio-checkin`，不带 `client_payload`。

| 方案 | 费用 | 精度 | 适合场景 |
| --- | --- | --- | --- |
| Cloudflare Workers Cron | 免费额度足够 | 分钟级，最准 | 推荐。仓库已带 `cloudflare-scheduler/`，部署一次即可 |
| cron-job.org | 免费 | 分钟级 | 不想碰命令行。网页填 URL + Header + Body |
| 腾讯云函数 SCF 定时触发器 | 免费额度足够 | 分钟级 | 国内网络，还能直接跑签到脚本，不经过 GitHub |
| 本机 Windows 计划任务 | 0 | 依赖机器开机 | 兜底。一条 `curl` 即可 |

PAT 需要 `repo` 权限（经典 PAT）或 `Contents`/`Actions` 写权限（细粒度 PAT）；公共仓库用 `public_repo` 也够。

### 3.4 部署 Cloudflare 调度器

```bash
cd cloudflare-scheduler
npm install -g wrangler
wrangler login
wrangler secret put GITHUB_DISPATCH_TOKEN   # 粘贴上面的 PAT
wrangler secret put TRIGGER_TOKEN           # 自定义一串随机字符串，用于手动触发鉴权
wrangler deploy
```

部署后访问 `https://ai-news-scheduler.<你的子域>.workers.dev/health` 应返回 JSON。手动补跑：

```bash
curl -X POST -H "X-Trigger-Token: <TRIGGER_TOKEN>" \
  https://ai-news-scheduler.<你的子域>.workers.dev/run/daily-digest
```

cron 时间写在 `wrangler.jsonc` 的 `triggers.crons`，**UTC 时间**，当前为 UTC 00:17（北京 08:17 日报）和 UTC 23:17（北京 07:17 签到）。改时间只需改这两行和 `src/index.js` 里的 `CRON_TARGETS` 映射。

### 3.5 验证链路是否打通

仓库内置 `selftest` 工作流，被触发后会打印触发来源与北京时间（用于核对 cron 换算）、检查 Secrets 是否配置、校验 Cloud Studio Cookie 结构，并发一封主题为「[ai-news-daily] 调度自检通过」的邮件。任一步失败即标红。

**方式一：先用网页手动触发**（验证工作流本身）

`Actions → Scheduler self test → Run workflow`。收到邮件说明 Secrets 与 SMTP 正常。这一步不通，先别配调度器。

**方式二：外部调度器触发**（验证真正的自动执行）

这条链路不经过 GitHub 自带的 `schedule`，所以不会被 60 天无活动停用，也不会在负载高峰被丢弃。实测从发出请求到开始执行约 1 秒。

想长期无人值守地自动触发，最快是 cron-job.org，不用装任何东西：

| 字段 | 值 |
| --- | --- |
| URL | `https://api.github.com/repos/dcgitcode/ai-news-daily/dispatches` |
| Method | POST |
| Header | `Authorization: Bearer <PAT>`、`Accept: application/vnd.github+json`、`Content-Type: application/json` |
| Body | `{"event_type": "selftest"}` |
| 周期 | 自选，`*/30 * * * *` 为每 30 分钟 |

建三条 job，body 的 `event_type` 依次为 `selftest`、`daily-digest`、`cloudstudio-checkin`，时间建议错开整点。

**验证通过后务必清理**：删除 `.github/workflows/selftest.yml`。保留它不会自动触发（没有绑定 cron），但若配了外部定时任务忘了删，它会按周期持续给你发邮件。

### 跨天去重如何保存

不依赖可能被清理的 Actions cache。workflow 在单独的 `digest-state` 分支保存 URL/规范化标题的 SHA-256 摘要和受理时间，两个渠道独立记录；推送失败不标记该渠道。某渠道成功后另一渠道失败，仍执行状态提交，任务最终标红提示失败。下一次运行会重试仍在抓取窗口内且尚未成功的条目。

记录保留 90 天。不要删除 `digest-state` 分支，否则下一次会重新推送抓取窗口内的内容。公开仓库的状态分支不存地址或密钥，但会公开哈希和时间；报告 artifact 只包含内容与回执，使用仓库正常权限访问，不部署公开网页。

外部推送与 Git 提交无法成为同一个事务：如果推送已受理但机器崩溃、状态提交失败或响应超时，重试可能重复。SMTP 多收件人发生部分拒收也可能让已接受的收件人收到重发。V1 不承诺 exactly-once，也不自动重试推送 POST。请检查失败任务，避免盲目重复运行。首次配置缺失会在抓取前失败；错误日志隐藏响应正文与密钥。

## 4. 新闻规则与来源

编辑 `config.json`：

- 默认最近 **48 小时**，覆盖调度延迟和部分源更新时间；改 `lookback_hours` 可用 24 小时，允许 1–720 小时。
- `keywords` 为关键词权重，命中至少一个才入选；ASCII 词有边界，避免 AI 匹配到 paid。
- `exclude_keywords` 命中即排除，适合过滤广告等。
- 得分 = 来源权重 + 命中关键词权重总和（最多 12）+ 新鲜度（24 小时内 +2，否则 +1）+ GitHub 星数奖励（每 1000 星 +1，最多 +3）。不是新闻事实可靠性评分。
- URL 去跟踪参数、去片段；arXiv 版本号归并；规范化标题去重。同一批标题相似度默认 ≥0.92 合并；跨次使用 URL/标题精确哈希，不做跨语言语义去重。
- `max_items` 默认每渠道最多 12 条；微信总文本控制在 16 KB 内，超出时减少条数，只记录实际提交的条目。
- 没有发布时间的条目排除；未来超过 1 小时的条目排除。RSS 优先 published，缺失才用 updated。
- GitHub 使用官方仓库搜索 API，按最近 push 时间筛选、总星数排序，**不是 GitHub Trending，也不是新发布新闻**。已推送仓库默认 90 天内不重复推送，即使之后有更新。
- arXiv 抓取最新 40 篇，GitHub 最多 30 个候选；各可调到 100。V1 有意限制抓取量，并非所有 AI 论文或仓库的完整覆盖。
- 默认来源：IT之家、极客公园的中文 RSS，经过 AI 关键词过滤。2026-09-06 实测两个源均可用；量子位返回 403，机器之心未返回有效 RSS，因此不纳入默认配置。
- `chinese_only: true` 要求标题及实际展示的摘要各至少含两个汉字，且汉字占中英文字母至少 20%，允许 GPT、OpenAI 等产品名称。这是可读性启发式筛选，不是翻译，也不保证语种识别完全准确。
- arXiv 与 GitHub 抓取代码保留但默认关闭，避免英文内容占据日报；如需恢复原文模式，改为 `enabled: true` 并设置 `chinese_only: false`。可扩展 RSS 数组；不提供任意网页爬虫兜底。

部分源失败：继续其他源，`status.json` 记录失败种类/HTTP 状态。所有源失败：退出码 1，不发送日报。过滤后为空或没有新内容：生成本地报告，不发送空通知。GET 有有限次数重试和超时，arXiv 每次运行只请求一次（失败重试除外）。

## 5. 费用边界

项目没有付费大模型、云数据库、常驻服务器依赖。公共仓库的标准 GitHub 托管 runner 目前免费；私有仓库使用账户包含额度，超额可能收费。SMTP 使用已有邮箱服务；pushplus 微信渠道官方标注免费，但有服务额度和账户限制。**0 元成立于这些免费条件内，不保证第三方永久免费**；不要启用收费 runner 或收费通知渠道。

## 6. 故障排查

| 情况 | 检查 |
| --- | --- |
| FATAL ValueError | JSON 配置、缺少的环境变量、SMTP_PORT、损坏的状态 JSON |
| 邮件失败 | SMTP 是否开启、是否使用授权码、TLS 模式和端口、发件地址权限 |
| 微信失败 | token、公众号绑定、免费额度与平台发送记录 |
| 当天没有消息 | status.json 中来源状态、关键词、发布时间、已推送记录 |
| GitHub 403/429 | token 与搜索限流，稍后再运行 |
| 某 RSS 403 或 arXiv 超时 | 网络/源端限制；其余来源继续，必要时更换配置的源 |
| 状态保存失败 | contents: write、分支规则；先检查实际收到的通知再重跑 |
| 定时任务不执行 | 默认分支 workflow、Actions 是否启用、60 天无活动规则 |

## 参考文档

- [GitHub Actions 费用](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [GitHub schedule 触发条件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub 仓库搜索 API](https://docs.github.com/en/rest/search/search#search-repositories)
- [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)
- [pushplus API 与微信渠道](https://www.pushplus.plus/doc/guide/api.html)

文档与接口核对日期：2026-09-06。真实送达需要用户配置自己的凭据后验证。
