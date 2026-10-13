"""Exact quantity normalization, separate from semantic claim approval.

These checks establish numerical compatibility only. The independent editorial
review must also bind each claim to a verbatim cited passage and check its metric,
subject, direction, range and qualifiers. Publication timestamps are dates, never
a bag of digits that can justify unrelated performance or monetary claims.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

SCALES = {
    "thousand": 1000,
    "million": 10**6,
    "billion": 10**9,
    "trillion": 10**12,
    "k": 1000,
    "m": 10**6,
    "b": 10**9,
    "bn": 10**9,
    "t": 10**12,
    "千": 1000,
    "万": 10**4,
    "亿": 10**8,
    "万亿": 10**12,
}
WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
CURRENCY = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "美元": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "欧元": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "英镑": "GBP",
    "CNY": "CNY",
    "人民币": "CNY",
}
TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?"
    r"|(?:\d{4}年)?\d{1,2}月\d{1,2}日"
    r"|(?<![a-z0-9_.])\d+(?:\.\d+){2,}(?![a-z0-9_.])"
    r"|(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*(?![A-Za-z0-9_.-])"
    r"|(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?"
    r"|\b(?:" + "|".join(WORDS) + r")\b",
    re.I,
)
SCALE = re.compile(r"\s*(trillion|billion|million|thousand|万亿|亿|万|千|bn|[kmbt])(?=$|[^a-z])", re.I)


@dataclass(frozen=True)
class Quantity:
    start: int
    end: int
    value: Decimal | str
    unit: str


def quantities(text: str) -> list[Quantity]:
    result = []
    for match in TOKEN.finditer(text):
        token = match.group()
        start, end = match.span()
        if re.match(r"\d{4}-\d{2}-\d{2}", token):
            result.append(Quantity(start, end, token[:10], "date"))
            continue
        if "月" in token:
            parts = list(map(int, re.findall(r"\d+", token)))
            value = f"{parts[-2]:02}-{parts[-1]:02}"
            if len(parts) == 3:
                value = f"{parts[0]:04}-" + value
            result.append(Quantity(start, end, value, "date"))
            continue
        if re.fullmatch(r"v?\d+(?:\.\d+){2,}", token, re.I):
            result.append(Quantity(start, end, token.lstrip("vV"), "version"))
            continue
        if token.lower() not in WORDS and (re.search(r"[a-z]", token, re.I) or token.count(".") > 1):
            result.append(Quantity(start, end, token.casefold(), "identifier"))
            continue
        value = Decimal(WORDS[token.lower()]) if token.lower() in WORDS else Decimal(token.replace(",", ""))
        unit = "number"
        scale = SCALE.match(text[end:])
        if scale:
            value *= SCALES[scale[1].lower()]
            end += scale.end()
        prefix, suffix = text[:start], text[end:]
        currency = re.search(r"(US\$|USD|EUR|GBP|CNY|\$|€|£|美元|欧元|英镑|人民币)\s*$", prefix, re.I)
        after = re.match(r"\s*(美元|欧元|英镑|人民币|USD\b|EUR\b|GBP\b|CNY\b)", suffix, re.I)
        if currency or after:
            name = (currency or after)[1]
            unit = CURRENCY.get(name, CURRENCY.get(name.upper(), "unknown_currency"))
            if currency:
                start = currency.start()
            if after:
                end += after.end()
        elif re.match(r"\s*(?:percentage points?\b|个百分点)", suffix, re.I):
            unit = "percentage_points"
            end += re.match(r"\s*(?:percentage points?\b|个百分点)", suffix, re.I).end()
        elif re.match(r"\s*(?:%|percent\b|per cent\b)", suffix, re.I) or prefix.endswith("百分之"):
            unit = "ratio"
            value /= 100
            percent = re.match(r"\s*(?:%|percent\b|per cent\b)", suffix, re.I)
            if percent:
                end += percent.end()
        elif re.match(r"\s*(?:倍|[x×](?![a-z]))", suffix, re.I):
            unit = "multiple"
            end += re.match(r"\s*(?:倍|[x×](?![a-z]))", suffix, re.I).end()
        else:
            storage = re.match(r"\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|bytes?\b|字节)", text[match.end() :], re.I)
            if storage:
                name = storage[1].lower()
                powers = {"tb": 12, "gb": 9, "mb": 6, "kb": 3}
                binary = {"tib": 40, "gib": 30, "mib": 20, "kib": 10}
                raw_value = (
                    Decimal(token.replace(",", ""))
                    if token.lower() not in WORDS
                    else Decimal(WORDS[token.lower()])
                )
                value = raw_value * (
                    Decimal(2) ** binary[name] if name in binary else Decimal(10) ** powers.get(name, 0)
                )
                unit, end = "bytes", match.end() + storage.end()
                result.append(Quantity(start, end, value, unit))
                continue
            duration = re.match(
                r"\s*(milliseconds?\b|ms\b|毫秒|seconds?\b|秒|minutes?\b|分钟|hours?\b|小时)", suffix, re.I
            )
            if duration:
                name = duration[1].lower()
                factor = (
                    Decimal("0.001")
                    if name.startswith("millisecond") or name in {"ms", "毫秒"}
                    else (
                        Decimal(60)
                        if name.startswith("minute") or name == "分钟"
                        else (Decimal(3600) if name.startswith("hour") or name == "小时" else Decimal(1))
                    )
                )
                unit, value = "seconds", value * factor
                end += duration.end()
        result.append(Quantity(start, end, value, unit))
    # In ranges such as 90 to 99 percent, the trailing unit applies to both bounds.
    for index in range(len(result) - 1):
        left, right = result[index : index + 2]
        if left.unit == "number" and right.unit not in {"number", "date", "version", "identifier"}:
            if re.fullmatch(r"\s*(?:to|through|[-–—~～至到])\s*", text[left.end : right.start], re.I):
                raw = TOKEN.match(text, right.start)
                if raw and re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?", raw.group()):
                    factor = (
                        right.value / Decimal(raw.group().replace(",", ""))
                        if right.value
                        else quantities("1" + text[raw.end() : right.end])[0].value
                    )
                    result[index] = Quantity(left.start, left.end, left.value * factor, right.unit)
    return result


def equivalent(left: Quantity, right: Quantity) -> bool:
    if left.unit != right.unit:
        return False
    if left.unit == "date":
        return left.value == right.value or (
            len(str(left.value)) == 5 and str(right.value).endswith(str(left.value))
        )
    return left.value == right.value


def validate_quantities(text: str, evidence_texts: list[str]) -> None:
    # Retain the existing Arabic-money contract; Chinese scale units are allowed.
    for match in re.finditer(r"[零一二两三四五六七八九十百千万亿点]+(?:美元|欧元|人民币)", text):
        if match.start() == 0 or not text[match.start() - 1].isdigit():
            raise ValueError("Monetary amounts must use Arabic digits")
    available = [q for source in evidence_texts for q in quantities(source)]
    for claim in quantities(text):
        if not any(equivalent(claim, source) for source in available):
            if claim.unit in {"USD", "EUR", "GBP", "CNY"}:
                if not any(source.unit == claim.unit for source in available):
                    raise ValueError("Unsupported monetary currency")
                raise ValueError("Unsupported numeric magnitude")
            raise ValueError("Unsupported numeric claim")


def numeric_spans(fields: dict[str, str]) -> dict[str, list[dict]]:
    return {
        name: [
            {"index": i, "text": text[q.start : q.end], "start": q.start, "end": q.end}
            for i, q in enumerate(quantities(text))
        ]
        for name, text in fields.items()
        if quantities(text)
    }


def claim_clause(text: str, index: int) -> str:
    spans = quantities(text)
    if not 0 <= index < len(spans):
        raise ValueError("Unverified numeric anchor")
    q = spans[index]
    before = list(re.finditer(r"[，。；;!?！？]|(?<!\d)\.(?!\d)", text[: q.start]))
    start = before[-1].end() if before else 0
    after = re.search(r"[，。；;!?！？]|(?<!\d)\.(?!\d)", text[q.end :])
    end = q.end + after.start() if after else len(text)
    return text[start:end].strip()


METRICS = {
    "funding": r"\braised?\b|\bfunding\b|\bfinancing\b|融资|募资",
    "valuation": r"\bvaluation\b|\bvalued at\b|估值",
    "revenue": r"\brevenue\b|营收|收入",
    "profit": r"\bprofit\b|利润",
    "harvested_data": r"\bamount of data\b|\bdata harvested\b|\bdata collected\b|数据采集量|采集的数据量|采集量",
    "attack_success": r"\battack success rate\b|攻击成功率|攻击有效率|蒸馏攻击率",
}


def validate_metric_context(output_quote: str, source_quote: str) -> None:
    # Catch common, unambiguous metric substitutions deterministically. Open-ended
    # subjects and other statistical meanings remain mandatory semantic-review work.
    target = {key for key, pattern in METRICS.items() if re.search(pattern, output_quote, re.I)}
    source = {key for key, pattern in METRICS.items() if re.search(pattern, source_quote, re.I)}
    if len(target) > 1 or len(source) > 1:
        raise ValueError("Numeric anchor ambiguous metrics")
    if target and source and target.isdisjoint(source):
        raise ValueError("Numeric anchor metric mismatch")
    for by, to in (
        (
            r"(?:reduc\w*|decreas\w*|lower\w*)\b.{0,50}\bby\b|降低了|减少了",
            r"(?:reduc\w*|decreas\w*|lower\w*)\b.{0,50}\bto\b|降至|降低至|减少至",
        ),
    ):
        if (re.search(by, output_quote, re.I) and re.search(to, source_quote, re.I)) or (
            re.search(to, output_quote, re.I) and re.search(by, source_quote, re.I)
        ):
            raise ValueError("Numeric anchor change basis mismatch")


def validate_anchors(fields: dict[str, str], sources: dict[str, str], anchors) -> None:
    """Require verbatim claim/source anchors for every numeric occurrence.

    An anchor cannot justify a number in another sentence or from another source.
    Equal values across different metrics still require the semantic review's
    explicit approval; local numeric compatibility is never factual approval.
    """
    covered = {field: set() for field in fields}
    for anchor in anchors:
        text = fields.get(anchor.field, "")
        source = sources.get(anchor.evidence_id, "")
        quote = anchor.output_quote
        # Whitespace normalization is lossless for content but tolerates model JSON line wrapping.
        passage = " ".join(anchor.source_quote.split())
        source = " ".join(source.split())
        if not quote or quote not in text or not passage or passage not in source:
            raise ValueError("Unverified numeric anchor")
        validate_metric_context(quote, passage)
        claims, originals = quantities(quote), quantities(passage)
        if not claims or any(not any(equivalent(q, p) for p in originals) for q in claims):
            raise ValueError("Numeric anchor quantity mismatch")
        if any(q.unit not in {"date", "identifier", "version"} for q in claims):

            def context(value, tokens):
                for token in reversed(tokens):
                    value = value[: token.start] + value[token.end :]
                return len(re.findall(r"[a-z\u4e00-\u9fff]", value, re.I))

            if context(quote, claims) < 2 or context(passage, originals) < 2:
                raise ValueError("Numeric anchor missing metric context")
        # A quote covering several claims must preserve each quantity occurrence.
        if any(
            n > Counter((q.unit, q.value) for q in originals)[key]
            for key, n in Counter((q.unit, q.value) for q in claims).items()
            if key[0] != "date"
        ):
            raise ValueError("Numeric anchor multiplicity mismatch")
        for occurrence in re.finditer(re.escape(quote), text):
            covered[anchor.field].update(
                (q.start, q.end)
                for q in quantities(text)
                if occurrence.start() <= q.start and q.end <= occurrence.end()
            )
    for field, text in fields.items():
        if {(q.start, q.end) for q in quantities(text)} - covered[field]:
            raise ValueError("Numeric claim missing semantic audit")


def source_passages(sources: dict[str, str]) -> list[dict[str, str]]:
    """Supply immutable evidence references instead of asking a model to recopy source text."""
    passages = []
    for evidence_id, text in sources.items():
        # Keep decimals and version identifiers intact. 'at a' separates the
        # common funding/valuation sentence into independent metric passages.
        chunks = re.split(r"(?<=[.!?。！？])\s+|[；;]\s*|\s+\*\s+|\s+(?=at an?\s)", text)
        for chunk in chunks:
            chunk = chunk.strip()
            if not quantities(chunk):
                continue
            # Long excerpts are bounded already; a window includes nearby metric
            # context without having a multi-page passage certify a single value.
            spans = quantities(chunk)
            windows = (
                [chunk]
                if len(chunk) <= 600
                else [chunk[max(0, q.start - 160) : min(len(chunk), q.end + 160)] for q in spans]
            )
            for window in dict.fromkeys(windows):
                passages.append(
                    {"passage_id": f"p{len(passages)}", "evidence_id": evidence_id, "text": window}
                )
    return passages
