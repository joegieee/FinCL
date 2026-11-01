import type { LucideIcon } from 'lucide-react'
import {
  Banknote, Building2, CalendarClock, CalendarRange, CircleHelp, CreditCard, FileBarChart, GitCompare,
  Gauge, HandCoins, Layers, LayoutDashboard, PiggyBank, Receipt, Repeat, Scale, SlidersHorizontal,
  Tag, Target, UploadCloud, Wallet, Wrench, ListChecks, MapPinned,
} from 'lucide-react'

export type TabScope = 'range' | 'month' | 'currency' | 'none'
export type RailSectionName = 'Overview' | 'Money in & out' | 'Commitments' | 'Planning' | 'Position & forecast' | 'Setup & data'

// What each section is called, and which of its destinations opens when the
// group itself is clicked. Twenty-eight flat rows made the rail a list to scan
// rather than a place to navigate; grouping them puts six things on screen and
// keeps the twenty-eight one click further in, none of them hidden. The lead
// tab is the one that answers the group's own question, so clicking a group is
// always useful rather than merely expanding it.
export const sectionChrome: Record<RailSectionName, { label: string; lead: string }> = {
  'Overview': { label: 'Overview', lead: 'Overview' },
  'Money in & out': { label: 'Money', lead: 'Transactions' },
  'Commitments': { label: 'Commitments', lead: 'Obligations' },
  'Planning': { label: 'Plan ahead', lead: 'Planning' },
  'Position & forecast': { label: 'Position', lead: 'Will I have enough' },
  'Setup & data': { label: 'Setup', lead: 'Setup' },
}

export type TabMeta = {
  scope: TabScope
  title: string
  subtitle: string
  /** Display label, when it differs from the tab's internal identifier (e.g. `Plans` reads as `Budgets`). */
  label?: string
  /** Rail section this destination belongs to. Absent for aliases (`Cash Flow`, `Money Owed`), which are not rail rows. */
  section?: RailSectionName
  icon?: LucideIcon
}

