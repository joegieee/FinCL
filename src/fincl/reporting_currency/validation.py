"""Structural validation of ``REPORTING_CURRENCY_SET`` (§4).

Pure shape and registry checks only — no database. Full structural rejection
(unsupported code, or a tracked currency with no configured direct/inverse
pair at all) happens in :mod:`.queries` preview, which needs the ledger's
tracked-currency inventory; this module only enforces the payload shape and
that the requested currency exists in the checked registry at all.
"""

from __future__ import annotations

from typing import Any

from ..errors import FinCLError
from .models import REPORTING_CURRENCY_SET, is_registered_currency

_REASON_MAX = 2000


def validate_reporting_currency_payload(
    decision_kind: str, anchors: dict[str, str], payload: dict[str, Any]
) -> None:
    if decision_kind != REPORTING_CURRENCY_SET:
        raise FinCLError(f"unknown reporting-currency decision kind {decision_kind!r}")
    if set(anchors) != {"ledger"}:
        raise FinCLError("REPORTING_CURRENCY_SET needs a single 'ledger' anchor")
    if anchors["ledger"] != "default":
        raise FinCLError("REPORTING_CURRENCY_SET anchors the one ledger as 'default'")
    expected = {"currency", "reason"}
    if set(payload) != expected:
        raise FinCLError(f"REPORTING_CURRENCY_SET payload is exactly {sorted(expected)}")
    currency = payload["currency"]
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        raise FinCLError("currency must be a three-letter uppercase code")
    if not is_registered_currency(currency):
        raise FinCLError(
            f"{currency!r} is not in the supported reporting-currency registry"
        )
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _REASON_MAX:
        raise FinCLError("a reason of 1-2,000 characters is required")


__all__ = ["validate_reporting_currency_payload"]
