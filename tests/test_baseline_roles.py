"""Targeted Stage 1 role-default and reserve-read tests."""

from fincl.baseline.roles import ROLE_RULE_VERSION, _role


def test_builtin_role_rules_keep_dimensions_separate() -> None:
    rent = _role("Housing / Rent", recurring=False)
    subscription = _role("Leisure / Entertainment", recurring=True)

    assert rent == {
        "cost_pattern": "variable_routine",
        "essential_status": "essential",
        "tier": 1,
        "basis": ROLE_RULE_VERSION,
        "review_status": "automatic",
    }
    assert subscription == {
        "cost_pattern": "fixed_recurring",
        "essential_status": "discretionary",
        "tier": 4,
        "basis": "confirmed recurring occurrence",
        "review_status": "confirmed",
    }


def test_unmapped_category_stays_unresolved() -> None:
    assert _role("Unclassified / Unclassified", recurring=False) == {
        "cost_pattern": "unresolved",
        "essential_status": "unresolved",
        "tier": None,
        "basis": "unmapped or unclassified category",
        "review_status": "unresolved",
    }
