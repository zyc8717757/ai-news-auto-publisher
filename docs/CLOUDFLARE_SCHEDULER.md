# 独立云端调度兜底

Worker 每5分钟检查当前北京时间最近一期，读取公开配置和公开状态，仅在未完成时触发既有 GitHub 工作流。已发送、发送中或结果未知的邮件不会重复触发。每期最多4次触发，间隔至少30分钟；GitHub自身并发锁、冻结日报及SMTP状态机继续保护重复执行，AI每期20次限制不变。

Cloudflare Cron独立于GitHub定时事件，但实际新闻处理仍依赖GitHub运行器；不能保证平台故障时绝不延误。免费计划CPU、Cron数量、KV配额需部署时核验，不启用付费计划。

部署前须完成：Cloudflare登录并确认目标账户使用Workers Free；创建专用KV命名空间，替换配置中的占位ID；创建仅授权本仓库的GitHub fine-grained token，权限仅Actions读写（以及默认Metadata读取），作为Worker Secret `GITHUB_DISPATCH_TOKEN` 输入。不要复用Git Credential Manager的广泛登录凭据，不要在聊天、源码或日志中输入Token。Worker不接收Gemini或SMTP凭据。

默认 `ENABLED=false`，完成真实部署、云端手动测试（已完成期号应返回completed）、Cron触发日志验证后才启用。Cron更改传播可能需要时间，以Cloudflare实际事件日志为准；尚未完成账号授权及部署时，不能宣称独立调度已上线。
