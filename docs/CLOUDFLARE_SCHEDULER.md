# 独立云端调度兜底

Worker 每5分钟检查当前北京时间最近一期，读取公开配置和公开状态，仅在未完成时触发既有 GitHub 工作流。已发送、发送中或结果未知的邮件不会重复触发。每期最多4次触发，间隔至少30分钟；GitHub自身并发锁、冻结日报及SMTP状态机继续保护重复执行，AI每期20次限制不变。

Cloudflare Cron独立于GitHub定时事件，但实际新闻处理仍依赖GitHub运行器；不能保证平台故障时绝不延误。免费计划CPU、Cron数量、KV配额需部署时核验，不启用付费计划。

部署前须完成：Cloudflare登录并确认目标账户使用Workers Free；创建专用KV命名空间，替换配置中的占位ID；创建仅授权本仓库的GitHub fine-grained token，权限仅Actions读写（以及默认Metadata读取），作为Worker Secret `GITHUB_DISPATCH_TOKEN` 输入。不要复用Git Credential Manager的广泛登录凭据，不要在聊天、源码或日志中输入Token。Worker不接收Gemini或SMTP凭据。

初次部署使用 `ENABLED=false`。完成免费计划核验、OAuth和限权GitHub Token的无AI/无邮件preview触发验证后，改为 `ENABLED=true` 部署，再核对真实Cron事件。生产配置现为已启用；凭据只保存为Worker Secret，不提交Git。

`scripts/configure_watchdog.py` 提供仅监听127.0.0.1的一次性安全录入页：Host、Origin、CSRF及显式保存确认校验，通过preview验证Actions写权限，再经标准输入交给固定版本Wrangler写入Secret。Token不作为命令行参数，不写本地文件或日志。GitHub Token只显示一次，生成后立即复制到安全页；过期前需人工更新（建议90天有效期）。禁止复用宽权限Git登录凭据。

部署CLI固定Wrangler 4.149.0，OAuth使用account:read、user:read、workers_scripts:write、workers_kv:write及客户端刷新登录所需offline_access。OAuth凭据通过官方keyring选项存入Windows凭据库密钥保护的加密文件。现有域名、Worker和其他KV不修改。

Cron更改传播可能需要时间，以Cloudflare实际事件日志为准；部署成功不等于已验证真实Cron触发。代码测试及Token的preview验证也不等于未来每期一定投递。

每次真实Cron执行向专用KV的 `heartbeat` 写入计划时间及固定状态，保留24小时，不含任何Token或正文。每5分钟约288次写入/天，另加最多8次早晚触发预留，仍须与同账户其他应用共用免费KV日配额。可用固定版本Wrangler的KV get命令只读检查心跳，日志界面不可用时仍能核验执行证据。

巡检按实际执行时间选择最近一期，延迟事件不能沿用过期的计划时间选期；心跳分别记录执行时间 `at` 与原计划时间 `scheduled_at`。当前Cron为等价的明确分钟列表（0、5、10…55）。无Cron配置变化的代码修复可通过 `wrangler versions upload` 和 `wrangler versions deploy <版本>@100% --yes` 更新，避免反复重新登记触发器。

可选一次性 `VERIFY_PREVIEW=true` 自检由Worker自身调用GitHub preview，不采集、不调用AI、不发邮件，成功后保存 `preview_verified` 元数据，之后不再重复preview。该标记加真实心跳才能验证云端凭据及巡检链路；本机安全页的preview成功仅验证录入Token本身。

首次执行在网络请求前记录 `started`，HTTP请求每次最多20秒，完成后更新为completed/dispatched等固定状态；失败仅记录脱敏错误码。首次额外启动记录增加1次KV写入，不能将只有started的记录当成完整巡检成功，也不能将本机模拟或手工写入当成真实Cron证据。
