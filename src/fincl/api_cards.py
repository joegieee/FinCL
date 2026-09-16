"""Card and installment read routes under `/api/v1` (v0.4).

Registered by `api.create_app`. Slice 1 wires the two card reads that stand on
the extraction-derived `card_statement_facts` projection and the printed source
equation; `/api/v1/cards/{id}`, `/api/v1/installments` and
`/api/v1/installments/{id}` land in later slices. Every route takes the
generation token as the `generation` query parameter and reuses the shared
stale-generation / not-found mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from .errors import FinCLError
from .locking import ledger_writer


def _map_error(exc: FinCLError) -> HTTPException:
    message = str(exc)
    if message.startswith("STALE_GENERATION"):
        return HTTPException(409, message)
    if "does not exist" in message:
        return HTTPException(404, message)
    return HTTPException(422, message)


def register_card_reads(app: FastAPI, database: Path) -> None:
    @app.get("/api/v1/cards")
    def cards(
        as_of: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        generation: str | None = None,
    ) -> Any:
        from .cards.queries import cards_page

        with ledger_writer(database):
            try:
                return cards_page(
                    database,
                    as_of=as_of,
                    cursor=cursor,
                    limit=limit,
                    expected=generation,
                )
            except FinCLError as exc:
                raise _map_error(exc) from exc

    @app.get("/api/v1/cards/{account_id}")
    def card_detail_route(
        account_id: str,
        as_of: str | None = None,
        generation: str | None = None,
    ) -> Any:
        from .cards.queries import card_detail

        with ledger_writer(database):
            try:
                return card_detail(
                    database, account_id, as_of=as_of, expected=generation
                )
            except FinCLError as exc:
                raise _map_error(exc) from exc

    @app.get("/api/v1/cards/{account_id}/statements")
    def card_statements_route(
        account_id: str,
        cursor: str | None = None,
        limit: int = 50,
        generation: str | None = None,
    ) -> Any:
        from .cards.queries import card_statements

        with ledger_writer(database):
            try:
                return card_statements(
                    database,
                    account_id,
                    cursor=cursor,
                    limit=limit,
                    expected=generation,
                )
            except FinCLError as exc:
                raise _map_error(exc) from exc

    @app.get("/api/v1/installments")
    def installments(
        card: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        generation: str | None = None,
    ) -> Any:
        from .installments.queries import installments_page

        with ledger_writer(database):
            try:
                return installments_page(
                    database,
                    card=card,
                    status=status,
                    cursor=cursor,
                    limit=limit,
                    expected=generation,
                )
            except FinCLError as exc:
                raise _map_error(exc) from exc

    # Registered before `/installments/{plan_id}`: FastAPI matches in definition
    # order, so the literal path has to win before "candidates" is read as an id.
    @app.get("/api/v1/installments/candidates")
    def installment_candidates_route(
        card: str | None = None,
        generation: str | None = None,
    ) -> Any:
        from .installments.candidates import installment_candidates

        with ledger_writer(database):
            try:
                return installment_candidates(database, card=card, expected=generation)
            except FinCLError as exc:
                raise _map_error(exc) from exc

    @app.get("/api/v1/installments/{plan_id}")
    def installment_detail_route(
        plan_id: str,
        as_of: str | None = None,
        generation: str | None = None,
    ) -> Any:
        from .installments.queries import installment_detail

        with ledger_writer(database):
            try:
                return installment_detail(
                    database, plan_id, as_of=as_of, expected=generation
                )
            except FinCLError as exc:
                raise _map_error(exc) from exc


__all__ = ["register_card_reads"]
