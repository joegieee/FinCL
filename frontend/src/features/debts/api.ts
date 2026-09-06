import { api } from '@/lib/api'

// v0.3 slice 1 debt read shapes. These are hand-written until the integration
// branch regenerates schema.d.ts from the FastAPI models.
export type WireMoney = { value: string; currency: string | null }

export type DebtCard = {
  debt_id: string
  direction: 'PAYABLE' | 'RECEIVABLE'
  currency: string
  counterparty_label: string
  opened_on: string
  due_on: string | null
  status: 'OPEN' | 'CLOSED'
  closed_on: string | null
  remaining_principal: WireMoney
  last_event: { kind: string; effective_on: string } | null
  reconciliation_status: string
  event_count: number
}

export type DebtEvent = {
  event_id: string
  event_kind: string
  effective_on: string
  opening_balance: boolean
  signed: number
  principal: WireMoney
  expense_category: string | null
  bound: boolean
  dangling_reason: string | null
  reason: string
  journal_seq: number
}

export type DebtsPage = {
  generation: string
  as_of: string
  i_owe: DebtCard[]
  owed_to_me: DebtCard[]
  next_cursor: string | null
}

export type DebtDetail = {
  generation: string
  as_of: string
  debt: DebtCard & { principal_history: DebtEvent[]; reason: string }
}

export function loadDebts(opts: { asOf?: string; generation: string }): Promise<DebtsPage> {
  const q = new URLSearchParams({ generation: opts.generation, limit: '200' })
  if (opts.asOf) q.set('as_of', opts.asOf)
  return api<DebtsPage>('debts?' + q)
}

export function loadDebtDetail(id: string, opts: { asOf?: string; generation: string }): Promise<DebtDetail> {
  const q = new URLSearchParams({ generation: opts.generation })
  if (opts.asOf) q.set('as_of', opts.asOf)
  return api<DebtDetail>(`debts/${encodeURIComponent(id)}?` + q)
}

export const RECONCILIATION_LABEL: Record<string, string> = {
  no_events: 'No events yet',
  opening_balance_only: 'Opening balance only — historical funding undisclosed',
  unverified: 'Unverified — a source no longer resolves',
  reconciled: 'Reconciled against source transactions',
}
