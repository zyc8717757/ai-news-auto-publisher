# AI News Auto Publisher v1.0 · Free Edition

默认每天北京时间 08:00 / 20:00 采集全球 AI 公开资讯，时间可在本机配置页调整。规则筛选与 Gemini 免费增强后发布 GitHub Pages 日报，并将完整日报和两版 X 文案发送到私人邮箱。**X 始终由人工审核、手动发布；没有 X API 或自动点击发帖功能。**

## 能力与公开边界

- 30 个候选信源：RSS、官方 GitHub Release、小范围官方 HTML 适配器。
- 时间校验、规则评分、转载识别、事件归并、48 小时历史去重与版本更新关联。
- Gemini 结构化中文摘要、影响分析、证据引用检查；免费条件不明或调用失败时明确降级。
- 每期最多 15 条；响应式日报、固定期号、版本保留、历史/月度归档。
- 私人邮件包含完整内容、精简版/Premium 长帖版和两份 UTF-8 文本附件。
- 精简版用官方 `twitter-text` 校验 280 加权长度，长帖版产品上限 8,000。
- 冻结日报、串行工作流、调用前持久化计数、邮件发送状态机及显式补发。

公共仓库与 Pages **不保存草稿、邮件、地址、授权码或原站全文**。X 草稿只在邮件发送过程中生成。不将 Actions Artifact 当作私人文件存储。

来源比较、评分和 AI 摘要均是辅助方法，不等于独立完成事实核验。官方声明、性能宣传及论文结果保留必要限定。阅读发布前请人工审核。

## 本地开始

要求 Python 3.12 和 Node.js 24。使用独立虚拟环境，不替换系统解释器。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
python -m pip install --no-deps --no-build-isolation -e .
npm ci --ignore-scripts
ai-news check-config
ai-news preview
```

如果所用内置 Python 没有 `ensurepip`，可以使用已有 pip 的 `--python .venv\Scripts\python.exe` 参数安装到虚拟环境；不必更换全局 Python。

演示页面输出到 `.local/preview`。所有示例均清楚标注为演示，**不是实际新闻**。预览不会调用 Gemini、发信、部署、修改持久化运行状态。

```powershell
python -m pytest -q
npm test
python -m ruff check src tests scripts
```

若 Windows 沙箱限制临时目录，可指定全新工作区临时路径，例如 `python -m pytest --basetemp=.local/test-session-new`。pytest 会清空已存在的 basetemp，请只使用专用测试目录。

## 常用命令

全局参数 `--config`、`--data`、`--remote` 放在子命令前。默认只操作 `.local/data`；远程持久化必须显式指定 `--remote`，并指向独立的 `news-data` 检出目录。

```powershell
# 只读验证公开信源，不调用 AI 或邮件
ai-news check-sources --enforce

# 真实采集但使用默认规则模式，期号必须已结束
ai-news generate --issue 2026-10-10-am

# 根据已冻结日报生成网站
ai-news build-site
ai-news audit-site --output site

# 创建新修订，保留旧版本；需要重新采集，AI 开启时会消耗免费额度
ai-news generate --issue 2026-10-10-am --regenerate

# 邮件仅在 EMAIL_ENABLED=true 且必要环境变量已配置时发送
ai-news send --issue 2026-10-10-am

