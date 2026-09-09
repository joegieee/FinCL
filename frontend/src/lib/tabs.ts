export type TabScope = 'range' | 'month' | 'currency' | 'none'

export type TabMeta = {
  scope: TabScope
  eyebrow?: string
  title: string
  subtitle: string
}

// One entry per rendered tab. Aliases (`Cash Flow` / `Money Owed`) get their
// own entries rather than resolving through the primary name, so a tab's
// scope, title and subtitle live in exactly one place instead of drifting
// across ternary chains.
export const tabMeta: Record<string, TabMeta> = {
  Overview: { scope: 'range', eyebrow: 'YOUR MONEY, UNDERSTOOD', title: 'A clearer picture.', subtitle: 'Review the evidence-backed picture and act deliberately.' },
  Baseline: { scope: 'none', title: 'What your evidence can support.', subtitle: 'Month-by-month evidence readiness before any "what my life costs" figure. The baseline is discovered from imports, never typed in.' },
  Cash: { scope: 'none', title: 'What you actually hold.', subtitle: 'Recorded and observed cash balances, kept separate from anything only inferred.' },
  Forecast: { scope: 'none', title: 'Your next six months, with the assumptions visible.', subtitle: 'A read-only estimate from the supported cash cutoff. Conditional timing remains conditional, and unavailable inputs stay named gaps.' },
  'Annual funds': { scope: 'none', title: 'Spread across the year, set aside ahead of time.', subtitle: 'Recurring annual costs, saved toward ahead of the month they land in.' },
  Variance: { scope: 'currency', title: 'Where the plan and the ledger disagree.', subtitle: 'Budgets and goals compared against covered ledger totals, and by how much they differ.' },
  Controls: { scope: 'month', title: 'Spend with the whole path in view.', subtitle: 'Disjoint commitments, budget pace, and hypothetical corrections that never alter the ledger.' },
  'Cash Flow': { scope: 'range', title: 'Where the money moved.', subtitle: 'Spending and income grouped and compared across the selected period.' },
  Analysis: { scope: 'range', title: 'Where the money moved.', subtitle: 'Spending and income grouped and compared across the selected period.' },
  Transactions: { scope: 'range', title: 'Every recorded transaction.', subtitle: 'Filter, search and open any transaction in the selected period.' },
  Categorization: { scope: 'range', title: 'Teach it once.', subtitle: 'Clear your backlog in groups, then choose a one-off correction or a reusable rule.' },
  Debts: { scope: 'none', title: 'Your debts, clearly separated.', subtitle: 'Informal money you owe or are owed. Payables and receivables are shown separately and never netted.' },
  Cards: { scope: 'none', title: 'Your cards, statement by statement.', subtitle: "Each figure is a fact at a statement period end, from that issuer's verified source equation — never a timeless \"current balance\"." },
  Installments: { scope: 'none', title: 'Plans, split over time.', subtitle: 'Confirmed source-proven installment chains on your cards, with unknown principal shown rather than assumed.' },
  'Money Owed': { scope: 'none', title: 'Everything owed, in one place.', subtitle: 'A read-only merge of debts, installments, card statement dues and recurring commitments, grouped by confidence.' },
  Obligations: { scope: 'none', title: 'Everything owed, in one place.', subtitle: 'A read-only merge of debts, installments, card statement dues and recurring commitments, grouped by confidence.' },
  'Net worth': { scope: 'none', title: 'A tracked subtotal, not a verdict.', subtitle: 'A per-currency subtotal of the dated cash, debt, receivable and card positions the ledger can already support.' },
  Review: { scope: 'none', title: 'What needs a decision.', subtitle: 'Confirm proposed transfers and resolve relationships that are still open.' },
  Planning: { scope: 'none', title: 'Plan by the month.', subtitle: 'Dated budgets, savings observations and reviewed recurring schedules.' },
  Plans: { scope: 'none', title: 'Give your spending some boundaries.', subtitle: 'Monthly budgets and savings goals, compared against covered ledger totals.' },
  Recurring: { scope: 'none', title: 'What keeps repeating.', subtitle: 'Payment candidates detected from imported history, not yet confirmed as subscriptions.' },
  Categories: { scope: 'none', title: 'What money was for.', subtitle: 'Organize categories without altering any recorded transaction.' },
  Rules: { scope: 'none', title: 'Teach it once.', subtitle: 'Build merchant aliases and category rules from closed literal predicates.' },
  Accounts: { scope: 'none', title: 'What this ledger covers.', subtitle: 'Declare expected-active windows and bind source accounts.' },
  Setup: { scope: 'none', title: 'Set up your ledger.', subtitle: 'Bring in statements and confirm coverage.' },
  Maintenance: { scope: 'none', title: 'Keep the ledger consistent.', subtitle: 'Housekeeping tasks for the underlying data model.' },
}
