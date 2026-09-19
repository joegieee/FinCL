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
    { kind: 'PAYABLE_FORGIVEN', label: 'Balance forgiven', sign: -1, offLedger: true, opening: false },
  ],
  RECEIVABLE: [
    { kind: 'LENT', label: 'Lent', sign: 1, offLedger: false, opening: true },
    { kind: 'PRINCIPAL_RETURNED', label: 'Principal returned to me', sign: -1, offLedger: false, opening: false },
    { kind: 'EXPENSE_PAID_FOR_OTHER', label: 'I paid an expense for someone', sign: 1, offLedger: false, opening: false },
    { kind: 'RECEIVABLE_WRITTEN_OFF', label: 'Written off as a loss', sign: -1, offLedger: true, opening: false },
  ],
} as const

export type EventKindSpec = (typeof EVENT_KINDS)[keyof typeof EVENT_KINDS][number]

/** The bank direction a source allocation for this event must carry, mirroring
 * `fincl.debts.projection.expected_bank_direction`: taking on a payable or
 * getting a receivable back is money in; everything else is money out. */
export function bankDirection(debtDirection: 'PAYABLE' | 'RECEIVABLE', spec: EventKindSpec): 'IN' | 'OUT' {
  return (debtDirection === 'PAYABLE' && spec.sign > 0) || (debtDirection === 'RECEIVABLE' && spec.sign < 0) ? 'IN' : 'OUT'
}

/** Event kinds on a debt of this direction that one bank movement in
 * `direction` can back (off-ledger kinds carry no source transaction). */
export function kindsForMovement(debtDirection: 'PAYABLE' | 'RECEIVABLE', direction: string): EventKindSpec[] {
  return EVENT_KINDS[debtDirection].filter(spec => !spec.offLedger && bankDirection(debtDirection, spec) === direction)
}

export type AllocationCandidate = {
  entry_anchor: string
  effective_at: string
  economic_direction: 'IN' | 'OUT'
  currency: string
  amount: string
  open_amount: string
  description: string | null
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
  search?: string
  /** Narrow to exactly one transaction (the classification editor's hand-off). */
  anchor?: string
  limit?: number
}): Promise<AllocationCandidatesPage> {
  const q = new URLSearchParams({ debt: opts.debtId, generation: opts.generation, limit: String(opts.limit ?? 50) })
  if (opts.eventKind) q.set('event_kind', opts.eventKind)
  if (opts.search) q.set('search', opts.search)
  if (opts.anchor) q.set('anchor', opts.anchor)
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

export function lifecycleCommand(opts: {
  kind: 'PERSONAL_DEBT_CLOSE' | 'PERSONAL_DEBT_REOPEN'
  debtId: string
  generation: string
  effectiveOn: string
  reason: string
}): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: opts.generation,
    kind: opts.kind,
    anchors: { debt: opts.debtId },
    payload: { effective_on: opts.effectiveOn, reason: opts.reason },
  })
}

export const RECONCILIATION_LABEL: Record<string, string> = {
  no_events: 'No events yet',
  opening_balance_only: 'Opening balance only — historical funding undisclosed',
  unverified: 'Unverified — a source no longer resolves',
  reconciled: 'Reconciled against source transactions',
}
