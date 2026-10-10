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

使用公共仓库的标准 GitHub Actions 运行器和 GitHub Pages，不使用收费运行器、付费采集、X API 或外部数据库。Gemini 默认 `gemini-2.5-flash-lite`，备用 `gemini-2.5-flash`；只允许这两个已确认免费模型，固定 Developer API 地址并明确关闭 Vertex AI。

**同一模型也可能在付费项目中计费。程序不能仅凭 API Key 判断项目是否绑定账单。**必须人工确认专用项目无付费账单后，才能设置 `AI_FREE_TIER_CONFIRMED=true`。免费层政策变更时先关闭 AI，重新核验。正常每期优先一次批量处理，尽量 1–3 次；包括备用、重试及格式修复在内硬上限 5 次、滚动 24 小时最多 10 次。RPM/TPM/RPD 按实际项目配额执行，不足就降级，不切换付费项目。

官方参考：[Gemini 价格](https://ai.google.dev/gemini-api/docs/pricing)、[额度](https://ai.google.dev/gemini-api/docs/rate-limits)、[Actions 调度](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[Pages](https://docs.github.com/en/pages/getting-started-with-github-pages)。

## 当前交付状态

代码、离线样例、自动测试和工作流已提供。真实 Gemini 项目、QQ/163 收件箱、GitHub 云端部署、信源上线门槛及连续 7 天观察必须分别验收；本地测试通过不代表这些外部链路已经启用。具体证据见验收记录。
