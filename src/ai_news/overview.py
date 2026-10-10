"""Render public frozen headlines deterministically; no extra AI call or private data."""

from __future__ import annotations

import io
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .models import Digest

HEADINGS = {
    "模型": "模型发布",
    "产品": "产品应用",
    "开源": "开发生态",
    "科研": "技术与洞察",
    "商业": "行业动态",
    "政策": "政策与治理",
}
COLORS = ["#B75936", "#27666A", "#AE822A", "#A74454", "#466879", "#72608C"]


def font(size: int):
    for path in [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise RuntimeError("CJK font required; install fonts-noto-cjk")


def wrap(text, draw, face, width):
    lines, current = [], ""
    for char in text.replace("\n", " "):
        if current and draw.textlength(current + char, font=face) > width:
            lines.append(current)
            current = char
        else:
            current += char
    return lines + ([current] if current else [])


def overview_png(digest: Digest) -> bytes:
    width, margin, gap = 1600, 72, 36
    card_width = (width - margin * 2 - gap) // 2
    regular, small, heading, title = font(30), font(23), font(36), font(66)
    canvas = Image.new("RGB", (width, 100), "#F6F4EF")
    measure = ImageDraw.Draw(canvas)
    groups = {}
    for index, story in enumerate(digest.stories, 1):
        groups.setdefault(story.category, []).append((index, story))
    cards, bottoms = [], [254, 254]
    for category, entries in groups.items():
        # Split long categories across both columns instead of leaving half the image blank.
        for offset in range(0, len(entries), 8):
            rows = []
            for index, story in entries[offset : offset + 8]:
                rows.append(wrap(f"{index:02d}  {story.title}", measure, regular, card_width - 64))
            height = 118 + sum(len(lines) * 45 + 22 for lines in rows)
            column = 0 if bottoms[0] <= bottoms[1] else 1
            x, y = margin + column * (card_width + gap), bottoms[column]
            label = HEADINGS.get(category, category or "精选要闻") + ("（续）" if offset else "")
            cards.append((x, y, height, label, rows))
            bottoms[column] += height + gap
    height = max(620, max(bottoms) + 140)
    canvas = Image.new("RGB", (width, height), "#F6F4EF")
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 35), "AI NEWS  /  FREE EDITION", font=small, fill="#647371")
    period = "早报" if digest.issue.endswith("am") else "晚报"
    draw.text((margin, 86), f"{digest.issue[:10]}  AI资讯概览", font=title, fill="#B75936")
    mode = "AI精选" if digest.mode == "ai" else "基础规则版 · AI增强未完成"
    cutoff = digest.cutoff.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    draw.text(
        (margin, 184),
        f"{period}  ·  {len(digest.stories)}条  ·  {mode}  ·  截止{cutoff} 北京时间",
        font=small,
        fill="#647371",
    )
    for i, (x, y, card_height, category, rows) in enumerate(cards):
        color = COLORS[i % len(COLORS)]
        draw.rounded_rectangle((x, y + 5, x + card_width, y + card_height + 5), radius=26, fill="#E9E6DF")
        draw.rounded_rectangle((x, y, x + card_width, y + card_height), radius=26, fill="white")
        draw.rounded_rectangle((x + 28, y + 32, x + 34, y + 72), radius=3, fill=color)
        draw.text((x + 50, y + 23), category, font=heading, fill=color)
        position = y + 95
        for lines in rows:
            for line in lines:
                draw.text((x + 32, position), line, font=regular, fill="#253437")
                position += 45
            position += 22
    if not cards:
        draw.text((margin, 300), "本期暂无达到来源与时效要求的新资讯。", font=regular, fill="#253437")
    draw.text(
        (margin, height - 100),
        "仅汇总已定稿新闻 · 事实与限定说明请查看完整日报及原始来源",
        font=small,
        fill="#647371",
    )
    draw.text((margin, height - 60), f"AI News Auto Publisher  /  {digest.key}", font=small, fill="#647371")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()
