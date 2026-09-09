"""Replay the ``BALANCE_OBSERVATION_*`` kinds into schema 19.

``apply_balance_observation`` projects one record: a ``SET`` upserts the
observation row (``retired = 0``); a ``RETIRE`` marks it retired so the read
path falls back to the next-best basis. The as-of position itself is computed
on read in :mod:`fincl.position.queries`; nothing here stores a mutable
authoritative balance.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..errors import FinCLError
from ..money import ExactMoney
from .models import BALANCE_OBSERVATION_RETIRE, BALANCE_OBSERVATION_SET


def apply_balance_observation(
    connection: sqlite3.Connection,
    *,
    decision_kind: str,
    anchors: dict[str, str],
    payload: dict[str, Any],
    journal_seq: int,
) -> None:
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
        f"{decision_kind} is not a balance-observation decision"
    )
