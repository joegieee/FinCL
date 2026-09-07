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

// --- slice 2 writes: source-linked events and shared-expense residuals --------

// event_kind -> +1 raises the obligation, -1 reduces it. Off-ledger kinds carry
// no source allocations; EXPENSE_PAID_FOR_ME additionally needs a category.
export const EVENT_KINDS = {
  PAYABLE: [
    { kind: 'BORROWED', label: 'Borrowed', sign: 1, offLedger: false, opening: true },
    { kind: 'PRINCIPAL_REPAID', label: 'Repaid principal', sign: -1, offLedger: false, opening: false },
    { kind: 'EXPENSE_PAID_FOR_ME', label: 'Someone paid an expense for me', sign: 1, offLedger: true, opening: false },
  ],
  RECEIVABLE: [
    { kind: 'LENT', label: 'Lent', sign: 1, offLedger: false, opening: true },
    { kind: 'PRINCIPAL_RETURNED', label: 'Principal returned to me', sign: -1, offLedger: false, opening: false },
    { kind: 'EXPENSE_PAID_FOR_OTHER', label: 'I paid an expense for someone', sign: 1, offLedger: false, opening: false },
  ],
} as const

export type EventKindSpec = (typeof EVENT_KINDS)[keyof typeof EVENT_KINDS][number]

export type AllocationCandidate = {
  entry_anchor: string
  effective_at: string
  economic_direction: 'IN' | 'OUT'
  currency: string
  amount: string
  open_amount: string
}

export type AllocationCandidatesPage = {
  generation: string
  debt_id: string
  event_kind: string
  required_direction: 'IN' | 'OUT'
  currency: string
  candidates: AllocationCandidate[]
  next_cursor: string | null
}

export function loadAllocationCandidates(opts: {
  debtId: string
  eventKind?: string
  generation: string
}): Promise<AllocationCandidatesPage> {
  const q = new URLSearchParams({ debt: opts.debtId, generation: opts.generation, limit: '100' })
  if (opts.eventKind) q.set('event_kind', opts.eventKind)
  return api<AllocationCandidatesPage>('debts/allocation-candidates?' + q)
}

export type AllocationInput = {
  entry_anchor: string
  principal_amount: string
  interest_amount: string
  fee_amount: string
  separate_fee_amount: string
}

export type DebtCommandBody = {
  request_id: string
  expected_generation: string
  kind: string
  anchors: Record<string, string>
  payload: Record<string, unknown>
}

export async function previewThenCommit(body: DebtCommandBody): Promise<{ status: string }> {
  await api('preview', body)
  return api<{ status: string }>('commands', body)
}

export const RECONCILIATION_LABEL: Record<string, string> = {
  no_events: 'No events yet',
  opening_balance_only: 'Opening balance only — historical funding undisclosed',
  unverified: 'Unverified — a source no longer resolves',
  reconciled: 'Reconciled against source transactions',
}
