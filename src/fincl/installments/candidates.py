"""Propose installment chains from the occurrences the card parser already recorded.

A Mari card statement prints each instalment as its own transaction line carrying
a family, an ordinal and a term (``Split Payment 3/12 Instalment``). The parser
keeps that block verbatim in ``document_records.raw_fields_json``; this module
reads it back and groups the lines into the chains a plan would confirm, so the
operator picks a proposed chain instead of hand-authoring occurrence anchors.

When the operator converts purchases into a split payment, the statement that
prints the chain's first instalment also prints a ``Split Payment Conversion``
credit for the converted total. That credit is the plan's principal, printed; it
is paired to a chain only when exactly one credit and one chain fit each other
(see ``_pair_conversions``), so a running chain can carry a known principal
without inventing one from ``amount x term``.

Nothing here writes. A proposal is evidence plus an arrangement of it, never a
confirmed plan: the operator still records ``INSTALLMENT_PLAN_SET``. Chains are
built from the printed ordinals alone, never from "these amounts look alike" --
similar monthly amounts are not a plan. Where the printed evidence cannot
distinguish two concurrent chains, the group is returned ``ambiguous`` with its
raw occurrences and proposes nothing, rather than guessing a split.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..db import connect_readonly
from ..errors import FinCLError
from ..generation import current_generation
from ..money import format_decimal

# The final instalment of a chain absorbs the rounding remainder, so it is the
# one occurrence permitted to differ from the fixed monthly amount (statements
# print it as 33.33 x 2 then 33.34). Every
# non-final occurrence must match exactly; a mid-chain change of amount starts a
# new chain and is surfaced for the operator rather than absorbed silently.
_CANDIDATE_SOURCE = "mari-card-installment-block-v1"
_LABEL_MAX = 200  # matches validation._LABEL_MAX for merchant_label


def _generation(connection: Any, expected: str | None) -> str:
    generation = current_generation(connection)
    if generation is None:
        raise FinCLError("Ledger is not ready. Run fincl refresh first.")
    token = str(generation["generation_token"])
    if expected and expected != token:
        raise FinCLError("STALE_GENERATION: reload before continuing")
    return token


def _installment_block(raw_fields_json: str) -> dict[str, Any] | None:
    """The parser's verbatim installment block, or None when the row has none."""
    try:
        fields = json.loads(raw_fields_json)
    except ValueError:
        return None
    block = fields.get("installment") if isinstance(fields, dict) else None
    if not isinstance(block, dict):
        return None
    ordinal, term, family = block.get("ordinal"), block.get("term"), block.get("family")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
        return None
    if not isinstance(term, int) or isinstance(term, bool) or term <= 0:
        return None
    if not isinstance(family, str) or not family.strip():
        return None
    if ordinal > term:
        return None
    return {"family": family.strip(), "ordinal": ordinal, "term": term}


def _rows(connection: Any, card: str | None) -> list[dict[str, Any]]:
    """Selected card entries carrying an installment block, oldest first.

    Only entries from the analytically selected extraction run are eligible, so
    a re-parsed statement cannot propose the same chain twice.
    """
    sql = """
        SELECT e.natural_anchor, e.account_id, e.currency_pocket, e.effective_at,
               e.effective_precision, e.amount_coefficient, e.amount_scale,
               r.description_raw, r.raw_fields_json, r.statement_id
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE x.selection_state = 'SELECTED'
          AND r.raw_fields_json LIKE '%"installment"%'
    """
    params: list[Any] = []
    if card:
        sql += " AND e.account_id = ?"
        params.append(card)
    sql += " ORDER BY e.effective_at, e.natural_anchor"
    out: list[dict[str, Any]] = []
    for row in connection.execute(sql, params).fetchall():
        block = _installment_block(str(row["raw_fields_json"]))
        if block is None:
            continue
        out.append(
            {
                "entry_anchor": str(row["natural_anchor"]),
                "account_id": str(row["account_id"]),
                "currency": str(row["currency_pocket"]),
                "effective_on": str(row["effective_at"])[:10],
                "date_precision": str(row["effective_precision"]),
                # An instalment is an outflow; the plan payload wants the
                # magnitude, so the sign is dropped here rather than in the UI.
                "coefficient": abs(int(row["amount_coefficient"])),
                "scale": int(row["amount_scale"]),
                "description": (row["description_raw"] or "") or None,
                "statement_id": str(row["statement_id"]),
                **block,
            }
        )
    return out


