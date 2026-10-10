# 部署与配置

## 已自动准备的最少操作入口

本项目提供本机一次性配置页。仓库创建、代码推送、Pages 设置和云端检查可由助手自动完成；你只需在本机页面填写发信邮箱、SMTP 授权码、Gemini Key，并确认专用项目未关联付费账单。接收邮箱默认与发信邮箱相同，可按需修改。

配置页先要求当前代码的云端 Tests 与信源 20/8 门槛通过，再使用 GitHub 公钥 sealed-box 加密写入 Secrets。全部配置成功才启用任务并启动第一期运行。原始密钥只在本机进程内存使用，不进入项目文件或日志；页面提交后服务自动关闭。

本机启动方式：`python scripts/configure_local.py --owner zyc8717757`。打开输出的本机地址，不要把带 token 的配置地址转发给别人。配置页仅绑定 `127.0.0.1`，自动选择空闲端口，避免与旧入口冲突。

页面可设置早报、晚报时间（北京时间），默认 `08:00`、`20:00`。两项须为同一天内从早到晚的不同时间。保存时原子更新 `config/settings.yaml` 和 GitHub workflow 的两个 cron，并等待新提交的云端检查通过后启用。不会要求你手工编辑 cron。

以后只调整时间可使用 `python scripts/configure_local.py --owner zyc8717757 --schedule-only`，无需重新输入邮箱授权码和 Gemini Key。该操作只修改远程的两份调度配置，使用非强制提交保护并发改动；本地开发前同步最新 main。

只更换Gemini Key可使用 `python scripts/configure_local.py --owner zyc8717757 --ai-key-only`。该入口只更新Gemini密钥和免费确认，复用邮箱与时间，直接对已冻结规则日报生成AI修订再部署/发信；成功AI期号重跑不会再次调用模型。所有请求仍计入原期号上限。

Gemini 2.5系列目前限制给过去实际使用过这些模型的项目/用户，新项目即使显示配额也可能返回404。保持2.5模型时必须使用以前成功调用过2.5的免费项目Key。[官方模型可用性说明](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite.md)

