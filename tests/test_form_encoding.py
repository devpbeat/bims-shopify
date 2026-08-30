"""Tests for the CakePHP data[Model][field] form encoder."""
from bims_shopify.adapters.bims.form_encoding import encode_cakephp_form


def test_encodes_scalar_fields_under_model():
    result = encode_cakephp_form({"Sale": {"id": 1, "amount": 10.5}})
    assert result == {"data[Sale][id]": "1", "data[Sale][amount]": "10.5"}


def test_encodes_list_of_line_items_with_index():
    result = encode_cakephp_form(
        {"SalesProduct": [{"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 3}]}
    )
    assert result == {
        "data[SalesProduct][0][product_id]": "1",
        "data[SalesProduct][0][quantity]": "2",
        "data[SalesProduct][1][product_id]": "2",
        "data[SalesProduct][1][quantity]": "3",
    }


def test_encodes_booleans_as_lowercase_strings():
    result = encode_cakephp_form({"Sale": {"billed": True, "void": False}})
    assert result == {"data[Sale][billed]": "true", "data[Sale][void]": "false"}


def test_none_values_are_skipped():
    result = encode_cakephp_form({"Sale": {"id": 1, "invoice_number": None}})
    assert result == {"data[Sale][id]": "1"}


def test_custom_root_key():
    result = encode_cakephp_form({"Contact": {"name": "x"}}, root_key="Contact")
    assert result == {"Contact[Contact][name]": "x"}
