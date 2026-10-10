from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, select_autoescape

from .models import Digest
from .overview import overview_png
from .store import atomic_write

CSS = """:root{color-scheme:light;--ink:#182c35;--muted:#52666e;--line:#d5e0e2;--accent:#006d77}
*{box-sizing:border-box}body{margin:0;background:#f5f8f8;color:var(--ink);font:16px/1.8 system-ui,sans-serif}
main,header,footer{max-width:900px;margin:auto;padding:24px}header{border-bottom:1px solid var(--line)}
h1{font-size:clamp(26px,5vw,40px);line-height:1.3;margin:8px 0}h2{font-size:22px;line-height:1.5}
a{color:var(--accent);overflow-wrap:anywhere}nav{display:flex;gap:20px;flex-wrap:wrap}
.meta,small{color:var(--muted)}.tag{font-size:13px;color:var(--accent);background:#e3f3f0;padding:3px 10px;border-radius:5px}
article{background:white;border:1px solid var(--line);border-radius:12px;margin:20px 0;padding:24px}
.notice{padding:16px;border-left:4px solid #ad7312;background:#fff5dd}.impact{border-top:1px solid var(--line);padding-top:12px}
ul{padding-left:22px}footer{font-size:14px;border-top:1px solid var(--line)}p{overflow-wrap:anywhere}
@media(max-width:600px){main,header,footer{padding:16px}article{padding:18px}h2{font-size:20px}}
"""

