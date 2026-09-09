"""Resolve the as-of liquid position per ``(account_id, currency_pocket)``.

Pure read. Every figure is derived on this call from evidence that already
exists in the ledger:

* the exact running balance printed on the most recent completed
  ``ledger_entries`` row for the pocket (``statement-running-balance``), or
* a newer, non-retired operator ``balance_observations`` row
  (``manual-observation``) — dormant until Stage 2 wires the journal kind, but
  read here already so the two halves never diverge.

Nothing is stored as a mutable authoritative balance. A pocket older than
``STALE_AFTER_DAYS`` is still returned (a three-week-old statement close is the
best evidence there is) but flagged ``stale``. Credit-card pockets are returned
with ``liquid = false`` and excluded from the liquid total: an outstanding card
balance is a future obligation, not cash on hand.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from ..money import ExactMoney, exact_sum, format_decimal
from .models import STALE_AFTER_DAYS

# ``accounts.source_type`` suffix for a credit-card facility statement. The
# authoritative signal is a bound ``card_facility_accounts`` row; this is the
# fallback for a card imported before a facility was declared.
_CARD_SOURCE_SUFFIX = "_CARD"


def _card_account_ids(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT DISTINCT account_id FROM card_facility_accounts WHERE bound = 1"
    ).fetchall()
    return {row[0] for row in rows}


def _latest_running_balances(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, Any]]:
    """The newest completed printed running balance for each pocket.

    Ordered so the first row seen per ``(account_id, currency_pocket)`` is the
    latest: newest ``effective_at`` first, then a deterministic anchor/id
    tiebreak for same-instant rows.
    """
    rows = connection.execute(
        """
        SELECT account_id, currency_pocket, effective_at, effective_precision,
               balance_coefficient, balance_scale, balance_text
        FROM ledger_entries
        WHERE balance_coefficient IS NOT NULL
          AND lifecycle_state = 'COMPLETED'
        ORDER BY account_id, currency_pocket,
                 effective_at DESC, natural_anchor DESC, id DESC
        """
    ).fetchall()
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row[0], row[1])
        if key in latest:
            continue
        latest[key] = {
            "as_of": row[2][:10],
            "date_precision": row[3],
            "coefficient": row[4],
            "scale": row[5],
            "balance": format_decimal(row[4], row[5]),
            "printed_text": row[6],
        }
    return latest


def _latest_manual_observations(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], dict[str, Any]]:
    """The newest non-retired operator balance observation for each pocket."""
    try:
        rows = connection.execute(
            """
            SELECT account_id, currency_pocket, observed_on,
                   balance_coefficient, balance_scale
            FROM balance_observations
            WHERE retired = 0
            ORDER BY account_id, currency_pocket, observed_on DESC, journal_seq DESC
            """
        ).fetchall()
    except sqlite3.OperationalError:
        # Pre-schema-19 ledger: no manual overrides are possible.
        return {}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row[0], row[1])
        if key in latest:
            continue
        latest[key] = {
            "as_of": row[2][:10],
            "coefficient": row[3],
            "scale": row[4],
            "balance": format_decimal(row[3], row[4]),
        }
    return latest


def position_page(database: Path, *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    connection = sqlite3.connect(database)
    try:
        connection.row_factory = sqlite3.Row
        accounts = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id, institution, source_type, display_name, masked_identifier "
                "FROM accounts"
            )
        }
        card_ids = _card_account_ids(connection)
        running = _latest_running_balances(connection)
        manual = _latest_manual_observations(connection)
    finally:
        connection.close()

    scopes = sorted(set(running) | set(manual))
    pockets: list[dict[str, Any]] = []
    liquid_by_currency: dict[str, list[tuple[int, int]]] = {}
    withheld_currencies: dict[str, list[str]] = {}

    for account_id, currency_pocket in scopes:
        account = accounts.get(account_id)
        is_card = account_id in card_ids or (
            account is not None
            and str(account["source_type"]).upper().endswith(_CARD_SOURCE_SUFFIX)
        )
        stmt = running.get((account_id, currency_pocket))
        obs = manual.get((account_id, currency_pocket))

        basis = "unavailable"
        chosen: dict[str, Any] | None = None
        if obs is not None and (stmt is None or obs["as_of"] >= stmt["as_of"]):
            basis, chosen = "manual-observation", obs
        elif stmt is not None:
            basis, chosen = "statement-running-balance", stmt

        entry: dict[str, Any] = {
            "account_id": account_id,
            "institution": account["institution"] if account else None,
            "display_name": account["display_name"] if account else None,
            "source_type": account["source_type"] if account else None,
            "currency_pocket": currency_pocket,
            "liquid": not is_card,
            "excluded_reason": "credit-card facility balance is an obligation, not cash"
            if is_card
            else None,
            "basis": basis,
            "balance": chosen["balance"] if chosen else None,
            "as_of": chosen["as_of"] if chosen else None,
            "date_precision": stmt["date_precision"] if stmt else None,
            "stale": False,
            "staleness_days": None,
            "unavailable_reason": None
            if chosen
            else "no printed running balance or operator observation for this pocket",
            "statement_running_balance": stmt,
            "manual_observation": obs,
        }

        if chosen is not None:
            age = (today - date.fromisoformat(chosen["as_of"])).days
            entry["staleness_days"] = age
            entry["stale"] = age > STALE_AFTER_DAYS

        if not is_card:
            if chosen is not None:
                liquid_by_currency.setdefault(currency_pocket, []).append(
                    (chosen["coefficient"], chosen["scale"])
                )
            else:
                withheld_currencies.setdefault(currency_pocket, []).append(account_id)

        pockets.append(entry)

    liquid_total: dict[str, str] = {}
    liquid_total_withheld: dict[str, str] = {}
    currencies = set(liquid_by_currency) | set(withheld_currencies)
    for currency in sorted(currencies):
        if currency in withheld_currencies:
            missing = ", ".join(sorted(withheld_currencies[currency]))
            liquid_total_withheld[currency] = (
                f"withheld: no current balance for {missing}"
            )
            continue
        amounts = [
            ExactMoney(coefficient=coefficient, scale=scale, text="")
            for coefficient, scale in liquid_by_currency[currency]
        ]
        coefficient, scale = exact_sum(*amounts)
        liquid_total[currency] = format_decimal(coefficient, scale)

    return {
        "generated_for": today.isoformat(),
        "stale_after_days": STALE_AFTER_DAYS,
        "pockets": pockets,
        "liquid_total": liquid_total,
        "liquid_total_withheld": liquid_total_withheld,
    }