本次实测新旧Key调用2.5主备均返回404，用户已同意使用免费3.5 Flash-Lite进行验证，当前配置主3.5 Flash-Lite、备3.8 Flash。免费层价格：[官方价格](https://ai.google.dev/gemini-api/docs/pricing)。Key不因换模型自动重置次数；软件不能用控制台显示配额替代真实调用验收。

内置浏览器的表单 Origin 可能为 `null` 或缺失。配置服务仍要求精确回环 Host 和不可猜测的会话表单令牌，明确的外站 Origin 始终拒绝。提交后立即显示进度页，不会在浏览器中长时间无反馈；不记录表单正文。

保存密钥后，配置页继续等待对应云端运行，并核对日报模式为AI、Pages已确认、SMTP已接受三个条件。仅保存成功或规则版工作流成功不会显示全流程通过；失败时展示固定诊断码和运行链接，已保存的Key无需重复输入。

已登录 Git Credential Manager 时，`python scripts/github_setup.py --owner zyc8717757 --publish` 可上传指定白名单代码并配置 Pages；不会修改系统 GitHub 认证、系统代理或全局 Git 配置。`--check-sources` 启动云端信源检查。

## 1. 准备仓库

核验实际 GitHub 登录账号，新建或选择公共仓库 `ai-news-auto-publisher`，将项目推送到 `main`。不要上传 `.venv`、`.local`、`.runtime`、`.env` 或邮件文件。

在 Settings → Pages → Build and deployment 中选择 **GitHub Actions**。使用标准 `ubuntu-latest`，不要切换收费的大规格运行器。工作流自动创建/使用独立 `news-data` 分支；首次启动不需要手工上传历史文件。

工作流的写权限仅用于受信任默认分支的正式任务。PR 测试没有业务 Secrets。`news-data` 仅存公开日报及不含个人信息的技术状态。若分支保护禁止机器人更新此分支，应在不放宽 `main` 保护的前提下为该数据分支配置所需权限。

## 2. 配置 Variables

Settings → Secrets and variables → Actions → Variables：

| 名称 | 初始值及含义 |
|---|---|
| `PAGES_BASE_URL` | 实际 Pages 地址，如 `https://OWNER.github.io/ai-news-auto-publisher`，不含尾部 `/` |
| `PUBLISHER_ENABLED` | 初始 `false`；完成前置验证后改 `true` |
| `PUBLISHER_PAUSED` | `false`；紧急暂停改为 `true` |
| `AI_ENABLED` | 初始 `false` |
| `EMAIL_ENABLED` | 初始 `false` |
| `AI_FREE_TIER_CONFIRMED` | 初始 `false`；仅人工确认专用免费项目无付费账单后改 `true` |
| `GEMINI_RPM` | 当前截图 Lite 为 10、Flash 为 5；系统对两者合计保守设为 5 |
| `GEMINI_TPM` | 当前3.5截图为 250000；低于单次预留需求时会安全降级 |
| `GEMINI_RPD` | AI Studio 核实值，3.5截图为500；按太平洋时间零点重置，不能超过该值 |

配额按项目共享，不是按 Key 单独计算。建议此免费项目只服务本系统。程序采用输入 UTF-8 大小加最大输出长度保守预留，不调用额外计数 API。

当前3.5 Flash-Lite截图配额为15 RPM、250000 TPM、500 RPD；每期最多20次。备用模型保守限制5 RPM / 20 RPD，不继承3.5提高的配额。400/403/404不重试或切换模型，429/503有限退避，认证/免费条件错误不会通过换模型绕过。正常最多 30 个候选用一次请求批量处理，修复/重试也计入同一额度账本。

## 3. 配置 Secrets

在 **Secrets** 标签页配置，勿放入 Variables、代码、聊天或截图：

| 名称 | 说明 |
|---|---|
| `GEMINI_API_KEY` | 已核验专用免费项目的 API Key |
| `SMTP_HOST` | QQ：`smtp.qq.com`；163：`smtp.163.com` |
| `SMTP_PORT` | 推荐 `465` |
| `SMTP_SECURITY` | 推荐 `ssl`；支持配合 587 的 `starttls` |
| `SMTP_USERNAME` | 发信邮箱完整地址 |
| `SMTP_PASSWORD` | 邮箱 SMTP 授权码，通常不是网页登录密码 |
| `MAIL_FROM` | 发信邮箱完整地址，建议与认证账号相同 |
| `MAIL_TO` | 你的私人收件邮箱，v1 支持一个接收地址 |

QQ / 163 先在各自官方邮箱设置中开启 SMTP，并生成授权码。认证方式和可用性以当前邮箱设置为准。系统不会帮你关闭安全措施，也不支持明文 SMTP 端口 25。

## 4. 分阶段启用

1. 运行 Tests，确认测试和样例预览通过。
2. 手动运行 News Publisher，模式选择 `check-sources`。要求至少 20 个信源传输/格式正常，其中至少 8 个官方或研究来源。该门槛不是新闻数量保证。
3. 使用 `preview` 检查样例，尚不产生公开部署或真实邮件。
4. 核验目标仓库、公开内容范围和 `PAGES_BASE_URL`，设置 `PUBLISHER_ENABLED=true`，AI 和邮件仍保持关闭。
5. 手动选择 `run`，填一个已结束的当日期号，验证规则日报、`news-data`、Pages 固定链接和修订标记。
6. 确认 Gemini 专用免费项目未绑定付费账单，将 `AI_FREE_TIER_CONFIRMED`、`AI_ENABLED` 设为 `true`。生成新的已结束期号或显式 `regenerate`，检查模式确实为 `ai`，不能把降级版算作 AI 通过。
7. 配好邮箱 Secrets，设置 `EMAIL_ENABLED=true`，对已冻结期号运行 `send`。确认收件箱正文、两版文案、中文及附件。SMTP 接受不等于实际收到。
8. 检查早晚计划触发，开始连续 7 天观察。部署完成后电脑和 Codex 可退出。

手动运行只接受默认分支的正式任务。模式说明见运行文档。`run` 和 `regenerate` 会生成/部署并按开关发信；`redeploy` 不重新生成、不发邮件；`send` 不采集、不调用 AI、不部署。

## 5. 发布和隐私检查

发布产物仅允许经过白名单审计的 HTML、CSS 和日报 JSON。工作流自行打包该目录，再用固定 SHA 的官方 Artifact Action 上传，避免在复合 Action 内引入浮动版本依赖。

只有公开站点压缩包进入 Artifact。原始采集内容只临时使用，私有邮件和草稿不落盘。公开 JSON 经过严格类型校验，禁止增加 `x_draft`、邮箱或认证配置字段。

新闻内容可能包含不可信指令。程序将其作为数据，不允许其更改模型、连接地址、邮件接收人或发布流程。

## 6. 免费与可用性边界

GitHub 定时任务可能延迟、丢弃或因长期无活动停用。超过计划时点 6 小时的自动任务会跳过；手动入口可补处理。不要把它作为精确到秒的调度器。

本机直连结果不等于 GitHub 云端结果。DNS 污染、代理和网络封锁可能影响本地抓取；程序拒绝内网/元数据地址和不安全重定向，不为通过测试关闭保护。

不得在未确认免费账单状态时打开 AI；不要因额度耗尽自动切换付费项目或模型。