TEMPLATE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ title }} · AI News</title>
<meta name="description" content="全球 AI 精选日报，来源可追溯，中文摘要与影响分析">
<link rel="stylesheet" href="{{ root }}/assets/style.css"></head><body>
<header {% if digest %}data-digest="{{ digest.key }}"{% endif %}><small>AI NEWS · FREE EDITION</small><h1>{{ title }}</h1>
<nav><a href="{{ root }}/index.html">最新日报</a><a href="{{ root }}/archive/index.html">历史归档</a>
<a href="{{ root }}/about.html">关于与纠错</a></nav></header><main>
{% if digest %}<p class="meta">截止 {{ local(digest.cutoff) }} · 更新 {{ local(digest.created_at) }} · 第 {{ digest.revision }} 版 · {{ digest.stories|length }} 条</p>
{% if digest.mode == 'rules' %}<p class="notice">基础规则版，AI 增强未完成。保留来源原文信息，不编造中文翻译或影响分析。</p>{% endif %}
{% if digest.warnings %}<p class="notice">{{ warnings }}</p>{% endif %}
{% if digest.editorial_version == 2 %}<p class="meta">综合重要新闻版 · {{ digest.news|length }} 条正式新闻 · {{ digest.observations|length }} 条前瞻观察 · 普通论文不入选</p>
{% if digest.news %}<h2>本期重点（最多三条）</h2><ol>{% for story in digest.news[:3] %}<li>{{ story.title }}</li>{% endfor %}</ol>{% endif %}{% endif %}
<p><a href="{{ root }}/assets/{{ digest.key }}-overview.png">下载本期资讯汇总图</a></p>
<img src="{{ root }}/assets/{{ digest.key }}-overview.png" alt="本期资讯分类概览" style="width:100%;height:auto;border-radius:12px">
{% for story in digest.stories %}{% if story.section == 'observation' and (loop.first or digest.stories[loop.index0-1].section != 'observation') %}<h2>前瞻观察</h2><p class="notice">以下未经官方确认，不属于正式 Top 3，不能视为已发布或已验证的能力。</p>{% endif %}
<article><span class="tag">{{ story.category }}{% if story.backfill %} · 重要补录{% endif %}</span><h2>{{ loop.index }}. {{ story.title }}</h2>
<p>{{ story.summary }}</p>{% if story.impact %}<p class="impact"><strong>为什么重要：</strong>{{ story.impact }}</p>{% endif %}
{% if story.selection_reason %}<p class="meta">入选理由：{{ story.selection_reason }}</p>{% endif %}
{% if story.uncertainty %}<p class="notice">{{ story.uncertainty }}</p>{% endif %}
{% if story.previous_issue %}<p>新增进展：{{ story.update_note }} <a href="{{ root }}/issues/{{ story.previous_issue }}.html">此前报道</a></p>{% endif %}
<ul>{% for source in story.sources %}<li><a href="{{ source.url }}" rel="noopener noreferrer">{{ source.source_name }} · {{ source.title }}</a>
<small>{{ source_time(source) }}</small></li>{% endfor %}</ul></article>{% else %}<p>本期暂无达到来源与时效要求的新资讯。</p>{% endfor %}
<nav>{% if previous %}<a href="{{ root }}/issues/{{ previous }}.html">上一期</a>{% endif %}
{% if following %}<a href="{{ root }}/issues/{{ following }}.html">下一期</a>{% endif %}</nav>
<nav>{% for revision in versions %}<a href="{{ root }}/revisions/{{ revision.key }}.html">历史版本 r{{ revision.revision }}</a>{% endfor %}</nav>
{% elif months is not none %}{% for month, issues in months.items() %}<h2><a href="{{ root }}/archive/{{ month }}.html">{{ month }}</a></h2><ul>
{% for value in issues %}<li><a href="{{ root }}/issues/{{ value.issue }}.html">{{ value.label }}</a> · {{ value.stories|length }} 条 · {{ 'AI版' if value.mode == 'ai' else '规则版' }}</li>{% endfor %}</ul>{% endfor %}
{% else %}<h2>关于本站</h2><p>自动采集全球 AI 公开资讯，优先一手来源。评分用于排序，不能替代事实核验。
官方性能宣传与未同行评审论文均需谨慎阅读。免费 AI 不可用时明确降级。</p>
<h2>纠错</h2><p>如发现错误，请通过<a href="{{ issue_url }}">项目 Issues</a>提供期号、条目和可靠来源。
纠错采用新版本，历史版本保留。本站不公开私人草稿、邮箱或认证信息。</p>{% endif %}
</main><footer>来源版权归原作者。本站提供摘要与来源链接，不归档原站全文。更新时间不代表事件发生时间。</footer></body></html>"""

WARNING_TEXT = {
    "source_errors": "部分新闻源暂不可用，本期覆盖可能不完整。",
    "ai_unavailable": "免费 AI 增强未完成。",
    "no_new_items": "本期没有符合要求的新资讯。",
}


def digest_url(base_url: str, digest: Digest) -> str:
    return f"{base_url.rstrip('/')}/issues/{digest.issue}.html"


def source_time(source) -> str:
    if source.published_at is None or source.time_precision == "unknown":
        return "原文未注明发布时间（仅辅助核实）"
    if source.time_basis == "repository_created":
        return (
            source.published_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M 北京时间")
            + "（仓库创建记录，非正式发布时间）"
        )
    if source.time_precision == "day":
        return (source.source_date or source.published_at.date().isoformat()) + "（仅日期，时区未知）"
    return source.published_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M 北京时间")


def build_site(digests: list[Digest], output: Path, base_url: str, repository: str = "") -> None:
    if not digests:
        raise ValueError("No digests to render")
    (output / "assets").mkdir(parents=True, exist_ok=True)
    latest = {}
    for digest in digests:
        (output / "assets" / f"{digest.key}-overview.png").write_bytes(overview_png(digest))
        if digest.issue not in latest or latest[digest.issue].revision < digest.revision:
            latest[digest.issue] = digest
    ordered = sorted(latest.values(), key=lambda digest: digest.cutoff)
    root = urlsplit(base_url).path.rstrip("/")
    env = Environment(autoescape=select_autoescape(default=True))
    template = env.from_string(TEMPLATE)

    def render(title, digest=None, previous=None, following=None, months=None):
        return template.render(
            title=title,
            digest=digest,
            previous=previous,
            following=following,
            months=months,
            versions=sorted(
                (d for d in digests if digest and d.issue == digest.issue), key=lambda d: d.revision
            ),
            root=root,
            warnings=" ".join(
                WARNING_TEXT.get(code, "本期部分处理能力受限。")
                for code in (digest.warnings if digest else [])
            ),
            local=lambda value: value.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
                "%Y-%m-%d %H:%M 北京时间"
            ),
            source_time=source_time,
            issue_url=f"https://github.com/{repository}/issues" if repository else "https://github.com/",
        )

    atomic_write(output / "assets/style.css", CSS)
    atomic_write(output / ".nojekyll", "")
    for index, digest in enumerate(ordered):
        previous = ordered[index - 1].issue if index else None
        following = ordered[index + 1].issue if index + 1 < len(ordered) else None
        atomic_write(
            output / "issues" / f"{digest.issue}.html", render(digest.label, digest, previous, following)
        )
    for digest in digests:
        atomic_write(output / "revisions" / f"{digest.key}.html", render(digest.label, digest))
        atomic_write(output / "data" / f"{digest.key}.json", digest.model_dump_json(indent=2))
    atomic_write(output / "index.html", render(ordered[-1].label, ordered[-1]))
    months = {}
    for digest in reversed(ordered):
        months.setdefault(digest.issue[:7], []).append(digest)
    atomic_write(output / "archive/index.html", render("历史归档", months=months))
    for month, values in months.items():
        atomic_write(output / "archive" / f"{month}.html", render(month + " 归档", months={month: values}))
    atomic_write(output / "about.html", render("关于与纠错"))
    audit_site(output)


def audit_site(output: Path) -> None:
    allowed = re.compile(
        r"^(index\.html|about\.html|\.nojekyll|assets/style\.css|"
        r"assets/\d{4}-\d{2}-\d{2}-(am|pm)-r\d+-overview\.png|"
        r"issues/\d{4}-\d{2}-\d{2}-(am|pm)\.html|"
        r"revisions/\d{4}-\d{2}-\d{2}-(am|pm)-r\d+\.html|"
        r"data/\d{4}-\d{2}-\d{2}-(am|pm)-r\d+\.json|"
        r"archive/(index|\d{4}-\d{2})\.html)$"
    )
    for path in output.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are forbidden in public artifacts")
        if path.is_file():
            if not allowed.fullmatch(path.relative_to(output).as_posix()):
                raise ValueError("Unexpected file in public artifact")
            if path.suffix == ".json":
                Digest.model_validate(json.loads(path.read_text("utf-8")))