// One entry per tab. Declared in rail order (grouped by section) so the rail
// and the command palette read directly off this table instead of a second
// list that can drift from it. Aliases resolve to a primary tab's content in
// App.tsx but carry no `section`, so they never render as a rail row.
export const tabMeta: Record<string, TabMeta> = {
  Overview: { scope: 'range', title: 'Overview', subtitle: '', section: 'Overview', icon: LayoutDashboard },

  Transactions: { scope: 'range', title: 'Transactions', subtitle: 'Filter, search and open any transaction in the selected period.', section: 'Money in & out', icon: Receipt },
  Analysis: { scope: 'range', title: 'Analysis', subtitle: 'Spending and income grouped and compared across the selected period.', section: 'Money in & out', icon: GitCompare },
  Trips: { scope: 'none', title: 'Trips', subtitle: 'Trips by date and country. Transactions join a trip only when their country is known.', section: 'Money in & out', icon: MapPinned },
  Reports: { scope: 'none', title: 'Reports', subtitle: 'Keep a period, filters and grouping; the figures are worked out fresh each time you open one.', section: 'Money in & out', icon: FileBarChart },
  Income: { scope: 'none', title: 'Income', subtitle: 'Regular payers found in your imported receipts. Nothing is classified until you confirm it.', section: 'Money in & out', icon: HandCoins },
  Categorization: { scope: 'none', title: 'Categorization', subtitle: 'Clear your backlog in groups, then choose a one-off correction or a reusable rule.', section: 'Money in & out', icon: Tag },
  Categories: { scope: 'none', title: 'Categories', subtitle: 'Organize categories without altering any recorded transaction.', section: 'Money in & out', icon: Layers },
  Rules: { scope: 'none', title: 'Rules', subtitle: 'Build merchant aliases and category rules from exact matches.', section: 'Money in & out', icon: ListChecks },
  Review: { scope: 'none', title: 'Review', subtitle: 'Confirm proposed transfers and resolve relationships that are still open.', section: 'Money in & out', icon: CircleHelp },

  Obligations: { scope: 'none', title: 'Obligations', subtitle: 'Debts, installments, card dues and recurring commitments in one list, grouped by confidence.', section: 'Commitments', icon: HandCoins },
  Debts: { scope: 'none', title: 'Debts', subtitle: 'Informal money you owe or are owed. Payables and receivables are shown separately and never netted.', section: 'Commitments', icon: Wallet },
  Cards: { scope: 'none', title: 'Cards', subtitle: "Each figure comes from a verified statement and holds for that statement's period, not as a current balance.", section: 'Commitments', icon: CreditCard },
  Installments: { scope: 'none', title: 'Installments', subtitle: 'Confirmed installment plans on your cards. Unknown principal is shown as unknown.', section: 'Commitments', icon: Layers },

  Planning: { scope: 'none', title: 'Planning', subtitle: 'Dated budgets, savings progress and reviewed recurring schedules.', section: 'Planning', icon: CalendarRange },
  Planned: { scope: 'none', title: 'Planned', subtitle: 'Purchases, bills, instalments and income you expect, counted in the forecast and checked against what was actually paid once each date passes.', section: 'Planning', icon: CalendarClock },
  Plans: { scope: 'none', label: 'Budgets', title: 'Budgets', subtitle: 'Monthly budgets and savings goals, compared against covered ledger totals.', section: 'Planning', icon: Target },
  Recurring: { scope: 'none', title: 'Recurring', subtitle: 'Payments that repeat in your imported history, not yet confirmed as subscriptions.', section: 'Planning', icon: Repeat },
  'Annual funds': { scope: 'none', title: 'Annual funds', subtitle: 'Recurring annual costs, saved toward ahead of the month they land in.', section: 'Planning', icon: PiggyBank },
  Controls: { scope: 'month', title: 'Controls', subtitle: 'Commitments, budget pace and what-if changes that never alter the ledger.', section: 'Planning', icon: SlidersHorizontal },

  'Will I have enough': { scope: 'none', title: 'Will I have enough?', subtitle: 'One answer across all your accounts and currencies, what it assumes, and what would make it better.', section: 'Position & forecast', icon: Gauge },
  Baseline: { scope: 'none', title: 'Baseline', subtitle: 'How much of each month your statements support, before working out what life costs. It comes from imports and is never typed in.', section: 'Position & forecast', icon: FileBarChart },
  Cash: { scope: 'none', title: 'Cash', subtitle: 'Recorded and observed cash balances, kept separate from anything only inferred.', section: 'Position & forecast', icon: Banknote },
  Forecast: { scope: 'none', title: 'Forecast', subtitle: 'A read-only estimate from the last date your statements support. Missing inputs are named, not assumed.', section: 'Position & forecast', icon: GitCompare },
  'Net worth': { scope: 'none', title: 'Net worth', subtitle: 'A subtotal, per currency, of the dated cash, debt, receivable and card balances your records support.', section: 'Position & forecast', icon: Scale },
  Variance: { scope: 'none', title: 'Variance', subtitle: 'Budgets and goals compared against covered ledger totals, and by how much they differ.', section: 'Position & forecast', icon: GitCompare },

  Setup: { scope: 'none', title: 'Setup', subtitle: 'Bring in statements and confirm coverage.', section: 'Setup & data', icon: UploadCloud },
  Accounts: { scope: 'none', title: 'Accounts', subtitle: 'Set when each account is active and link its statements.', section: 'Setup & data', icon: Building2 },
  Maintenance: { scope: 'none', title: 'Maintenance', subtitle: 'Back up the ledger and check that it restores.', section: 'Setup & data', icon: Wrench },

  // Aliases: same content as a primary tab, reachable by URL and the command
  // palette, but not a separate rail row.
  'Cash Flow': { scope: 'range', title: 'Analysis', subtitle: 'Spending and income grouped and compared across the selected period.' },
  'Money Owed': { scope: 'none', title: 'Obligations', subtitle: 'Debts, installments, card dues and recurring commitments in one list, grouped by confidence.' },
}

export function tabLabel(tab: string): string { return tabMeta[tab]?.label ?? tab }

// Alias -> primary tab it resolves to, for the command palette and deep links.
export const tabAliases: Record<string, string> = { 'Cash Flow': 'Analysis', 'Money Owed': 'Obligations' }

export type RailSection = {
  name: RailSectionName
  /** Short label shown on the group row. */
  label: string
  /** The destination the group row opens, always one of `tabs`. */
  lead: string
  tabs: string[]
}

// Grouped in the same order as declared above, derived once at module load.
export const railSections: RailSection[] = (() => {
  const sections: RailSection[] = []
  for (const [key, meta] of Object.entries(tabMeta)) {
    if (!meta.section) continue
    let bucket = sections.find(s => s.name === meta.section)
    if (!bucket) {
      const chrome = sectionChrome[meta.section]
      bucket = { name: meta.section, label: chrome.label, lead: chrome.lead, tabs: [] }
      sections.push(bucket)
    }
    bucket.tabs.push(key)
  }
  return sections
})()

/** The section a destination belongs to, for expanding the right group. */
export const sectionOfTab: Record<string, RailSectionName> = Object.fromEntries(
  railSections.flatMap(section => section.tabs.map(tab => [tab, section.name])),
)

export const railTabs: string[] = railSections.flatMap(s => s.tabs)

// Every destination reachable from the command palette: rail rows in order,
// then aliases appended so `Cash Flow` and `Money Owed` remain one keystroke
// away without occupying a row of their own.
export const commandDestinations: string[] = [...railTabs, ...Object.keys(tabAliases)]
