from types import SimpleNamespace

import pytest

from ai_news.quantities import validate_anchors, validate_quantities


@pytest.mark.parametrize(
    ("source", "translation"),
    [
        ("Raised $870 million at a $7.5 billion valuation.", "融资8.7亿美元，估值75亿美元。"),
        ("Raised US$1,200,000.", "融资120万美元。"),
        ("Latency is 2 seconds.", "延迟为2000毫秒。"),
        ("Video length from 1 minute to over 10.", "视频时长从1分钟延长至超过10分钟。"),
        ("The error rate fell by 90 percent.", "错误率降低90%。"),
        ("The error rate fell by 90 percent.", "错误率降低百分之90。"),
        ("Data harvested fell by 90 to 99 percent.", "数据采集量降低90%至99%。"),
        ("Two devices run the model.", "使用2台设备运行模型。"),
        ("Four tests showed one improvement.", "四次测试中仅一次显示提升。"),
        ("Eleven tests were run.", "进行了十一次测试。"),
        ("Error rate is 90 percent.", "错误率为九成。"),
        ("checkpoint==4.3.0", "checkpoint 4.3.0版本发布。"),
        ("2026-10-09", "于10月9日发布。"),
        ("Storage is 1 GB.", "存储容量为1000 MB。"),
        ("Memory is 1 GiB.", "内存容量为1024 MiB。"),
        ("Raised US$1.75bn.", "融资17.5亿美元。"),
        ("Raised $5.8 trillion.", "融资5.8万亿美元。"),
        ("Raised $1.2-1.5bn.", "融资12亿至15亿美元。"),
        ("Raised $1 billion to $5.", "融资10亿美元至5美元。"),
        ("Raised $1.2 million to $1.5 billion.", "融资120万美元至15亿美元。"),
        ("The error rate is 10 percent.", "错误率为百分之十。"),
        ("The error rate is 10 percent.", "错误率为十百分比。"),
        ("The error rate rose by 10 percentage points.", "错误率上升十个百分点。"),
        ("Raised CNY 100 million.", "融资1亿元。"),
        ("融资一亿元。", "融资10000万元。"),
        ("Raised CNY 100.", "融资100元。"),
        ("Storage is 1 million bytes.", "存储容量为1000000字节。"),
        ("Storage is 1 million GB.", "存储容量为1000000000000000字节。"),
        ("Revenue was 90 to 99 percent.", "营收为90%至99%。"),
        ("Revenue was 0 to 0 percent.", "营收为0%至0%。"),
        ("Revenue was 5 to 0 percent.", "营收为5%至0%。"),
        ("Latency is 0 seconds to 1.", "延迟为0秒至1秒。"),
    ],
)
def test_exact_translations_and_unit_conversions(source, translation):
    validate_quantities(translation, [source])


@pytest.mark.parametrize(
    ("source", "unsupported"),
    [
        ("2026-10-13T09:28:27Z", "性能提高13%。"),
        ("Raised $870 million.", "融资8.7亿欧元。"),
        ("Valuation $7.5 billion.", "估值7.5亿美元。"),
        ("The error rate fell by 90 percent.", "错误率降低90个百分点。"),
        ("Speed increased 2x.", "速度提高2%。"),
        ("Latency is 2 seconds.", "延迟为2毫秒。"),
        ("Memory is 1 GB.", "内存容量为1 TB。"),
        ("Raised $1 billion to $5.", "融资10亿美元至50亿美元。"),
        ("The error rate is 10 percent.", "错误率为百分之九十。"),
        ("The error rate is 10 percent.", "错误率为十个百分点。"),
        ("Raised USD 100 million.", "融资1亿元。"),
        ("Storage is 1 million GB.", "存储容量为1000000字节。"),
        ("Four tests were run.", "进行了三四次测试。"),
    ],
)
def test_digits_cannot_cross_date_currency_scale_or_dimension(source, unsupported):
    with pytest.raises(ValueError):
        validate_quantities(unsupported, [source])


def test_amount_conversion_preserves_decimal_precision():
    with pytest.raises(ValueError, match="magnitude"):
        validate_quantities(
            "融资3亿美元。",
            ["Raised $0.300000000000000001 billion."],
        )


def test_chinese_numeral_currency_amount_still_requires_arabic_digits():
    with pytest.raises(ValueError, match="Arabic digits"):
        validate_quantities("融资一亿元。", ["Raised CNY 100 million."])


def anchor(output="融资8.7亿美元", source="Raised $870 million", evidence_id="e"):
    return SimpleNamespace(field="summary", output_quote=output, source_quote=source, evidence_id=evidence_id)


