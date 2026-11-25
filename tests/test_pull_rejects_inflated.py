"""A pull that GROWS implausibly must be rejected too, not only one that shrinks.

Measured 2026-11-24: BIMS intermittently answers 33,705 rows where every
other pull that day answered 13,196 -- 2.55x. The sanity check only looked
for drops, so the inflated pull was always accepted, became the reference,
and then every correct 13,196-row pull looked like a 61% drop and was
rejected. It happened four times in one day and froze the store for four
days the first time.

The asymmetry was the bug: bad data always won because nobody checked it on
the way IN.
"""
from __future__ import annotations

from types import SimpleNamespace

from bims_shopify.application.pull_snapshot import (
    REJECT_TOO_FEW_ROWS,
    REJECT_TOO_MANY_ROWS,
    check_sanity,
)


def _tenant(**kw):
    base = dict(
        pull_max_drop_ratio=0.3,
        pull_max_rise_ratio=0.5,
        field_mappings={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _pull(rows: int, stock: int | None = None):
    stock = rows if stock is None else stock
    return SimpleNamespace(
        rows=[object()] * rows,
        stock={f"S{i}": 1.0 for i in range(stock)},
        complete=None,
        reported_count=None,
        stop_reason="empty_page",
        warnings=[],
    )


def _previous(rows: int, stock: int | None = None):
    return SimpleNamespace(id=1, rows_pulled=rows, stock_rows=rows if stock is None else stock)


def test_the_real_inflated_pull_is_rejected():
    verdict = check_sanity(_tenant(), _pull(33705), _previous(13196))
    assert verdict is not None
    assert verdict[0] == REJECT_TOO_MANY_ROWS


def test_an_ordinary_days_growth_is_accepted():
    # New products arrive every day; that is a few percent, not 155%.
    assert check_sanity(_tenant(), _pull(13400), _previous(13196)) is None


def test_the_drop_check_still_works():
    verdict = check_sanity(_tenant(), _pull(5000), _previous(13196))
    assert verdict is not None
    assert verdict[0] == REJECT_TOO_FEW_ROWS


def test_the_first_pull_has_nothing_to_compare_against():
    assert check_sanity(_tenant(), _pull(33705), None) is None


def test_the_rise_limit_is_tenant_configurable():
    # A tenant genuinely loading a big catalogue can allow the jump.
    assert check_sanity(_tenant(pull_max_rise_ratio=5.0), _pull(33705), _previous(13196)) is None
