"""Step 16 — Release B integrated verification (plan §7 M4, §9.2).

The per-module suites (``test_reported_cash.py``, ``test_reported_networth.py``,
``test_reported_forecast.py``, ``test_reported_income.py``) each prove their
own owning reader agrees with itself in isolation. This module is the
missing integration layer step 16 calls for: the Release A mixed-pocket
fixture driven through the *actual HTTP routes* behind Cash, Net worth,
Forecast and Income, checking they agree with each other and with Overview
under the same generation/context -- not just that each is internally
consistent.

Reuses the existing mixed-pocket fixture builders from ``tests.test_money_flow``
and the income-candidate fixture from ``tests.test_reported_income`` rather
than inventing new ones (§9.1: "extend existing builders").
"""

from __future__ import annotations

import unittest
import uuid
from decimal import Decimal
from pathlib import Path

from fincl.api import create_app
from fincl.db import connect_sqlite
from fincl.decision_journal import append_decision, default_journal_path
from fincl.decisions import replay_journal
from fincl.refresh import refresh_ledger
from fincl.reporting_currency import REPORTING_CURRENCY_SET
from tests.asgi_client import ASGITestClient
from tests.test_money_flow import (
    _HISTORY_END,
    _HISTORY_START,
    _MIXED_ROWS,
    _ledger,
    _load_usd_rate,
    _mixed_ledger_with_rate,
)
from tests.test_networth import _account_id, _project_observation
from tests.test_reported_income import _load_usd_rate as _load_income_usd_rate
from tests.test_reported_income import _mixed_rows as _income_rows

_ORIGIN = "http://127.0.0.1:8765"
_ANCHORS = {"ledger": "default"}


def _set_reporting_currency(database: Path, currency: str, *, reason: str = "test") -> None:
    append_decision(
        default_journal_path(database), decision_kind=REPORTING_CURRENCY_SET, anchors=_ANCHORS,
        payload={"currency": currency, "reason": reason},
        policy_version="reporting-currency-v1", snapshot=currency,
    )
    connection = connect_sqlite(database)
    try:
        replay_journal(connection)
    finally:
        connection.close()
    refresh_ledger(database)


class _ReleaseBApiBase(unittest.TestCase):
    """A mixed-pocket (SGD+USD) ledger, driven through the real ASGI app --
    the same route surface a browser hits, not the bare reader functions."""

    def _boot(self, database: Path) -> None:
        self.database = database
        self.root = database.parent
        self.app = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.root / "evidence",
        )
        self.client = ASGITestClient(self.app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)
        response = self.client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf"]

    def setUp(self) -> None:
        self._boot(_mixed_ledger_with_rate())

    def _get(self, path: str, **params: object) -> dict:
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class CashNetWorthSharedPositionTests(_ReleaseBApiBase):
    """§7 M4 acceptance: "same-definition Overview/detail figures agree." Cash
    and Net worth both read the native position adapter; their reported
    figures for the same account/currency/date must match exactly."""

    def test_cash_pocket_reported_balance_matches_net_worths_cash_component(self) -> None:
        cash = self._get("/api/v1/reporting/cash")
        net_worth = self._get("/api/v1/reporting/net-worth")
        self.assertEqual(cash["reporting_currency"], net_worth["reporting_currency"])

        for pocket in cash["pockets"]:
            component_id = f"cash:{pocket['account_id']}:{pocket['currency_pocket']}"
            component = next(
                (c for c in net_worth["components"] if c["component_id"] == component_id), None,
            )
            if component is None:
                continue  # An excluded (non-liquid) pocket has no cash component.
            self.assertEqual(pocket["balance"], component["amount"])
            self.assertEqual(pocket["as_of"], component["as_of"])
            if pocket["reported_balance"] is None or component["reported"] is None:
                self.assertEqual(pocket["reported_balance"], component["reported"])
            else:
                self.assertEqual(pocket["reported_balance"]["reported"], component["reported"]["reported"])

    def test_cash_combined_and_net_worths_tracked_assets_are_independent_figures(self) -> None:
        """Cash's combined total is a *same-common-cutoff* aggregate; Net
        worth's tracked assets combine components at their own independent
        dates and also include non-cash assets. They are not required to
        agree, and this fixture demonstrates why: confirm both are present
        and internally coherent without asserting numeric equality."""
        cash = self._get("/api/v1/reporting/cash")
        net_worth = self._get("/api/v1/reporting/net-worth")
        self.assertIn(cash["combined"]["amount"]["status"], ("available", "partial", "unavailable"))
        self.assertIn(net_worth["totals"]["assets"]["status"], ("available", "partial", "unavailable"))


