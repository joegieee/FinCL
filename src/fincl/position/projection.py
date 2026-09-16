"""Replay the ``BALANCE_OBSERVATION_*`` kinds into schema 19.

``apply_balance_observation`` projects one record: a ``SET`` upserts the
observation row (``retired = 0``); a ``RETIRE`` marks it retired so the read
path falls back to the next-best basis. The as-of position itself is computed
on read in :mod:`fincl.position.queries`; nothing here stores a mutable
authoritative balance.

The same function projects the ``MOVEMENT_COVERAGE_*`` kinds into
``movement_coverage_certifications`` (schema 24). A certification row carries a
date range and its inspected basis and no amount, so replaying one can widen
which days count as covered but can never change a figure directly.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import (
    BALANCE_OBSERVATION_RETIRE,
    BALANCE_OBSERVATION_SET,
    MOVEMENT_COVERAGE_CERTIFY,
    MOVEMENT_COVERAGE_RETIRE,
)


def apply_balance_observation(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
) -> None:
    if decision_kind in (MOVEMENT_COVERAGE_CERTIFY, MOVEMENT_COVERAGE_RETIRE):
        _apply_movement_coverage(
            connection,
            decision_kind=decision_kind,
            anchors=anchors,
            payload=payload,
            journal_seq=journal_seq,
        )
        return

    observation = anchors["observation"]
    if decision_kind == BALANCE_OBSERVATION_SET:
        balance = ExactMoney.parse(str(payload["balance"]))
        connection.execute(
            """
            INSERT INTO balance_observations
                (observation, account_id, currency_pocket, observed_on,
                 balance_coefficient, balance_scale, journal_seq, retired)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(observation) DO UPDATE SET
                account_id = excluded.account_id,
                currency_pocket = excluded.currency_pocket,
                observed_on = excluded.observed_on,
                balance_coefficient = excluded.balance_coefficient,
                balance_scale = excluded.balance_scale,
                journal_seq = excluded.journal_seq,
                retired = 0
            """,
            (
                observation,
                str(payload["account_id"]),
                str(payload["currency_pocket"]),
                str(payload["observed_on"]),
                balance.coefficient,
                balance.scale,
                journal_seq,
            ),
        )
        return

    if decision_kind == BALANCE_OBSERVATION_RETIRE:
        updated = connection.execute(
            "UPDATE balance_observations SET retired = 1, journal_seq = ? "
            "WHERE observation = ?",
            (journal_seq, observation),
        ).rowcount
        if updated == 0:
            raise FinCLError(
                f"balance observation {observation} was retired before it was set"
            )
        return

    raise FinCLError(  # pragma: no cover - guarded by the dispatcher
        f"{decision_kind} is not a position decision"
    )


def _apply_movement_coverage(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
) -> None:
    certification = anchors["certification"]
    if decision_kind == MOVEMENT_COVERAGE_CERTIFY:
        connection.execute(
            """
            INSERT INTO movement_coverage_certifications
                (certification, account_id, currency_pocket, period_start,
                 period_end, basis, journal_seq, retired)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(certification) DO UPDATE SET
                account_id = excluded.account_id,
                currency_pocket = excluded.currency_pocket,
                period_start = excluded.period_start,
                period_end = excluded.period_end,
                basis = excluded.basis,
                journal_seq = excluded.journal_seq,
                retired = 0
            """,
            (
                certification,
                str(payload["account_id"]),
                str(payload["currency_pocket"]),
                str(payload["period_start"]),
                str(payload["period_end"]),
                str(payload["basis"]),
                journal_seq,
            ),
        )
        return

    updated = connection.execute(
        "UPDATE movement_coverage_certifications SET retired = 1, journal_seq = ? "
        "WHERE certification = ?",
        (journal_seq, certification),
    ).rowcount
    if updated == 0:
        raise FinCLError(
            f"movement coverage certification {certification} was retired "
            "before it was certified"
        )