def test_semantic_audit_requires_cited_verbatim_metric_passages_and_complete_coverage():
    fields = {"summary": "该公司融资8.7亿美元，估值75亿美元。"}
    sources = {"e": "Raised $870 million at a $7.5 billion valuation."}
    checks = [anchor(), anchor("估值75亿美元", "$7.5 billion valuation")]
    validate_anchors(fields, sources, checks)
    with pytest.raises(ValueError, match="missing semantic audit"):
        validate_anchors(fields, sources, checks[:1])
    with pytest.raises(ValueError, match="Unverified"):
        validate_anchors(fields, sources, [anchor(source="Raised $870 million yesterday")])
    with pytest.raises(ValueError, match="Unverified"):
        validate_anchors(fields, sources, [anchor(evidence_id="other_event")])
    with pytest.raises(ValueError, match="metric context"):
        validate_anchors(fields, sources, [anchor("8.7亿美元", "$870 million")])


def test_same_digit_elsewhere_cannot_supply_an_anchor():
    sources = {"e": "Revenue $870 million. Raised $7.5 billion."}
    with pytest.raises(ValueError, match="quantity mismatch"):
        validate_anchors({"summary": "融资8.7亿美元。"}, sources, [anchor(source="Raised $7.5 billion")])


def test_matching_value_does_not_justify_a_different_metric_or_change_basis():
    with pytest.raises(ValueError, match="metric mismatch"):
        validate_anchors(
            {"summary": "融资8.7亿美元。"},
            {"e": "Revenue $870 million."},
            [anchor(source="Revenue $870 million")],
        )
    with pytest.raises(ValueError, match="metric mismatch"):
        validate_anchors(
            {"summary": "攻击成功率降低90%。"},
            {"e": "The amount of data harvested fell by 90%."},
            [anchor("攻击成功率降低90%", "The amount of data harvested fell by 90%")],
        )
    with pytest.raises(ValueError, match="change basis"):
        validate_anchors(
            {"summary": "错误率降低了90%。"},
            {"e": "Error rate reduced to 90%."},
            [anchor("错误率降低了90%", "Error rate reduced to 90%")],
        )
    mixed_metrics = "Revenue reduced by 90%, profit reduced to 90%."
    validate_anchors(
        {"summary": "营收降低了90%。"},
        {"e": mixed_metrics},
        [anchor("营收降低了90%", mixed_metrics)],
    )
    with pytest.raises(ValueError, match="change basis"):
        validate_anchors(
            {"summary": "营收降至90%。"},
            {"e": mixed_metrics},
            [anchor("营收降至90%", mixed_metrics)],
        )
    with pytest.raises(ValueError, match="metric mismatch"):
        validate_anchors(
            {"summary": "融资75亿美元。"},
            {"e": "Raised $870 million at a $7.5 billion valuation."},
            [anchor("融资75亿美元", "Raised $870 million at a $7.5 billion valuation")],
        )


def test_each_amount_in_a_multi_metric_quote_uses_its_nearest_metric():
    source = "Raised $870 million and a valuation of $7.5 billion."
    fields = {"summary": "该公司融资8.7亿美元，估值75亿美元。"}
    validate_anchors(
        fields,
        {"e": source},
        [
            anchor("融资8.7亿美元", source),
            anchor("估值75亿美元", source),
        ],
    )


def test_equal_amounts_still_require_the_claimed_metric_to_appear_in_evidence():
    source = "Revenue was $870 million and valuation was $870 million."
    with pytest.raises(ValueError, match="metric mismatch"):
        validate_anchors(
            {"summary": "融资8.7亿美元。"},
            {"e": source},
            [anchor("融资8.7亿美元", source)],
        )


def test_duplicate_amount_under_the_claimed_and_another_metric_can_match_by_context():
    source = "Funding was $870 million and revenue was $870 million."
    validate_anchors(
        {"summary": "融资8.7亿美元。"},
        {"e": source},
        [anchor("融资8.7亿美元", source)],
    )


def test_metric_clause_boundary_keeps_thousands_commas_inside_the_amount():
    source = "Revenue was $1,000, profit was $2,000."
    validate_anchors(
        {"summary": "营收为1,000美元。"},
        {"e": source},
        [anchor("营收为1,000美元", source)],
    )


def test_coordinated_metrics_sharing_one_number_remain_ambiguous():
    source = "Revenue and profit were both $870 million."
    with pytest.raises(ValueError, match="ambiguous metrics"):
        validate_anchors(
            {"summary": "营收为8.7亿美元。"},
            {"e": source},
            [anchor("营收为8.7亿美元", source)],
        )