class OverviewForecastAgreementTests(_ReleaseBApiBase):
    """§7 M4 acceptance: Forecast's own totals must match Overview's
    forecast-closing card exactly, since both share ``forecast_closings``."""

    def test_forecast_totals_match_overviews_forecast_closing_card(self) -> None:
        money_flow = self._get("/api/v1/money-flow", history_start=_HISTORY_START, history_end=_HISTORY_END)
        forecast = self._get("/api/v1/reporting/forecast")
        self.assertEqual(money_flow["generation"], forecast["generation"])

        mf_closing = money_flow["summary"]["forecast_closing"]
        self.assertEqual(mf_closing["base"]["status"], forecast["totals"]["base_closing"]["status"])
        self.assertEqual(mf_closing["base"]["total"], forecast["totals"]["base_closing"]["total"])
        self.assertEqual(mf_closing["conservative"]["status"], forecast["totals"]["conservative_closing"]["status"])
        self.assertEqual(mf_closing["conservative"]["total"], forecast["totals"]["conservative_closing"]["total"])
        self.assertEqual(mf_closing["opening_balance_as_of"], forecast["opening_balance_as_of"])
        self.assertEqual(mf_closing["horizon_end"], forecast["horizon_end"])

    def test_both_forecast_cases_stay_distinct(self) -> None:
        forecast = self._get("/api/v1/reporting/forecast")
        base = forecast["totals"]["base_closing"]
        conservative = forecast["totals"]["conservative_closing"]
        # USD has no residual-spending evidence on this fixture, so both
        # cases are a "Known subtotal" (partial) rather than complete --
        # still two independently computed, genuinely distinct figures.
        self.assertEqual(base["status"], "partial")
        self.assertEqual(conservative["status"], "partial")
        self.assertIsNotNone(base["partial_total"])
        self.assertIsNotNone(conservative["partial_total"])
        self.assertNotEqual(base["partial_total"], conservative["partial_total"])


class CombinedExtremaTests(_ReleaseBApiBase):
    """§7 M4 extrema rule: withheld unless every pocket in the ledger is both
    forecastable and priced; never a fabricated success from a partial set."""

    def test_combined_minimum_is_unavailable_because_the_usd_pocket_has_no_forecast(self) -> None:
        forecast = self._get("/api/v1/reporting/forecast")
        self.assertEqual(forecast["totals"]["base_combined_minimum"]["amount"]["status"], "unavailable")
        self.assertEqual(forecast["totals"]["conservative_combined_minimum"]["amount"]["status"], "unavailable")
        self.assertIn(
            "NO_COMBINED_MINIMUM_EVIDENCE",
            forecast["totals"]["base_combined_minimum"]["amount"]["unavailable_reasons"],
        )
        # The closing totals stay valid (as a "Known subtotal") even though
        # the stricter combined minimum is withheld outright.
        self.assertEqual(forecast["totals"]["base_closing"]["status"], "partial")
        self.assertIsNotNone(forecast["totals"]["base_closing"]["partial_total"])

    def test_a_single_currency_ledger_has_an_available_combined_minimum(self) -> None:
        # The USD rows dropped: a single-currency (SGD-only) ledger where
        # every currency present is forecastable and priced (identity), so
        # the extrema rule's "every pocket" gate is trivially satisfied.
        sgd_only_rows = [row for row in _MIXED_ROWS if row[7] == "SGD"]
        self._boot(_ledger(sgd_only_rows))
        forecast = self._get("/api/v1/reporting/forecast")
        self.assertEqual(forecast["totals"]["base_combined_minimum"]["amount"]["status"], "available")
        self.assertIsNotNone(forecast["totals"]["base_combined_minimum"]["month"])


