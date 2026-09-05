from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_SCALE = 9
MAX_ABS_COEFFICIENT = 9_000_000_000_000_000_000
_DECIMAL_RE = re.compile(r"^(?P<sign>[+-]?)(?P<whole>\d+)(?:\.(?P<fraction>\d+))?$")


@dataclass(frozen=True, slots=True)
class ExactMoney:
    """A printed decimal represented without binary floating point."""

    coefficient: int
    scale: int
    # The printed source spelling is retained for provenance only; two values are
    # equal when their numeric (coefficient, scale) pair matches, regardless of
    # how the source formatted them.
    text: str = field(compare=False)

    @classmethod
    def parse(cls, value: str) -> ExactMoney:
        if not isinstance(value, str):
            raise TypeError("money must be parsed from source text")
        match = _DECIMAL_RE.fullmatch(value)
        if match is None:
            raise ValueError(f"invalid plain decimal: {value!r}")
        fraction = match.group("fraction") or ""
        if len(fraction) > MAX_SCALE:
            raise ValueError(f"decimal scale exceeds {MAX_SCALE}: {value!r}")
        digits = match.group("whole") + fraction
        coefficient = int(digits)
        if match.group("sign") == "-":
            coefficient = -coefficient
        if abs(coefficient) > MAX_ABS_COEFFICIENT:
            raise OverflowError("money coefficient exceeds the SQLite integer limit")
        return cls(coefficient=coefficient, scale=len(fraction), text=value)

    def rescaled_coefficient(self, scale: int) -> int:
        if scale < self.scale:
            raise ValueError("lossy rescaling is forbidden")
        coefficient = self.coefficient * (10 ** (scale - self.scale))
        if abs(coefficient) > MAX_ABS_COEFFICIENT:
            raise OverflowError("rescaled money coefficient exceeds the configured limit")
        return coefficient

    def is_zero(self) -> bool:
        return self.coefficient == 0


def compare(left: ExactMoney, right: ExactMoney) -> int:
    """Sign of ``left - right`` (-1 / 0 / 1) by exact integer arithmetic.

    Rescales both to the greater scale so a scale-2 amount is never normalized
    down to a scale-0 band bound (``rescaled_coefficient`` refuses lossy
    rescaling). Used by the categorization amount bands (§2.3).
    """
    scale = max(left.scale, right.scale)
    a, b = left.rescaled_coefficient(scale), right.rescaled_coefficient(scale)
    return (a > b) - (a < b)


def exact_sum(*values: ExactMoney) -> tuple[int, int]:
    if not values:
        return (0, 0)
    scale = max(value.scale for value in values)
    coefficient = sum(value.rescaled_coefficient(scale) for value in values)
    if abs(coefficient) > MAX_ABS_COEFFICIENT:
        raise OverflowError("money sum exceeds the configured limit")
    return coefficient, scale


def compare(left: ExactMoney, right: ExactMoney) -> int:
    """Compare exact decimals after safely aligning to their greatest scale."""
    scale = max(left.scale, right.scale)
    a = left.rescaled_coefficient(scale)
    b = right.rescaled_coefficient(scale)
    return (a > b) - (a < b)


def format_decimal(coefficient: int, scale: int) -> str:
    sign = "-" if coefficient < 0 else ""
    digits = str(abs(coefficient))
    if scale == 0:
        return sign + digits
    digits = digits.rjust(scale + 1, "0")
    return f"{sign}{digits[:-scale]}.{digits[-scale:]}"
