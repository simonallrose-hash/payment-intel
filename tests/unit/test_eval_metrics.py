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