def _conversions(connection: Any, card: str | None) -> list[dict[str, Any]]:
    """Selected ``<family> Conversion`` credits from the Repayment/Conversion section.

    The credit is the total of the purchases the operator converted; the
    statement prints no link from it to the chain it opened, which is what
    ``_pair_conversions`` supplies.
    """
    sql = """
        SELECT e.natural_anchor, e.account_id, e.currency_pocket, e.effective_at,
               e.amount_coefficient, e.amount_scale,
               r.description_raw, r.raw_fields_json, r.statement_id
        FROM ledger_entries e
        JOIN document_records r ON r.id = e.document_record_id
        JOIN extraction_runs x ON x.id = e.extraction_run_id
        WHERE x.selection_state = 'SELECTED'
          AND e.amount_coefficient > 0
          AND r.description_raw LIKE '% Conversion'
    """
    params: list[Any] = []
    if card:
        sql += " AND e.account_id = ?"
        params.append(card)
    sql += " ORDER BY e.effective_at, e.natural_anchor"
    out: list[dict[str, Any]] = []
    for row in connection.execute(sql, params).fetchall():
        try:
            fields = json.loads(str(row["raw_fields_json"]))
        except ValueError:
            continue
        if not isinstance(fields, dict) or fields.get("summary_bucket") != "B":
            continue
        description = " ".join(str(row["description_raw"]).split())
        family = description[: -len(" Conversion")].strip()
        if not family:
            continue
        out.append(
            {
                "entry_anchor": str(row["natural_anchor"]),
                "account_id": str(row["account_id"]),
                "currency": str(row["currency_pocket"]),
                "effective_on": str(row["effective_at"])[:10],
                "coefficient": int(row["amount_coefficient"]),
                "scale": int(row["amount_scale"]),
                "description": description,
                "family": family,
                "statement_id": str(row["statement_id"]),
            }
        )
    return out


def _fits_conversion(chain: list[dict[str, Any]], conversion: dict[str, Any], term: int) -> bool:
    """Is ``chain`` the schedule this conversion credit would print?

    The chain must open with its 1/N line on the same statement as the credit,
    in the same family, and every observed instalment must be exactly what the
    principal implies: ``round-half-up(principal / term)`` each month, with the
    final instalment absorbing the remainder. Mari conversions
    follow that rule to the cent (e.g. 150.40 over 6 is 25.07 x 5 then 25.05).
    """
    first = chain[0]
    if first["ordinal"] != 1 or first["statement_id"] != conversion["statement_id"]:
        return False
    if (first["account_id"], first["currency"]) != (conversion["account_id"], conversion["currency"]):
        return False
    if first["family"].casefold() != conversion["family"].casefold():
        return False
    scale = max(conversion["scale"], *(item["scale"] for item in chain))
    principal = conversion["coefficient"] * 10 ** (scale - conversion["scale"])
    monthly = (2 * principal + term) // (2 * term)
    final = principal - monthly * (term - 1)
    if monthly <= 0 or final <= 0:
        return False
    return all(
        item["coefficient"] * 10 ** (scale - item["scale"])
        == (final if item["ordinal"] == term else monthly)
        for item in chain
    )


