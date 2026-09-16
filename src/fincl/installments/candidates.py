"""Propose installment chains from the occurrences the card parser already recorded.

A Mari card statement prints each instalment as its own transaction line carrying
a family, an ordinal and a term (``Split Payment 3/12 Instalment``). The parser
keeps that block verbatim in ``document_records.raw_fields_json``; this module
reads it back and groups the lines into the chains a plan would confirm, so the
operator picks a proposed chain instead of hand-authoring occurrence anchors.

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
               r.description_raw, r.raw_fields_json
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
                **block,
            }
        )
    return out


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


def _candidate(chain: list[dict[str, Any]], group: tuple[str, str, str, int]) -> dict[str, Any]:
    account_id, currency, family, term = group
    complete = len(chain) == term and chain[0]["ordinal"] == 1 and chain[-1]["ordinal"] == term
    scale = max(item["scale"] for item in chain)
    observed = sum(item["coefficient"] * 10 ** (scale - item["scale"]) for item in chain)
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
        # Only a chain whose every instalment is on a statement has an exactly
        # observed principal. A partial chain leaves it null: the remaining
        # instalments are not evidence, and `amount x term` would be an
        # invention the schema is built to refuse.
        "known_principal": (
            {"value": format_decimal(observed, scale), "currency": currency}
            if complete
            else None
        ),
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
        claimed = {
            str(row["entry_anchor"])
            for row in connection.execute(
                "SELECT entry_anchor FROM installment_occurrences"
            ).fetchall()
        }
        groups: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
        for row in _rows(connection, card):
            if row["entry_anchor"] in claimed:
                continue
            key = (row["account_id"], row["currency"], row["family"], row["term"])
            groups.setdefault(key, []).append(row)

        candidates: list[dict[str, Any]] = []
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
            candidates.extend(_candidate(chain, key) for chain in chains)

        candidates.sort(key=lambda c: (c["opened_on"], c["merchant_label"]))
        return {
            "generation": token,
            "candidates": candidates,
            "ambiguous_groups": ambiguous,
            "claimed_occurrence_count": len(claimed),
        }
    finally:
        connection.close()


__all__ = ["installment_candidates"]