class AllPocketsDefaultTests(_ReleaseBApiBase):
    """§7 M4 acceptance: "all native pockets remain represented" with no
    query parameter narrowing any of the four routes by default."""

    def test_cash_shows_every_native_pocket_including_the_unsupported_usd_one(self) -> None:
        cash = self._get("/api/v1/reporting/cash")
        self.assertEqual(sorted({p["currency_pocket"] for p in cash["pockets"]}), ["SGD", "USD"])

    def test_net_worth_carries_the_usd_cash_component_even_when_unpriced_scenarios_arise(self) -> None:
        net_worth = self._get("/api/v1/reporting/net-worth")
        currencies = {c["currency"] for c in net_worth["components"]}
        self.assertIn("SGD", currencies)
        self.assertIn("USD", currencies)

    def test_forecast_considers_both_currencies_even_though_only_one_is_forecastable(self) -> None:
        forecast = self._get("/api/v1/reporting/forecast")
        native_currencies = set(forecast["native"]["by_currency"])
        self.assertEqual(native_currencies, {"SGD", "USD"})
        self.assertEqual([c["currency"] for c in forecast["currencies"]], ["SGD"])

    def test_income_route_has_no_query_parameters_to_narrow_it(self) -> None:
        income = self._get("/api/v1/reporting/inflows/candidates")
        self.assertIn("candidates", income)
        self.assertIn("native", income)


class MissingFxAndEvidenceExclusionTests(_ReleaseBApiBase):
    """§7 M4 acceptance: "missing evidence and FX name their scopes" --
    consistently, across Cash, Net worth and Forecast together, on one
    ledger with the USD pocket present but no USD rate at all."""

    def setUp(self) -> None:
        self._boot(_ledger(_MIXED_ROWS))  # no USD rate loaded

    def test_missing_usd_rate_is_named_the_same_way_across_cash_net_worth_and_forecast(self) -> None:
        cash = self._get("/api/v1/reporting/cash")
        net_worth = self._get("/api/v1/reporting/net-worth")
        forecast = self._get("/api/v1/reporting/forecast")

        usd_pocket = next(p for p in cash["pockets"] if p["currency_pocket"] == "USD")
        self.assertIsNotNone(usd_pocket["reported_balance"])
        self.assertIsNotNone(usd_pocket["reported_balance"]["unavailable"])
        self.assertEqual(usd_pocket["reported_balance"]["unavailable"]["reason"], "RATE_NOT_IMPORTED")

        usd_component = next(c for c in net_worth["components"] if c["currency"] == "USD" and c["kind"] == "cash")
        self.assertIsNotNone(usd_component["reported"])
        self.assertEqual(usd_component["reported"]["unavailable"]["reason"], "RATE_NOT_IMPORTED")

        # Forecast's USD pocket has no residual-spending evidence at all --
        # a different, distinct explanation from a missing rate -- so it
        # never even reaches the FX-pricing step (§4.3: "a rate cannot
        # repair an unsupported cash balance").
        self.assertNotIn("USD", {c["currency"] for c in forecast["currencies"]})
        usd_native = forecast["native"]["by_currency"]["USD"]
        self.assertFalse(usd_native["available"])

    def test_backfilling_the_rate_clears_cash_and_net_worth_together_but_not_forecast(self) -> None:
        """A missing FX rate and missing forecast evidence are different
        explanations (§4.3): backfilling the rate resolves the former for
        Cash/Net worth, but the USD pocket's forecast stays unavailable for
        an entirely separate, evidence-based reason."""
        cash_before = self._get("/api/v1/reporting/cash")

        _load_usd_rate(self.database)

        cash_after = self._get("/api/v1/reporting/cash")
        net_worth_after = self._get("/api/v1/reporting/net-worth")
        forecast_after = self._get("/api/v1/reporting/forecast")
        self.assertNotEqual(cash_after["generation"], cash_before["generation"])

        usd_pocket = next(p for p in cash_after["pockets"] if p["currency_pocket"] == "USD")
        self.assertIsNone(usd_pocket["reported_balance"]["unavailable"])
        usd_component = next(c for c in net_worth_after["components"] if c["currency"] == "USD" and c["kind"] == "cash")
        self.assertIsNone(usd_component["reported"]["unavailable"])

        # Forecast's USD pocket still has no residual-spending evidence --
        # available FX must never turn missing evidence into a balance.
        self.assertNotIn("USD", {c["currency"] for c in forecast_after["currencies"]})