# 可能重复，确认邮箱实际收信后使用
ai-news send --issue 2026-10-10-am --resend
```

默认 AI 和邮件均关闭；`base_url` 是必须更换的样例值。参见 [部署说明](docs/DEPLOYMENT.md)、[运行和回滚](docs/OPERATIONS.md)、[模块契约](docs/ARCHITECTURE.md)、[验收记录](docs/VALIDATION.md)。

## 免费约束

使用公共仓库的标准 GitHub Actions 运行器和 GitHub Pages，不使用收费运行器、付费采集、X API 或外部数据库。经用户确认，当前默认 `gemini-3.5-flash-lite`、备用 `gemini-3.8-flash`，官方价格页列有免费层。兼容白名单保留 2.5 系列及 3.1 Flash-Lite；固定 Developer API 地址并明确关闭 Vertex AI。统一使用已通过云端最小测试的 `v1beta/models/{model}:generateContent`，不启用工具，不维护服务器会话。

**同一模型也可能在付费项目中计费。程序不能仅凭 API Key 判断项目是否绑定账单。**必须人工确认专用项目无付费账单后，才能设置 `AI_FREE_TIER_CONFIRMED=true`。免费层政策变更时先关闭 AI，重新核验。正常每期优先一次批量处理，正常约 6–10 次（按需）；包括备用、重试及格式修复在内硬上限 20 次、按当前 3.5 免费项目每日最多 500 次（太平洋时间零点重置）。RPM/TPM/RPD 按实际项目配额执行，不足就降级，不切换付费项目。

官方参考：[Gemini 价格](https://ai.google.dev/gemini-api/docs/pricing)、[额度](https://ai.google.dev/gemini-api/docs/rate-limits)、[Actions 调度](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[Pages](https://docs.github.com/en/pages/getting-started-with-github-pages)。

## 当前交付状态

代码、离线样例、自动测试和工作流已提供。真实 Gemini 项目、QQ/163 收件箱、GitHub 云端部署、信源上线门槛及连续 7 天观察必须分别验收；本地测试通过不代表这些外部链路已经启用。具体证据见验收记录。


Gemini 最小诊断：Actions 手动选择 `probe-gemini`。只要求 `gemini-3.5-flash-lite` 回复 OK，不采集新闻、不部署、不发邮件；输出 SDK 版本、Key 是否存在、HTTP 状态和脱敏错误字段。默认恰好一次请求，调用前持久化预算，受独立测试授权和每日 500 次上限保护。诊断结果不等于结构化新闻处理验收通过。400/403/404 不自动重试或切换模型，429/503 仅有限退避重试，全部计入预算。AI 不可用或预算不足时，正常 `run` 继续生成明确标注的基础规则日报。


2026-10-10 配额校准：按所有者 AI Studio 截图，Gemini 3.5 Flash-Lite 为 15 RPM / 250,000 TPM / 500 RPD，后台显示当日用量 2。历史 10 次请求尝试完整保留，另记有时间和证据类型的 `ai_quota_snapshot`，不将失败尝试直接解释为后台计费或配额用量。校准后每次尝试（包括失败）继续保守占用配额；429 仍是服务端最终约束。此快照不是实时遥测，其他应用共享项目时也会消耗额度。每期 20 次限制不变，备用模型不继承 3.5 的提高后配额。

每期汇总图由 `overview` 使用 Pillow 与系统中文字体从公开定稿生成，包含全部精选标题和真实分类，不额外请求 AI。PNG 随公开站点发布，邮件附上两个私有 TXT 草稿和一张公开 PNG。Linux 运行器安装 fonts-noto-cjk；本机使用微软雅黑。图片不包含邮箱、草稿或认证信息。


## 综合重要新闻编辑策略

按两位日刊最近30天选题分析确定软目标：产品/订阅28%、模型25%、开发16%、商业13%、安全9%、评测9%。比例用于周/月参考，重要性优先，不为配比或条数凑数。普通论文不入选，重大研究最多2条；前瞻观察独立分区最多2条，不进入正式Top3。早晚均复查24小时，重大漏报允许48小时补录。已发事件继续去重，旧版冻结日报保留。

采集新增官方开发者社区、Anthropic研究公告、国内厂商及公开日刊线索。聚合日刊只负责发现原始链接，不作为独立核实来源；未注明时间的官方原文只能辅助有日期的公告，不以日刊或采集时间代替发布时间。304会复用候选，候选限额按来源分配，普通arXiv论文不会挤占厂商新闻。

AI分为初筛淘汰、跨语言事件归并与排序、摘要、事实终审；每期最多20次，跨重跑持久化。`validate-news`手动入口先重新采集并冻结待验证修订，不部署、不发邮件；核对通过后使用`redeploy`和`send`发布同一冻结修订，无需重复调用AI。详细契约见[编辑策略](docs/EDITORIAL_POLICY.md)。
