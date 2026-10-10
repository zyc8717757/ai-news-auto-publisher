from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import Digest, DraftBundle


class TextCounter:
    def __init__(self, script: Path | None = None):
        self.script = script or Path(__file__).resolve().parents[2] / "scripts" / "text.cjs"

    def call(self, operation: str, texts: list[str], budget: int = 50):
        result = subprocess.run(
            ["node", str(self.script)],
            input=json.dumps({"operation": operation, "texts": texts, "budget": budget}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode:
            raise RuntimeError("Official text counter unavailable; run npm ci")
        return json.loads(result.stdout)

    def weights(self, texts: list[str]) -> list[int]:
        return self.call("weights", texts)

    def shorten(self, texts: list[str], budget: int) -> list[str]:
        return self.call("shorten", texts, budget)


def make_drafts(digest: Digest, url: str, counter: TextCounter | None = None) -> DraftBundle:
    counter = counter or TextCounter()
    header = digest.label + ("｜基础规则版" if digest.mode == "rules" else "")
    titles = [story.title for story in digest.news[:3]]
    for budget in (55, 45, 35, 25, 15):
        clipped = counter.shorten(titles, budget)
        short = "\n".join([header, *[f"{i}. {t}" for i, t in enumerate(clipped, 1)], "完整日报：", url])
        if counter.weights([short])[0] <= 280:
            break
    else:
        raise ValueError("Short draft cannot fit the official 280 weighted limit")
    if not titles:
        short = f"{header}\n本期暂无达到来源与时效要求的新资讯。\n完整日报：\n{url}"
    long = ""
    for summary_size, impact_size in ((500, 240), (200, 100), (120, 60), (70, 35)):
        summaries = counter.shorten([story.summary for story in digest.stories], summary_size)
        impacts = counter.shorten([story.impact for story in digest.stories], impact_size)
        sections = [header, "精选全球 AI 动态，事实与可能影响分开阅读。"]
        if digest.mode == "rules":
            sections.append("AI 增强未完成，以下保留来源原文信息，不代表中文深度复核。")
        for index, (story, summary, impact) in enumerate(
            zip(digest.stories, summaries, impacts, strict=True), 1
        ):
            prefix = "【前瞻观察·未经官方确认】" if story.section == "observation" else ""
            prefix += "【重要补录】" if story.backfill else ""
            section = f"{index}. {prefix}{story.title}\n{summary}"
            if impact:
                section += f"\n为什么重要：{impact}"
            if story.uncertainty:
                section += f"\n说明：{story.uncertainty}"
            if story.update_note:
                section += f"\n新增进展：{story.update_note}"
            section += f"\n来源：{story.sources[0].url}"
            sections.append(section)
        if not digest.stories:
            sections.append("本期暂无达到来源与时效要求的新资讯。")
        sections.append("完整日报：\n" + url)
        long = "\n\n".join(sections)
        if counter.weights([long])[0] <= 8000:
            break
    short_weight, long_weight = counter.weights([short, long])
    if short_weight > 280 or long_weight > 8000:
        raise ValueError("Draft length validation failed")
    return DraftBundle(short=short, long=long, short_weight=short_weight, long_weight=long_weight)