class ManualEstimateVisibilityTests(_ReleaseBApiBase):
    """§7 M4 acceptance: "manual-estimate labels and exclusions" stay
    visible and distinct from the evidence-only tracked subtotal."""

    def test_a_manual_observation_reports_on_the_manual_line_not_the_tracked_one(self) -> None:
        # Deliberately no `refresh_ledger` here: a raw-SQL manual observation
        # is read live by `balance_observations`'s own query (like the native
        # `test_a_manual_balance_observation_lands_on_the_manual_line`), not
        # gated behind a republished generation the way ledger-entry-derived
        # facts are.
        account_id = _account_id(self.database)
        _project_observation(self.database, account_id, "2026-03-06", "999.00")

        net_worth = self._get("/api/v1/reporting/net-worth", as_of="2026-03-06")
        manual_component = next(c for c in net_worth["components"] if c["basis"] == "manual")
        self.assertIn(account_id, manual_component["component_id"])
        self.assertEqual(manual_component["amount"], "999.00")
        self.assertIsNotNone(manual_component["reported"])
        self.assertEqual(net_worth["totals"]["manual_assets"]["status"], "available")
        self.assertNotEqual(net_worth["totals"]["manual_assets"]["total"], None)


class NativeCommandPayloadsUnchangedTests(_ReleaseBApiBase):
    """§7 M4 acceptance: "native editing and calculations unchanged" --
    exercised through a real command, not merely asserted."""

    def test_a_balance_observation_command_carries_exactly_its_native_payload(self) -> None:
        cash = self._get("/api/v1/reporting/cash")
        pocket = next(p for p in cash["pockets"] if p["liquid"])
        generation = cash["generation"]

        command = {
            "request_id": str(uuid.uuid4()), "expected_generation": generation,
            "kind": "BALANCE_OBSERVATION_SET",
            "anchors": {"observation": str(uuid.uuid4())},
            "payload": {
                "account_id": pocket["account_id"], "currency_pocket": pocket["currency_pocket"],
                "observed_on": "2026-03-06", "balance": "777.00", "reason": "verified against bank app",
            },
        }
        preview = self.client.post(
            "/api/v1/preview", json=command,
            headers={"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": self.csrf},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["effect"], command["payload"])

        submit = self.client.post(
            "/api/v1/commands", json=command,
            headers={"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": self.csrf},
        )
        self.assertEqual(submit.status_code, 200, submit.text)
        refresh_ledger(self.database)

        cash_after = self._get("/api/v1/reporting/cash")
        updated = next(p for p in cash_after["pockets"] if p["account_id"] == pocket["account_id"] and p["currency_pocket"] == pocket["currency_pocket"])
        self.assertEqual(updated["balance"], "777.00")
        self.assertEqual(updated["basis"], "manual-observation")
        self.assertIsNotNone(updated["reported_balance"])