def _pair_conversions(
    chains: list[tuple[list[dict[str, Any]], int]], conversions: list[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """Chain index -> the one conversion credit that is its principal.

    A pair is made only when the chain fits exactly one credit and that credit
    fits exactly one chain. Two identical conversions on one statement are
    indistinguishable, so both chains keep an unknown principal rather than
    receiving a guess.
    """
    fits = {
        index: [c for c, conversion in enumerate(conversions) if _fits_conversion(chain, conversion, term)]
        for index, (chain, term) in enumerate(chains)
    }
    claims: dict[int, int] = {}
    for matched in fits.values():
        for c in matched:
            claims[c] = claims.get(c, 0) + 1
    return {
        index: conversions[matched[0]]
        for index, matched in fits.items()
        if len(matched) == 1 and claims[matched[0]] == 1
    }


def _same_amount(left: dict[str, Any], right: dict[str, Any]) -> bool:
    scale = max(left["scale"], right["scale"])
    return left["coefficient"] * 10 ** (scale - left["scale"]) == right[
        "coefficient"
    ] * 10 ** (scale - right["scale"])


def _within_rounding(left: dict[str, Any], right: dict[str, Any], term: int) -> bool:
    """Do two amounts differ by no more than the chain's disclosed rounding remainder?

    Mirrors the plan validator, which permits a schedule to miss the principal by
    at most one minor unit per row. That bound is tight enough to keep two
    genuinely different instalments apart -- 10.05 against a 25.07 chain is 1,502
    minor units out, nowhere near the 6 a six-month term allows.
    """
    scale = max(left["scale"], right["scale"])
    delta = abs(
        left["coefficient"] * 10 ** (scale - left["scale"])
        - right["coefficient"] * 10 ** (scale - right["scale"])
    )
    return delta <= term


def _chain_for(chains: list[list[dict[str, Any]]], row: dict[str, Any]) -> tuple[int | None, bool]:
    """Index of the one chain ``row`` continues, and whether the choice was ambiguous.

    A row continues a chain when it is the next ordinal and either repeats the
    chain's fixed amount or, being the chain's final instalment, lands within the
    disclosed rounding remainder of it. When two open chains both accept it the
    printed evidence does not say which, so the caller marks the group ambiguous
    rather than picking one.
    """
    matches = [
        index
        for index, chain in enumerate(chains)
        if chain[-1]["ordinal"] == row["ordinal"] - 1
        and (
            _same_amount(chain[-1], row)
            or (
                row["ordinal"] == row["term"]
                and _within_rounding(chain[-1], row, row["term"])
            )
        )
    ]
    if not matches:
        return None, False
    return matches[0], len(matches) > 1


def _label(chain: list[dict[str, Any]], family: str, term: int) -> str:
    """The best merchant name the statement offers for this chain.

    When a statement line carries no merchant the parser synthesizes its
    description from the instalment block itself ("Split Payment 3/12
    Instalment"), which names the scheme rather than the purchase and reads
    identically for every concurrent chain. Those are skipped in favour of a real
    merchant line, and when a chain has none the monthly amount is what actually
    tells the operator which purchase this is.
    """
    for item in chain:
        description = (item["description"] or "").strip()
        if not description:
            continue
        synthesized = f"{family} {item['ordinal']}/{item['term']} Instalment"
        if description.casefold() != synthesized.casefold():
            return description[:_LABEL_MAX]
    monthly = format_decimal(chain[0]["coefficient"], chain[0]["scale"])
    return f"{family} · {monthly} × {term}"[:_LABEL_MAX]


def _candidate(
    chain: list[dict[str, Any]],
    group: tuple[str, str, str, int],
    conversion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    account_id, currency, family, term = group
    complete = len(chain) == term and chain[0]["ordinal"] == 1 and chain[-1]["ordinal"] == term
    scale = max(item["scale"] for item in chain)
    observed = sum(item["coefficient"] * 10 ** (scale - item["scale"]) for item in chain)
    # The principal is known two ways, both printed: a conversion credit paired
    # to this chain, or every instalment of the chain on a statement. Anything
    # else leaves it null -- the remaining instalments are not evidence, and
    # `amount x term` would be an invention the schema is built to refuse.
    principal: dict[str, str] | None = None
    principal_source: str | None = None
    if conversion is not None:
        principal = {
            "value": format_decimal(conversion["coefficient"], conversion["scale"]),
            "currency": currency,
        }
        principal_source = "conversion_credit"
    elif complete:
        principal = {"value": format_decimal(observed, scale), "currency": currency}
        principal_source = "observed_chain"
    return {
        # Stable across refreshes: the same evidence proposes the same key, so a
        # dismissed or confirmed chain can be recognized again without storing it.
        "candidate_key": f"{account_id}:{family}:{term}:{chain[0]['entry_anchor']}",
        "card_account_id": account_id,
        "currency": currency,
        "family": family,
        "term_months": term,
        "merchant_label": _label(chain, family, term),
        "opened_on": chain[0]["effective_on"],
        "first_ordinal": chain[0]["ordinal"],
        "last_ordinal": chain[-1]["ordinal"],
        "observed_count": len(chain),
        "complete": complete,
        "observed_total": {"value": format_decimal(observed, scale), "currency": currency},
        "known_principal": principal,
        "principal_source": principal_source,
        "conversion": _conversion_shape(conversion) if conversion is not None else None,
        "occurrences": [
            {
                "entry_anchor": item["entry_anchor"],
                "occurrence_index": item["ordinal"],
                "occurrence_count": item["term"],
                "effective_on": item["effective_on"],
                "date_precision": item["date_precision"],
                "amount": format_decimal(item["coefficient"], item["scale"]),
                "description": item["description"],
            }
            for item in chain
        ],
        "source": _CANDIDATE_SOURCE,
    }


def _conversion_shape(conversion: dict[str, Any]) -> dict[str, str]:
    return {
        "entry_anchor": conversion["entry_anchor"],
        "effective_on": conversion["effective_on"],
        "amount": format_decimal(conversion["coefficient"], conversion["scale"]),
        "description": conversion["description"],
    }


def _unconverted_plan_chains(
    connection: Any, rows: list[dict[str, Any]]
) -> list[tuple[str, list[dict[str, Any]], int]]:
    """Confirmed plans with no conversion credit yet, as ``(plan_id, chain, term)``.

    A plan confirmed before conversion credits were read (or one whose credit
    arrived on a later import) still competes for its credit here, so the
    Installments screen can offer to attach it.
    """
    by_anchor = {row["entry_anchor"]: row for row in rows}
    plans: dict[str, tuple[int | None, list[dict[str, Any]]]] = {}
    for row in connection.execute(
        """
        SELECT p.plan_id, p.term_months, o.entry_anchor
        FROM installment_plans p
        JOIN installment_occurrences o ON o.plan_id = p.plan_id
        WHERE p.bound = 1 AND p.status = 'CONFIRMED'
          AND NOT EXISTS (SELECT 1 FROM installment_conversions c WHERE c.plan_id = p.plan_id)
        ORDER BY p.plan_id
        """
    ).fetchall():
        term, chain = plans.setdefault(str(row["plan_id"]), (row["term_months"], []))
        if str(row["entry_anchor"]) in by_anchor:
            chain.append(by_anchor[str(row["entry_anchor"])])
    out = []
    for plan_id, (term, chain) in plans.items():
        if not chain:
            continue
        chain.sort(key=lambda item: (item["ordinal"], item["effective_on"]))
        out.append((plan_id, chain, int(term) if term else chain[0]["term"]))
    return out


def _pairing(
    connection: Any,
    proposed: list[tuple[list[dict[str, Any]], int]],
    rows: list[dict[str, Any]],
    card: str | None,
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Pair unattached conversion credits across proposed chains and confirmed
    plans together, so one credit is never offered to two of them.

    Returns ``(proposed index -> credit, plan_id -> credit)``.
    """
    attached = {
        str(row["entry_anchor"])
        for row in connection.execute("SELECT entry_anchor FROM installment_conversions")
    }
    conversions = [c for c in _conversions(connection, card) if c["entry_anchor"] not in attached]
    plans = _unconverted_plan_chains(connection, rows)
    paired = _pair_conversions(proposed + [(chain, term) for _, chain, term in plans], conversions)
    by_plan = {
        plan_id: paired[len(proposed) + index]
        for index, (plan_id, _chain, _term) in enumerate(plans)
        if len(proposed) + index in paired
    }
    return {i: c for i, c in paired.items() if i < len(proposed)}, by_plan


def suggested_conversion(connection: Any, plan_id: str) -> dict[str, str] | None:
    """The one conversion credit that is this confirmed plan's principal, if the
    printed evidence names exactly one; ``None`` otherwise."""
    card = connection.execute(
        "SELECT card_account_id FROM installment_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if card is None:
        return None
    card_id = card["card_account_id"]
    proposed, _ambiguous, rows, _claimed = _proposed_chains(connection, card_id)
    _paired, by_plan = _pairing(connection, [(chain, key[3]) for chain, key in proposed], rows, card_id)
    conversion = by_plan.get(plan_id)
    return _conversion_shape(conversion) if conversion is not None else None


_Chain = tuple[list[dict[str, Any]], tuple[str, str, str, int]]


def _proposed_chains(
    connection: Any, card: str | None
) -> tuple[list[_Chain], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """``(proposed chains, ambiguous groups, every installment row, claimed anchors)``."""
    claimed = {
        str(row["entry_anchor"])
        for row in connection.execute(
            "SELECT entry_anchor FROM installment_occurrences"
        ).fetchall()
    }
    groups: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    all_rows = _rows(connection, card)
    for row in all_rows:
        if row["entry_anchor"] in claimed:
            continue
        key = (row["account_id"], row["currency"], row["family"], row["term"])
        groups.setdefault(key, []).append(row)

    proposed: list[tuple[list[dict[str, Any]], tuple[str, str, str, int]]] = []
    ambiguous: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        chains: list[list[dict[str, Any]]] = []
        group_ambiguous = False
        for row in sorted(rows, key=lambda r: (r["ordinal"], r["effective_on"])):
            index, tied = _chain_for(chains, row)
            group_ambiguous = group_ambiguous or tied
            if index is None:
                chains.append([row])
            else:
                chains[index].append(row)
        if group_ambiguous:
            ambiguous.append(
                {
                    "card_account_id": key[0],
                    "currency": key[1],
                    "family": key[2],
                    "term_months": key[3],
                    "reason": (
                        "Two unconfirmed chains in this family share an instalment "
                        "amount and run at the same time, so the printed ordinals "
                        "do not say which chain a line belongs to. Confirm one plan "
                        "from the transaction list to separate them."
                    ),
                    "occurrences": [
                        {
                            "entry_anchor": row["entry_anchor"],
                            "occurrence_index": row["ordinal"],
                            "occurrence_count": row["term"],
                            "effective_on": row["effective_on"],
                            "amount": format_decimal(row["coefficient"], row["scale"]),
                            "description": row["description"],
                        }
                        for row in sorted(rows, key=lambda r: (r["ordinal"], r["effective_on"]))
                    ],
                }
            )
            continue
        proposed.extend((chain, key) for chain in chains)

    return proposed, ambiguous, all_rows, claimed


def propose(connection: Any, card: str | None = None) -> dict[str, Any]:
    """The proposals themselves, read on the caller's connection.

    Split out so a read model that already holds a generation-pinned
    connection (the categorization inbox's "Installment candidates" focus)
    proposes from exactly the snapshot it is showing, not a second one.
    """
    proposed, ambiguous, rows, claimed = _proposed_chains(connection, card)
    paired, _by_plan = _pairing(connection, [(chain, key[3]) for chain, key in proposed], rows, card)
    candidates = [
        _candidate(chain, key, paired.get(index)) for index, (chain, key) in enumerate(proposed)
    ]

    candidates.sort(key=lambda c: (c["opened_on"], c["merchant_label"]))
    return {
        "candidates": candidates,
        "ambiguous_groups": ambiguous,
        "claimed_occurrence_count": len(claimed),
    }


def installment_candidates(
    database: Path,
    *,
    card: str | None = None,
    expected: str | None = None,
) -> dict[str, Any]:
    """Propose unconfirmed installment chains from printed statement evidence."""
    connection = connect_readonly(database)
    try:
        token = _generation(connection, expected)
        return {"generation": token, **propose(connection, card)}
    finally:
        connection.close()


__all__ = ["installment_candidates", "propose", "suggested_conversion"]
