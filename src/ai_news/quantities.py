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
    "元": "CNY",
}
TOKEN = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?"
    r"|(?:\d{4}年)?\d{1,2}月\d{1,2}日"
    r"|(?<![a-z0-9_.])\d+(?:\.\d+){2,}(?![a-z0-9_.])"
    r"|(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*(?![A-Za-z0-9_.-])"
    r"|百分之[零一二两三四五六七八九十百千万亿点]+"
    r"|(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?"
    r"|(?<![\d零一二两三四五六七八九十百千万亿点数几])(?:[零二两三四五六七八九十百千万亿][零一二三四五六七八九十百千万亿]*|一)(?:点[零一二三四五六七八九]+)?(?=次|台|倍|人|秒|毫秒|分钟|小时|成|%|百分比|个百分点|百分点|亿|万|千|元|人民币|美元|欧元|英镑)"
    r"|\b(?:" + "|".join(WORDS) + r")\b",
    re.I,
)


def chinese_number(text: str) -> Decimal:
    digits = dict(zip("零一二两三四五六七八九", (0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    whole, _, fraction = text.partition("点")
    if len(whole) > 1 and not any(char in whole for char in "十百千万亿"):
        # '三四次' is an approximate range, not the scalar four. Do not invent
        # a normalization; let the reviewer rewrite it against explicit evidence.
        raise ValueError("Unsupported numeric claim")
    total = section = number = 0
    for char in whole:
        if char in digits:
            number = digits[char]
        else:
            scale = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 10**8}[char]
            if scale < 10000:
                section += (number or 1) * scale
            else:
                section += number
                total = (total + section) * scale if scale >= 10**8 else total + section * scale
                section = 0
            number = 0
    value = Decimal(total + section + number)
    if fraction:
        value += Decimal("0." + "".join(str(digits[c]) for c in fraction))
    return value


SCALE = re.compile(r"\s*(trillion|billion|million|thousand|万亿|亿|万|千|bn|[kmbt])(?=$|[^a-z])", re.I)


@dataclass(frozen=True)
class Quantity:
    start: int
    end: int
    value: Decimal | str
    unit: str


def quantities(text: str) -> list[Quantity]:
    result = []
    consumed_until = 0
    for match in TOKEN.finditer(text):
        if match.start() < consumed_until:
            continue
        token = match.group()
        start, end = match.span()
        if re.match(r"\d{4}-\d{2}-\d{2}", token):
            result.append(Quantity(start, end, token[:10], "date"))
            consumed_until = end
            continue
        if "月" in token:
            parts = list(map(int, re.findall(r"\d+", token)))
            value = f"{parts[-2]:02}-{parts[-1]:02}"
            if len(parts) == 3:
                value = f"{parts[0]:04}-" + value
            result.append(Quantity(start, end, value, "date"))
            consumed_until = end
            continue
        if re.fullmatch(r"v?\d+(?:\.\d+){2,}", token, re.I):
            result.append(Quantity(start, end, token.lstrip("vV"), "version"))
            consumed_until = end
            continue
        if token.lower() not in WORDS and (re.search(r"[a-z]", token, re.I) or token.count(".") > 1):
            result.append(Quantity(start, end, token.casefold(), "identifier"))
            consumed_until = end
            continue
        chinese_percent = token.startswith("百分之")
        value = (
            chinese_number(token[len("百分之") :]) / 100
            if chinese_percent
            else chinese_number(token)
            if re.search(r"[零一二两三四五六七八九十百千万亿]", token)
            else Decimal(WORDS[token.lower()])
            if token.lower() in WORDS
            else Decimal(token.replace(",", ""))
        )
        unit = "ratio" if chinese_percent else "number"
        scale = SCALE.match(text[end:])
        if scale:
            value *= SCALES[scale[1].lower()]
            end += scale.end()
        prefix, suffix = text[:start], text[end:]
        currency = re.search(r"(US\$|USD|EUR|GBP|CNY|\$|€|£|美元|欧元|英镑|人民币)\s*$", prefix, re.I)
        after = re.match(r"\s*(美元|欧元|英镑|人民币|元|USD\b|EUR\b|GBP\b|CNY\b)", suffix, re.I)
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
        elif re.match(r"\s*(?:%|percent\b|per cent\b|百分比)", suffix, re.I) or prefix.endswith("百分之"):
            unit = "ratio"
            if not chinese_percent:
                value /= 100
            percent = re.match(r"\s*(?:%|percent\b|per cent\b|百分比)", suffix, re.I)
            if percent:
                end += percent.end()
        elif re.match(r"\s*(?:倍|[x×](?![a-z]))", suffix, re.I):
            unit = "multiple"
            end += re.match(r"\s*(?:倍|[x×](?![a-z]))", suffix, re.I).end()
        elif suffix.startswith("成"):
            unit, value, end = "ratio", value / 10, end + 1
        else:
            storage = re.match(r"\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|bytes?\b|字节)", text[end:], re.I)
            if storage:
                name = storage[1].lower()
                powers = {"tb": 12, "gb": 9, "mb": 6, "kb": 3}
                binary = {"tib": 40, "gib": 30, "mib": 20, "kib": 10}
                value *= Decimal(2) ** binary[name] if name in binary else Decimal(10) ** powers.get(name, 0)
                unit, end = "bytes", end + storage.end()
                result.append(Quantity(start, end, value, unit))
                consumed_until = end
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
        consumed_until = end
    # A scale or unit on one range endpoint often applies to the other endpoint.
    range_scales = [_explicit_scale(text, quantity) for quantity in result]
    for index in range(len(result) - 1):
        left, right = result[index : index + 2]
        if left.unit in {"date", "identifier", "version"} or right.unit in {
            "date",
            "identifier",
            "version",
        }:
            continue
        if not RANGE_SEPARATOR.fullmatch(text[left.end : right.start]):
            continue
        if left.unit != right.unit and left.unit != "number" and right.unit != "number":
            continue

        left_scale, right_scale = range_scales[index : index + 2]
        # A rightmost scale can apply to both range endpoints. A scale written
        # only on the left does not: "$1 billion to $5" means $5, not $5bn.
        if right_scale and not left_scale:
            result[index] = Quantity(left.start, left.end, left.value * right_scale, left.unit)
            range_scales[index] = right_scale

        left, right = result[index : index + 2]
        left_raw, right_raw = _range_raw_value(text, left), _range_raw_value(text, right)
        if left_raw is None or right_raw is None:
            continue
        if left.unit == "number" and right.unit != "number":
            if not left_scale and not right_scale:
                left_value = left.value * _range_unit_factor(text, right, right_raw)
            else:
                left_value = left.value
            result[index] = Quantity(left.start, left.end, left_value, right.unit)
        elif left.unit != "number" and right.unit == "number":
            if not left_scale and not right_scale:
                right_value = right.value * _range_unit_factor(text, left, left_raw)
            else:
                right_value = right.value
            result[index + 1] = Quantity(right.start, right.end, right_value, left.unit)
    return result


def equivalent(left: Quantity, right: Quantity) -> bool:
    if left.unit != right.unit:
        return False
    if left.unit == "date":
        return left.value == right.value or (
            len(str(left.value)) == 5 and str(right.value).endswith(str(left.value))
        )
    return left.value == right.value


RANGE_SEPARATOR = re.compile(r"\s*(?:to|through|[-–—~～至到])\s*(?:over|about|超过|约)?\s*", re.I)
CLAUSE_BOUNDARY = re.compile(r"[!?！？。;；，]|,(?!\d)|(?<!\d)\.(?!\d)")
METRIC_COORDINATION = re.compile(r"[\s,，、]*(?:and/or|and|or|以及|与|及|和|或)[\s,，、]*", re.I)


def _clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    before = list(CLAUSE_BOUNDARY.finditer(text, 0, start))
    after = CLAUSE_BOUNDARY.search(text, end)
    return before[-1].end() if before else 0, after.start() if after else len(text)


def _metric_mentions(text: str, start: int, end: int) -> list[tuple[str, int, int]]:
    return [
        (key, match.start(), match.end())
        for key, pattern in METRICS.items()
        for match in re.compile(pattern, re.I).finditer(text, start, end)
    ]


def _metric_context(text: str, quantity: Quantity) -> tuple[set[str], list[tuple[str, int, int]]]:
    """Return the nearest metric label(s) in the quantity's clause.

    Coordinated labels applying to the same number remain ambiguous. A label
    next to one number does not make every other number in the sentence share
    that metric.
    """
    start, end = _clause_bounds(text, quantity.start, quantity.end)
    mentions = _metric_mentions(text, start, end)
    if not mentions:
        return set(), []

    def distance(mention: tuple[str, int, int]) -> int:
        _, left, right = mention
        if right <= quantity.start:
            return quantity.start - right
        if left >= quantity.end:
            return left - quantity.end
        return 0

    nearest_distance = min(distance(mention) for mention in mentions)
    nearest = [mention for mention in mentions if distance(mention) == nearest_distance]
    if len({mention[0] for mention in nearest}) > 1:
        # A metric label immediately followed by its own later amount belongs
        # to that amount, not to the preceding value at an equal distance.
        later_quantities = quantities(text)
        future_labels = {
            mention
            for mention in nearest
            if mention[1] >= quantity.end
            and any(
                other.start >= mention[2]
                and other.start > quantity.start
                and _clause_bounds(text, other.start, other.end) == (start, end)
                for other in later_quantities
            )
        }
        if future_labels:
            remaining = [mention for mention in nearest if mention not in future_labels]
            if remaining:
                nearest = remaining
    keys = {mention[0] for mention in nearest}

    # "Revenue and profit were both $10m" names two metrics for the same
    # amount. Keep that ambiguous even though one label is textually nearer.
    before = [mention for mention in mentions if mention[2] <= quantity.start]
    after = [mention for mention in mentions if mention[1] >= quantity.end]
    for group in (before, after):
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if left[0] == right[0]:
                    continue
                first, second = sorted((left, right), key=lambda mention: mention[1])
                if METRIC_COORDINATION.fullmatch(text[first[2] : second[1]]):
                    keys.update((left[0], right[0]))
    return keys, nearest


def _raw_numeric_value(text: str, quantity: Quantity) -> tuple[Decimal, re.Match[str]] | None:
    if quantity.unit in {"date", "identifier", "version"}:
        return None
    for match in TOKEN.finditer(text, quantity.start, quantity.end):
        token = match.group()
        if token.startswith("百分之"):
            return chinese_number(token[len("百分之") :]), match
        if token.lower() in WORDS:
            return Decimal(WORDS[token.lower()]), match
        if re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?", token):
            return Decimal(token.replace(",", "")), match
        if re.search(r"[零一二两三四五六七八九十百千万亿]", token):
            try:
                return chinese_number(token), match
            except ValueError:
                return None
    return None


def _explicit_scale(text: str, quantity: Quantity) -> Decimal | None:
    raw = _raw_numeric_value(text, quantity)
    if raw is None:
        return None
    _, match = raw
    scale = SCALE.match(text[match.end() :])
    if scale and match.end() + scale.end() <= quantity.end:
        return Decimal(SCALES[scale[1].lower()])
    return None


def _range_raw_value(text: str, quantity: Quantity) -> Decimal | None:
    raw = _raw_numeric_value(text, quantity)
    return raw[0] if raw else None


def _range_unit_factor(text: str, quantity: Quantity, raw_value: Decimal) -> Decimal:
    if raw_value:
        return quantity.value / raw_value
    raw = _raw_numeric_value(text, quantity)
    if raw is None:
        return Decimal(1)
    _, match = raw
    suffix = text[match.end() : quantity.end]
    if quantity.unit == "ratio":
        if raw[1].group().startswith("百分之"):
            return Decimal("0.01")
        if re.match(r"\s*(?:%|percent\b|per cent\b)", suffix, re.I) or text[: match.start()].endswith(
            "百分之"
        ):
            return Decimal("0.01")
        if suffix.startswith("成"):
            return Decimal("0.1")
    if quantity.unit == "seconds":
        duration = re.match(
            r"\s*(milliseconds?\b|ms\b|毫秒|seconds?\b|秒|minutes?\b|分钟|hours?\b|小时)",
            suffix,
            re.I,
        )
        if duration:
            name = duration[1].lower()
            if name.startswith("millisecond") or name in {"ms", "毫秒"}:
                return Decimal("0.001")
            if name.startswith("minute") or name == "分钟":
                return Decimal(60)
            if name.startswith("hour") or name == "小时":
                return Decimal(3600)
    if quantity.unit == "bytes":
        storage = re.match(r"\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|bytes?\b|字节)", suffix, re.I)
        if storage:
            name = storage[1].lower()
            powers = {"tb": 12, "gb": 9, "mb": 6, "kb": 3}
            binary = {"tib": 40, "gib": 30, "mib": 20, "kib": 10}
            return Decimal(2) ** binary[name] if name in binary else Decimal(10) ** powers.get(name, 0)
    return Decimal(1)


def validate_quantities(text: str, evidence_texts: list[str]) -> None:
    # Retain the existing Arabic-money contract; Chinese scale units are allowed.
    for match in re.finditer(r"[零一二两三四五六七八九十百千万亿点]+(?:美元|欧元|人民币|元)", text):
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


def validate_metric_context(
    output_quote: str, source_quote: str, claims: list[Quantity] | None = None
) -> None:
    # Catch common, unambiguous metric substitutions deterministically. Open-ended
    # subjects and other statistical meanings remain mandatory semantic-review work.
    claims = quantities(output_quote) if claims is None else claims
    originals = quantities(source_quote)
    for claim in claims:
        if claim.unit in {"date", "identifier", "version"}:
            continue
        matching = [original for original in originals if equivalent(claim, original)]
        if not matching:
            continue

        target_metrics, _ = _metric_context(output_quote, claim)
        if len(target_metrics) > 1:
            raise ValueError("Numeric anchor ambiguous metrics")
        basis_sources = matching
        if target_metrics:
            target_metric = next(iter(target_metrics))
            source_contexts = [
                (original, _metric_context(source_quote, original)[0]) for original in matching
            ]
            matching_metrics = [
                original for original, metrics in source_contexts if metrics == {target_metric}
            ]
            if matching_metrics:
                basis_sources = matching_metrics
            elif not any(metrics for _, metrics in source_contexts):
                # The source has no recognized metric label, so its local
                # baseline wording remains useful without metric binding.
                basis_sources = matching
            else:
                if any(len(metrics) > 1 for _, metrics in source_contexts):
                    raise ValueError("Numeric anchor ambiguous metrics")
                raise ValueError("Numeric anchor metric mismatch")

        target_basis = _change_basis(output_quote, claim)
        source_bases = [_change_basis(source_quote, original) for original in basis_sources]
        conflicts = [
            basis for basis in source_bases if target_basis and basis and target_basis.isdisjoint(basis)
        ]
        if conflicts and not any(target_basis & basis for basis in source_bases):
            raise ValueError("Numeric anchor change basis mismatch")


def _change_basis(text: str, quantity: Quantity) -> set[str]:
    start, end = _clause_bounds(text, quantity.start, quantity.end)
    markers: list[tuple[str, int, int]] = []
    verbs = re.compile(r"\b(?:reduc\w*|decreas\w*|lower\w*)\b", re.I)
    basis_words = re.compile(r"\b(by|to)\b", re.I)
    for verb in verbs.finditer(text, start, end):
        for word in basis_words.finditer(text, verb.end(), min(end, verb.end() + 50)):
            markers.append((word[1].lower(), word.start(), word.end()))
    for pattern, kind in (
        (r"降低了|减少了", "by"),
        (r"降至|降低至|减少至", "to"),
    ):
        for match in re.compile(pattern).finditer(text, start, end):
            markers.append((kind, match.start(), match.end()))
    if not markers:
        return set()

    def distance(marker: tuple[str, int, int]) -> int:
        _, left, right = marker
        if right <= quantity.start:
            return quantity.start - right
        if left >= quantity.end:
            return left - quantity.end
        return 0

    nearest_distance = min(distance(marker) for marker in markers)
    return {marker[0] for marker in markers if distance(marker) == nearest_distance}


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
        output_index = getattr(anchor, "output_index", None)
        selected = None
        if output_index is not None:
            field_quantities = quantities(text)
            if not 0 <= output_index < len(field_quantities):
                raise ValueError("Unverified numeric anchor")
            selected = field_quantities[output_index]
            occurrence = next(
                (
                    match
                    for match in re.finditer(re.escape(quote), text)
                    if match.start() <= selected.start and selected.end <= match.end()
                ),
                None,
            )
            if occurrence is None:
                raise ValueError("Unverified numeric anchor")
            # The clause supplies semantic context; this anchor certifies only
            # its indexed quantity, not every other number in the same clause.
            claims = [
                Quantity(
                    selected.start - occurrence.start(),
                    selected.end - occurrence.start(),
                    selected.value,
                    selected.unit,
                )
            ]
        else:
            claims = quantities(quote)
        validate_metric_context(quote, passage, claims)
        originals = quantities(passage)
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
        if selected is not None:
            covered[anchor.field].add((selected.start, selected.end))
            continue
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
