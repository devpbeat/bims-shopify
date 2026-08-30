"""CakePHP 1.3-style form-urlencoded key encoder used by most legacy BIMS endpoints.

BIMS legacy write endpoints expect nested data using flat keys such as
``data[Model][field]`` for scalar fields and ``data[Model][0][field]`` for
list/line-item entries. This module converts a nested Python dict/list
structure into that flat key -> value mapping, suitable for
``httpx``'s ``data=`` parameter (application/x-www-form-urlencoded).
"""
from __future__ import annotations

from typing import Any


def encode_cakephp_form(payload: dict[str, Any], root_key: str = "data") -> dict[str, str]:
    """Flatten a nested dict/list payload into CakePHP-style form field names.

    Example::

        encode_cakephp_form({"Sale": {"id": 1, "amount": 10.5}})
        # -> {"data[Sale][id]": "1", "data[Sale][amount]": "10.5"}

        encode_cakephp_form({"SalesProduct": [{"product_id": 1, "quantity": 2}]})
        # -> {"data[SalesProduct][0][product_id]": "1", "data[SalesProduct][0][quantity]": "2"}
    """
    flat: dict[str, str] = {}
    _flatten(payload, [root_key], flat)
    return flat


def _flatten(value: Any, path: list[str], out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            _flatten(sub, [*path, str(key)], out)
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            _flatten(sub, [*path, str(index)], out)
    elif value is None:
        return
    else:
        out[_build_key(path)] = _stringify(value)


def _build_key(path: list[str]) -> str:
    head, *rest = path
    return head + "".join(f"[{segment}]" for segment in rest)


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
