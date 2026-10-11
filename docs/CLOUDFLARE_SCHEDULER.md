# 独立云端调度兜底

Worker 每5分钟检查当前北京时间最近一期，读取公开配置和公开状态，仅在未完成时触发既有 GitHub 工作流。已发送、发送中或结果未知的邮件不会重复触发。每期最多4次触发，间隔至少30分钟；GitHub自身并发锁、冻结日报及SMTP状态机继续保护重复执行，AI每期20次限制不变。

Cloudflare Cron独立于GitHub定时事件，但实际新闻处理仍依赖GitHub运行器；不能保证平台故障时绝不延误。免费计划CPU、Cron数量、KV配额需部署时核验，不启用付费计划。

部署前须完成：Cloudflare登录并确认目标账户使用Workers Free；创建专用KV命名空间，替换配置中的占位ID；创建仅授权本仓库的GitHub fine-grained token，权限仅Actions读写（以及默认Metadata读取），作为Worker Secret `GITHUB_DISPATCH_TOKEN` 输入。不要复用Git Credential Manager的广泛登录凭据，不要在聊天、源码或日志中输入Token。Worker不接收Gemini或SMTP凭据。

初次部署使用 `ENABLED=false`。完成免费计划核验、OAuth和限权GitHub Token的无AI/无邮件preview触发验证后，改为 `ENABLED=true` 部署，再核对真实Cron事件。生产配置现为已启用；凭据只保存为Worker Secret，不提交Git。

`scripts/configure_watchdog.py` 提供仅监听127.0.0.1的一次性安全录入页：Host、Origin、CSRF及显式保存确认校验，通过preview验证Actions写权限，再经标准输入交给固定版本Wrangler写入Secret。Token不作为命令行参数，不写本地文件或日志。GitHub Token只显示一次，生成后立即复制到安全页；过期前需人工更新（建议90天有效期）。禁止复用宽权限Git登录凭据。

部署CLI固定Wrangler 4.149.0，OAuth使用account:read、user:read、workers_scripts:write、workers_kv:write及客户端刷新登录所需offline_access。OAuth凭据通过官方keyring选项存入Windows凭据库密钥保护的加密文件。现有域名、Worker和其他KV不修改。

Cron更改传播可能需要时间，以Cloudflare实际事件日志为准；部署成功不等于已验证真实Cron触发。代码测试及Token的preview验证也不等于未来每期一定投递。

每5分钟真实Cron继续检查并记录日志；专用KV的 `heartbeat` 每30分钟持久化计划时间及固定状态，保留24小时，不含Token或正文。稳定运行约48次心跳写入/UTC日，另加最多8次早晚触发预留；首次启动或升级心跳格式可能额外写入。一次性preview已验证，生产关闭VERIFY_PREVIEW，避免每轮额外读取。仍须与同账户其他应用共用免费KV日配额，心跳最多落后约30分钟，应结合真实调用日志判断故障。可用固定版本Wrangler的KV get命令只读检查心跳，日志界面不可用时仍能核验执行证据。

巡检按实际执行时间选择最近一期，延迟事件不能沿用过期的计划时间选期；心跳分别记录执行时间 `at` 与原计划时间 `scheduled_at`。当前Cron为等价的明确分钟列表（0、5、10…55）。无Cron配置变化的代码修复可通过 `wrangler versions upload` 和 `wrangler versions deploy <版本>@100% --yes` 更新，避免反复重新登记触发器。

可选一次性 `VERIFY_PREVIEW=true` 自检由Worker自身调用GitHub preview，不采集、不调用AI、不发邮件，成功后保存 `preview_verified` 元数据，之后不再重复preview。该标记加真实心跳才能验证云端凭据及巡检链路；本机安全页的preview成功仅验证录入Token本身。

HTTP请求每次最多20秒；每个持久化周期只保存一次completed/dispatched等终态，失败记录脱敏错误码。取消首次started与终态连续写同一键，避免触及KV单键每秒写入限制。不能将本机模拟或手工写入当成真实Cron证据。

## 2026-10-11运行时修复

实测原Worker在Cloudflare发出网络请求前抛出TypeError，使用 `redirect: "error"` 的版本连续失败。改为 `manual` 并拒绝所有非预期状态（包括3xx），不跟随任何跳转、不向其他主机转发凭据后，18:20北京时间真实Cron完成，且Worker发起的GitHub preview验证成功。Node fetch可接受的参数不等于Workers运行时全部兼容，应以真实Cron心跳和云端preview作为发布验收证据。

同时保留原生fetch调用上下文，并去除认证字符串首尾空白、拒绝内部空白；测试覆盖拒绝重定向、凭据不转发、网络超时脱敏、迟到按执行时间选期、自检只触发一次及已发期号跳过。运行时错误不属于Gemini调用，也不应通过重置AI计数处理。

## 全账户免费边界与重复执行

配额按UTC日、跨全部Workers及KV命名空间共享，不按本项目单独计算。巡检固定每天288次调用，约288次心跳读取及最多8次触发状态读取；正常仅48次心跳写入加最多8次触发预留。实际检查发生平台重放、人工验证或部署时会增加用量。workers.dev及preview URL均关闭，不对公网请求开放。不要自动开通付费或修改其他应用来满足额度。其他应用未来用量无法由本项目约束，须检查账户级Usage；免费额度耗尽可能让服务失败。

Cloudflare KV是最终一致存储，不能充当跨平台原子锁。所有真实执行最终进入同一news.yml工作流并发组ai-news-publisher；取得执行权后才读取最新news-data。已冻结期号不重复采集或调用AI，已确认sent不再发送；sending/unknown需要人工核对，不自动补发。重复事件可能创建多个工作流，但不会由两条入口并行采集推送。
