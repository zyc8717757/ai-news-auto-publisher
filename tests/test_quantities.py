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
        ("Four tests were run.", "进行了三四次测试。"),
    ],
)
def test_digits_cannot_cross_date_currency_scale_or_dimension(source, unsupported):
    with pytest.raises(ValueError):
        validate_quantities(unsupported, [source])


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
    with pytest.raises(ValueError, match="ambiguous metrics"):
        validate_anchors(
            {"summary": "融资75亿美元。"},
            {"e": "Raised $870 million at a $7.5 billion valuation."},
            [anchor("融资75亿美元", "Raised $870 million at a $7.5 billion valuation")],
        )
