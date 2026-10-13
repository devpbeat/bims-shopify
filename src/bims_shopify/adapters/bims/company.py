"""Company-scoping helpers for raw BIMS product rows.

BIMS ignores the ``company_id`` query param on ``products/index.json`` (a
pull for company 6 returned only company 1 rows), so the company has to be
enforced client-side. BIMS sends ``company_id`` as a string while the tenant
config holds an int, hence every comparison here is done on strings.
"""
from __future__ import annotations

from typing import Any

#: ``rows_by_company`` key for rows that carry no ``company_id``.
MISSING_COMPANY_KEY = "(missing company_id)"


def is_other_company(raw: dict[str, Any], company_id: int | str | None) -> bool:
    """True when the row carries a ``company_id`` different from ``company_id``.

    ``company_id=None`` disables the gate. A row without a ``company_id``
    cannot be proven foreign, so it is never "other".
    """
    row_company = raw.get("company_id")
    if company_id is None or row_company is None or str(row_company) == "":
        return False
    return str(row_company) != str(company_id)


def count_rows_by_company(raw_rows: list[dict[str, Any]]) -> dict[str, int]:
    """Row count per ``company_id`` over the RAW (unfiltered) pull.

    Largest company first. Rows with a missing/None/empty ``company_id`` are
    counted under ``MISSING_COMPANY_KEY`` instead of being discarded.
    """
    counts: dict[str, int] = {}
    for raw in raw_rows:
        value = raw.get("company_id")
        key = MISSING_COMPANY_KEY if value is None or str(value) == "" else str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
