"""Journal-owned monthly budgets and manually recorded savings goals."""
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import FinCLError


def validate_plan(anchors: dict[str, str], payload: dict[str, Any]) -> None:
    if not anchors.get("plan") or len(anchors["plan"]) > 100:
        raise FinCLError("A plan identifier is required")
    if set(payload) != {"type", "name", "currency", "target", "saved", "category", "reason"}:
        raise FinCLError("Invalid plan fields")
    if payload["type"] not in {"budget", "goal"}:
        raise FinCLError("Choose budget or goal")
    if not isinstance(payload["name"], str) or not 1 <= len(payload["name"].strip()) <= 80:
        raise FinCLError("Name must contain 1–80 characters")
    if not isinstance(payload["currency"], str) or len(payload["currency"]) != 3 or not payload["currency"].isalpha() or not payload["currency"].isupper():
        raise FinCLError("Choose a three-letter currency")
    for field in ("target", "saved"):
        try:
            value = Decimal(payload[field])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FinCLError("Enter an exact positive amount") from exc
        if not isinstance(payload[field], str) or not value.is_finite() or value < 0 or (field == "target" and value == 0) or value > Decimal("999999999999999.99") or (isinstance(value.as_tuple().exponent, int) and int(value.as_tuple().exponent) < -2):
            raise FinCLError("Amounts must be positive decimal strings with at most two decimal places")
    if not isinstance(payload["category"], str) or not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise FinCLError("A category and reason are required")
