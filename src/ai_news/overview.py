"""Render public frozen headlines deterministically; no extra AI call or private data."""

from __future__ import annotations

import io
import re
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
    "开发": "开发工具与Agent",
    "安全": "安全与政策",
    "评测": "技术评测与洞察",
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
    # Keep English product names and Chinese monetary units intact when they
    # fit a line; fall back to characters only for a genuinely oversized token.
    pattern = r"\d+(?:\.\d+)?(?:万亿美元|亿美元|美元)|[A-Za-z0-9]+(?:[.+'’/-][A-Za-z0-9]+)*|万亿美元|亿美元|美元|[^\n]"
    tokens = re.findall(pattern, text.replace("\n", " "))
    for token in tokens:
        pieces = list(token) if draw.textlength(token, font=face) > width else [token]
        for piece in pieces:
            if current and draw.textlength(current + piece, font=face) > width:
                if piece[0] in "，。；：！？、）》】”":
                    # Avoid Chinese closing punctuation at the beginning of a line.
                    tail = re.findall(pattern, current)[-1]
                    if draw.textlength(tail + piece, font=face) > width:
                        tail = current[-1]
                    current = current[: -len(tail)]
                    lines.append(current.rstrip())
                    current = tail + piece
                else:
                    lines.append(current.rstrip())
                    current = piece.lstrip()
            else:
                current += piece
    return lines + ([current] if current else [])


def layout_cards(digest, measure, regular, width=1600, margin=64, gap=24):
    """Row-major importance order; only the final odd item spans both columns."""
    full_width = width - margin * 2
    half_width = (full_width - gap) // 2
    cards, y = [], 242
    numbered = list(enumerate(digest.stories, 1))
    for section in ("news", "observation"):
        entries = [(i, s) for i, s in numbered if s.section == section]
        if section == "observation" and entries:
            y += 18
        for offset in range(0, len(entries), 2 if section == "news" else 1):
            row = entries[offset : offset + (2 if section == "news" else 1)]
            card_width = half_width if len(row) == 2 else full_width
            prepared = []
            for index, story in row:
                text = ("【补录】" if story.backfill else "") + story.title
                lines = wrap(text, measure, regular, card_width - 56)
                prepared.append((index, story, lines))
            height = 108 + max(len(lines) for _, _, lines in prepared) * 46
            for column, (index, story, lines) in enumerate(prepared):
                cards.append(
                    (margin + column * (card_width + gap), y, card_width, height, index, story, lines)
                )
            y += height + gap
    return cards, y - gap if cards else y


def overview_png(digest: Digest) -> bytes:
    width, margin = 1600, 64
    regular, small, heading, title = font(34), font(23), font(30), font(62)
    canvas = Image.new("RGB", (width, 100), "#F6F4EF")
    measure = ImageDraw.Draw(canvas)
    cards, bottom = layout_cards(digest, measure, regular)
    height = max(460, bottom + 138)
    canvas = Image.new("RGB", (width, height), "#F6F4EF")
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 32), "全球 AI · 每日精选", font=small, fill="#647371")
    period = "早报" if digest.issue.endswith("am") else "晚报"
    date = digest.issue[:10].replace("-", "年", 1).replace("-", "月") + "日"
    draw.text((margin, 78), f"{date}  AI{period}", font=title, fill="#B75936")
    mode = "AI精选" if digest.mode == "ai" else "基础规则版 · AI增强未完成"
    cutoff = digest.cutoff.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    draw.text(
        (margin, 172),
        f"{len(digest.news)}条要闻  ·  {len(digest.observations)}条前瞻  ·  {mode}  ·  截止{cutoff} 北京时间",
        font=small,
        fill="#647371",
    )
    for x, y, card_width, card_height, index, story, lines in cards:
        category = story.category
        color = COLORS[list(HEADINGS).index(category) % len(COLORS)] if category in HEADINGS else COLORS[0]
        observation = story.section == "observation"
        draw.rounded_rectangle((x, y + 5, x + card_width, y + card_height + 5), radius=26, fill="#E9E6DF")
        draw.rounded_rectangle(
            (x, y, x + card_width, y + card_height), radius=22, fill="#FFF8EE" if observation else "white"
        )
        draw.text((x + 28, y + 21), f"{index:02d}", font=heading, fill=color)
        label = "前瞻观察 · 未经官方确认" if observation else HEADINGS.get(category, category or "精选要闻")
        draw.text((x + 92, y + 26), label, font=small, fill=color)
        for line_index, line in enumerate(lines):
            draw.text((x + 28, y + 77 + line_index * 46), line, font=regular, fill="#253437")
    if not cards:
        draw.text((margin, 270), "本期暂无达到来源与时效要求的新资讯。", font=regular, fill="#253437")
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
