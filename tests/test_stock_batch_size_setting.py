"""The BIMS stock batch size is tenant-tunable.

BIMS answers differently depending on how many SKUs one request carries:
asked for SKU 112743 alone it reports an unfiltered total of 20, and asked
inside a 200-SKU batch it reports 0 for the same SKU in the same pull
(measured 2026-11-23). Until that is fixed upstream, the batch size has to be
adjustable per tenant without a deploy so the size can be tuned against a
live catalog.
"""
from __future__ import annotations

from bims_shopify.adapters.bims.stock import (
    STOCK_BATCH_SIZE,
    STOCK_BATCH_SIZE_KEY,
    resolve_stock_batch_size,
)


def test_the_default_is_unchanged_when_the_tenant_says_nothing():
    assert resolve_stock_batch_size({}) == STOCK_BATCH_SIZE


def test_a_tenant_can_lower_it():
    assert resolve_stock_batch_size({STOCK_BATCH_SIZE_KEY: 50}) == 50


def test_a_string_value_is_accepted_since_field_mappings_is_free_form():
    assert resolve_stock_batch_size({STOCK_BATCH_SIZE_KEY: "25"}) == 25


def test_a_nonsense_value_falls_back_to_the_default_instead_of_breaking_the_pull():
    # field_mappings is operator-edited free-form JSON; a typo must not stop
    # inventory from syncing.
    assert resolve_stock_batch_size({STOCK_BATCH_SIZE_KEY: "abc"}) == STOCK_BATCH_SIZE
    assert resolve_stock_batch_size({STOCK_BATCH_SIZE_KEY: 0}) == STOCK_BATCH_SIZE
    assert resolve_stock_batch_size({STOCK_BATCH_SIZE_KEY: -5}) == STOCK_BATCH_SIZE


def test_it_is_capped_so_one_request_cannot_grow_unbounded():
    assert resolve_stock_batch_size({STOCK_BATCH_SIZE_KEY: 100000}) == STOCK_BATCH_SIZE
