"""Cash-flow framework Stage 5B — basic tracked net worth.

A *tracked* net worth, not a personal-wealth figure.  It starts from the dated
positions the app already holds — statement-backed cash, personal debts and
receivables, and existing credit-card liability — and reports their subtotal
per currency with every missing component named.  It never asserts that the
subtotal is a complete statement of what someone owns.

Three existing reads are composed here, each used once and only once:

* :func:`fincl.position.queries.position_page` for cash pockets;
* :func:`fincl.obligations.queries.obligations_page` for personal debts and
  receivables (its card-statement and installment rows are deliberately
  ignored — card liability has one authority below);
* :func:`fincl.cards.cash_conversion.card_cash_conversion` for the existing
  ``unpaid_billed`` + ``posted_unbilled`` card liability.

Because each contributing figure is taken from exactly one of those reads, an
installment already inside a card statement, or a manual debt linked to a
tracked card, is structurally unable to be counted twice.

Manual values already in the ledger (a manual balance observation, a personal
debt opened from an opening balance rather than an observed movement) keep
their basis and are reported on a separate ``manual`` line — never folded into
the evidence-only subtotal.

Like the other read stages this exposes one API route and no journal kind: the
inputs are already journalled elsewhere.
"""

from .queries import assemble_components, net_worth_page, summarise

__all__ = ["assemble_components", "net_worth_page", "summarise"]
