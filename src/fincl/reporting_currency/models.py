"""Currency registry and the journalled ``REPORTING_CURRENCY_SET`` vocabulary.

This is the M1 slice of the reporting-currency plan (§3.2, §4): a small checked
registry of supported currencies, the decision kind that journals an operator's
choice of reporting currency, and the ``direct-or-inverse-v1`` rate path policy
constant. The orthogonal valuation model (``ValuationPurpose``, ``RateBasis``,
exact arithmetic) is M2 and does not live here.
"""

from __future__ import annotations

from dataclasses import dataclass

REPORTING_CURRENCY_SET = "REPORTING_CURRENCY_SET"

REPORTING_CURRENCY_DECISION_KINDS = frozenset({REPORTING_CURRENCY_SET})

# ``direct-or-inverse-v1`` (§3.2): identity, then a directly imported
# native -> reporting rate, then the exact reciprocal of a directly imported
# reporting -> native rate, otherwise unavailable. No triangulation in v1.
RATE_PATH_POLICY_VERSION = "direct-or-inverse-v1"

DEFAULT_REPORTING_CURRENCY = "SGD"


@dataclass(frozen=True, slots=True)
class CurrencyDefinition:
    code: str
    display_name: str
    minor_unit_scale: int


# The checked currency registry (§3.2): "three letters alone do not establish a
# valid currency or its scale." Includes every currency the MAS loader can
# import (fx.py's `_MAS_CURRENCY_NAMES`) plus SGD itself and two
# non-two-decimal currencies (JPY scale 0, KWD scale 3) so quantization is
# never allowed to assume "round to cents" is universal.
CURRENCY_REGISTRY: dict[str, CurrencyDefinition] = {
    definition.code: definition
    for definition in (
        CurrencyDefinition("SGD", "Singapore Dollar", 2),
        CurrencyDefinition("USD", "US Dollar", 2),
        CurrencyDefinition("EUR", "Euro", 2),
        CurrencyDefinition("GBP", "Pound Sterling", 2),
        CurrencyDefinition("AUD", "Australian Dollar", 2),
        CurrencyDefinition("CAD", "Canadian Dollar", 2),
        CurrencyDefinition("CHF", "Swiss Franc", 2),
        CurrencyDefinition("NZD", "New Zealand Dollar", 2),
        CurrencyDefinition("MYR", "Malaysian Ringgit", 2),
        CurrencyDefinition("HKD", "Hong Kong Dollar", 2),
        CurrencyDefinition("CNY", "Chinese Renminbi", 2),
        CurrencyDefinition("THB", "Thai Baht", 2),
        CurrencyDefinition("JPY", "Japanese Yen", 0),
        CurrencyDefinition("KWD", "Kuwaiti Dinar", 3),
    )
}


def is_registered_currency(code: str) -> bool:
    return code in CURRENCY_REGISTRY


def currency_definition(code: str) -> CurrencyDefinition | None:
    return CURRENCY_REGISTRY.get(code)


__all__ = [
    "CURRENCY_REGISTRY",
    "DEFAULT_REPORTING_CURRENCY",
    "RATE_PATH_POLICY_VERSION",
    "REPORTING_CURRENCY_DECISION_KINDS",
    "REPORTING_CURRENCY_SET",
    "CurrencyDefinition",
    "currency_definition",
    "is_registered_currency",
]
