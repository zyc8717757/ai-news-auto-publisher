import pytest
from PIL import Image, ImageDraw

from ai_news.overview import font, layout_cards, wrap


@pytest.mark.parametrize("count", [1, 2, 3, 6, 15])
def test_row_major_order_has_no_empty_column_before_final_row(digest, count):
    stories = [
        digest.stories[0].model_copy(update={"id": str(i), "title": "重要新闻" + "标题" * (i % 4)})
        for i in range(count)
    ]
    digest = digest.model_copy(update={"stories": stories})
    measure = ImageDraw.Draw(Image.new("RGB", (1600, 100)))
    cards, bottom = layout_cards(digest, measure, font(34))
    assert [card[4] for card in cards] == list(range(1, count + 1))
    rows = {}
    for x, y, width, height, _, _, lines in cards:
        rows.setdefault(y, []).append((x, width, height))
        assert y + 77 + len(lines) * 46 <= y + height
        assert x + width <= 1536
    previous_bottom = None
    for y, row in rows.items():
        if previous_bottom is not None:
            assert y - previous_bottom == 24
        if len(row) == 2:
            assert row[0][0] < row[1][0] and row[0][2] == row[1][2]
        else:
            assert y == max(rows) and row[0][1] == 1472
        previous_bottom = y + row[0][2]
    assert bottom == previous_bottom


def test_observation_is_separate_full_width_at_end(digest):
    formal = digest.stories[0]
    observation = formal.model_copy(update={"id": "obs", "section": "observation"})
    mixed = digest.model_copy(update={"stories": [observation, formal]})
    cards, _ = layout_cards(mixed, ImageDraw.Draw(Image.new("RGB", (1600, 100))), font(34))
    assert [card[5].section for card in cards] == ["news", "observation"]
    assert cards[-1][2] == 1472
    assert cards[-1][1] > cards[0][1] + cards[0][3]


def test_product_name_and_money_unit_do_not_break_in_middle():
    draw = ImageDraw.Draw(Image.new("RGB", (1600, 100)))
    lines = wrap("发布Project Beacon安全项目，估值75亿美元。", draw, font(34), 350)
    assert "Beacon" in " ".join(lines)
    assert "亿美元" in " ".join(lines)
    assert all(draw.textlength(line, font=font(34)) <= 350 for line in lines)
