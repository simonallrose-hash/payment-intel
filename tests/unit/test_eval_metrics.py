"""Pure metric arithmetic for the gold-set evaluation (FR-QA-02)."""

from __future__ import annotations

from payintel.quality.eval import Counts, EntityTypeReport, EvalReport, _score_sets, gate


def test_counts_math() -> None:
    c = Counts(tp=8, fp=2, fn=4)
    assert c.precision == 0.8
    assert c.recall == 8 / 12
    assert round(c.f1 or 0, 4) == round(2 * 0.8 * (8 / 12) / (0.8 + 8 / 12), 4)
    empty = Counts()
    assert empty.precision is None and empty.recall is None and empty.f1 is None
    assert empty.as_dict()["precision"] is None


def test_score_sets_counts_unlabelled_predictions_as_fp() -> None:
    present = {1: {"stripe", "paypal"}, 2: {"adyen"}, 3: set()}
    predicted = {1: {"stripe", "klarna"}, 2: {"adyen"}, 3: {"mollie"}, 4: {"ignored"}}
    overall, per_entity = _score_sets(present, predicted)
    assert (overall.tp, overall.fp, overall.fn) == (2, 2, 1)
    assert per_entity["klarna"].fp == 1
    assert per_entity["paypal"].fn == 1
    assert "ignored" not in per_entity  # host 4 is not labelled → not scored


def _report(tp: int, fp: int) -> EvalReport:
    etr = EntityTypeReport(labeled_hosts=1, overall=Counts(tp=tp, fp=fp, fn=0), per_entity={})
    empty = EntityTypeReport(labeled_hosts=0, overall=Counts(), per_entity={})
    return EvalReport(
        gold_hosts=1,
        min_confidence="low",
        provider=etr,
        payment_method=empty,
        platform=empty,
        country=empty,
    )


def test_gate() -> None:
    assert gate(_report(19, 1), min_psp_precision=0.95).passed
    assert not gate(_report(18, 2), min_psp_precision=0.95).passed
    assert gate(_report(0, 0), min_psp_precision=0.95).passed  # nothing to measure
    assert "FR-QA-02" in gate(_report(1, 1), min_psp_precision=0.95).reason


def test_targets_compare_measured_values_with_the_nfr_q_settings() -> None:
    """NFR-Q-02…05: informative target checks next to the release gate."""
    from payintel.core.settings import QualitySettings
    from payintel.quality.eval import Counts, EntityTypeReport, EvalReport, TargetCheck, targets

    def etr(tp: int, fp: int, fn: int) -> EntityTypeReport:
        return EntityTypeReport(labeled_hosts=1, overall=Counts(tp, fp, fn), per_entity={})

    report = EvalReport(
        gold_hosts=3,
        min_confidence="low",
        provider=etr(8, 0, 4),
        payment_method=etr(9, 1, 0),
        platform=etr(97, 3, 0),
        country=etr(0, 0, 0),
        provider_checkout=Counts(8, 0, 1),
        checkout_hosts=2,
    )
    checks = {c.code: c for c in targets(report, QualitySettings())}
    assert checks["NFR-Q-02"].value == 8 / 9 and checks["NFR-Q-02"].passed is True
    assert checks["NFR-Q-03"].value == 0.9 and checks["NFR-Q-03"].passed is True
    assert checks["NFR-Q-04"].value == 0.97 and checks["NFR-Q-04"].passed is True
    assert checks["NFR-Q-05"].value is None and checks["NFR-Q-05"].passed is None
    assert "[ok]" in checks["NFR-Q-03"].line() and "n/a" in checks["NFR-Q-05"].line()
    strict = targets(report, QualitySettings(target_psp_recall=0.95))
    assert strict[0].passed is False and "[WARN]" in strict[0].line()
    assert TargetCheck("x", "m", 0.5, 0.6).as_dict()["passed"] is False
    assert report.as_dict()["provider_checkout"]["recall"] == 8 / 9