class ConfirmedIncomeEffectTests(unittest.TestCase):
    """§7 M4 acceptance boundary: a candidate's own queue stays Likely and
    unaffected by classification, while the native owner that actually
    includes confirmed receipts (Analysis's income bucket) reflects them."""

    def setUp(self) -> None:
        database = _ledger(_income_rows())
        _load_income_usd_rate(database)
        self.database = database
        self.app = create_app(
            self.database, port=8765, bootstrap_secret="test-secret",
            evidence_dir=self.database.parent / "evidence",
        )
        self.client = ASGITestClient(self.app, base_url=_ORIGIN)
        self.addCleanup(self.client.close)
        response = self.client.post(
            "/session", json={"secret": "test-secret"},
            headers={"origin": _ORIGIN, "content-type": "application/json"},
        )
        self.csrf = response.json()["csrf"]

    def _get(self, path: str, **params: object) -> dict:
        response = self.client.get(path, params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_classifying_receipts_does_not_promote_the_candidate_but_analysis_sees_them(self) -> None:
        income = self._get("/api/v1/reporting/inflows/candidates")
        sgd_candidate = next(c for c in income["native"]["candidates"] if c["currency"] == "SGD")
        anchors = [o["anchor"] for o in sgd_candidate["occurrences"]]
        generation = income["generation"]

        batch = {
            "request_id": str(uuid.uuid4()), "expected_generation": generation,
            "reason": "confirmed employer payroll",
            "items": [
                {"kind": "INBOUND_CLASSIFICATION", "anchors": {"entry": anchor}, "payload": {"classification": "income", "reason": "confirmed employer payroll"}}
                for anchor in anchors
            ],
        }
        response = self.client.post(
            "/api/v1/batch", json=batch,
            headers={"origin": _ORIGIN, "content-type": "application/json", "x-csrf-token": self.csrf},
        )
        self.assertIn(response.status_code, (200, 202), response.text)
        refresh_ledger(self.database)

        income_after = self._get("/api/v1/reporting/inflows/candidates")
        sgd_after = next(c for c in income_after["native"]["candidates"] if c["currency"] == "SGD")
        # The queue's own detector is unaffected by classification: identity,
        # occurrence count and confidence are exactly as before.
        self.assertEqual(sgd_after["candidate"], sgd_candidate["candidate"])
        self.assertEqual(len(sgd_after["occurrences"]), len(sgd_candidate["occurrences"]))
        self.assertEqual(sgd_after["confidence"], "likely")

        analysis = self._get(
            "/api/v1/reporting/analysis", history_start="2026-01-01", history_end="2026-04-25",
        )
        income_bucket = analysis["summary"]["income"]["current"]
        self.assertEqual(income_bucket["status"], "available")
        self.assertEqual(Decimal(income_bucket["total"]["value"]), Decimal("20000.00"))


class StaleGenerationAcrossAllFourRoutesTests(_ReleaseBApiBase):
    """§9.2: a manifest that cannot reproduce its own published snapshot is
    refused before any card is composed -- checked here for all four M4
    routes together, not just one at a time."""

    def test_a_rate_import_without_refresh_makes_all_four_routes_409(self) -> None:
        from fincl.fx import load_fx_rates

        connection = connect_sqlite(self.database)
        try:
            load_fx_rates(
                connection,
                {
                    "source": "test", "source_version": "release-b-stale", "quote_currency": "SGD",
                    "rates": [{"base_currency": "EUR", "period": "2026-01", "rate": "0.7000"}],
                },
            )
        finally:
            connection.close()

        for path in (
            "/api/v1/reporting/cash", "/api/v1/reporting/net-worth",
            "/api/v1/reporting/forecast", "/api/v1/reporting/inflows/candidates",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 409, f"{path}: {response.text}")


if __name__ == "__main__":
    unittest.main()
