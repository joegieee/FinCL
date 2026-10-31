import { reportedApi } from '@/lib/workspaceReporting'
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
  /** The repayment plan's headline, when the debt has one. */
  plan?: { next_due: string | null; overdue_count: number; unscheduled_remainder: boolean } | null
}

export type PlanRowState = 'paid' | 'part_paid' | 'overdue' | 'upcoming' | 'trimmed'

export type PlanRow = {
  schedule_index: number
  due_on: string
  amount: string
  paid: string
  remaining: string
  /** How much of the row the principal no longer needs. */
  trimmed: string
  was_trimmed: boolean
  state: PlanRowState
  overdue: boolean
}

/** A debt's repayment plan as the server reads it: every amount is computed there. */
export type PlanState = {
  effective_from: string
  plan_kind: 'rows' | 'series'
  reason: string
  rows: PlanRow[]
  remaining_principal: string
  scheduled_remaining: string
  /** What the rows do not cover; dated when the debt has its own due date. */
  unscheduled_remainder: { amount: string; due_on: string | null; overdue: boolean } | null
  next_due: string | null
  overdue_count: number
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

/** An active recurring commitment whose payments are this debt's own movements. */
export type OverlappingCommitment = { commitment: string; name: string; payments: number }

export type DebtDetail = {
  generation: string
  as_of: string
  debt: DebtCard & { principal_history: DebtEvent[]; reason: string; overlapping_commitments?: OverlappingCommitment[]; schedule?: PlanState | null }
}

/** Every debt, following the server's pages: the lists, the split form and the
 * classification hand-off all need the whole set, not the first page of it. */
export async function loadDebts(opts: { asOf?: string; generation: string }): Promise<DebtsPage> {
  const q = new URLSearchParams({ generation: opts.generation, limit: '200' })
  if (opts.asOf) q.set('as_of', opts.asOf)
  const page = await reportedApi<DebtsPage>('debts?' + q)
  for (let cursor = page.next_cursor; cursor; ) {
    q.set('cursor', cursor)
    const next = await reportedApi<DebtsPage>('debts?' + q)
    page.i_owe.push(...next.i_owe)
    page.owed_to_me.push(...next.owed_to_me)
    cursor = next.next_cursor
  }
  page.next_cursor = null
  return page
}

export function loadDebtDetail(id: string, opts: { asOf?: string; generation: string }): Promise<DebtDetail> {
  const q = new URLSearchParams({ generation: opts.generation })
  if (opts.asOf) q.set('as_of', opts.asOf)
  return reportedApi<DebtDetail>(`debts/${encodeURIComponent(id)}?` + q)
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
  /** An inflow's recorded incoming type, if any. */
  incoming_type?: string | null
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
  /** An event being corrected: what it claims is offered as open again. */
  replacing?: string
  limit?: number
}): Promise<AllocationCandidatesPage> {
  const q = new URLSearchParams({ debt: opts.debtId, generation: opts.generation, limit: String(opts.limit ?? 50) })
  if (opts.eventKind) q.set('event_kind', opts.eventKind)
  if (opts.search) q.set('search', opts.search)
  if (opts.anchor) q.set('anchor', opts.anchor)
  if (opts.replacing) q.set('replacing', opts.replacing)
  return reportedApi<AllocationCandidatesPage>('debts/allocation-candidates?' + q)
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

/** Rename a debt or give it a due date (or clear one). Due dates put it into the forecast. */
export function detailsCommand(opts: {
  debtId: string
  generation: string
  counterpartyLabel: string
  dueOn: string | null
  reason: string
}): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: opts.generation,
    kind: 'PERSONAL_DEBT_DETAILS_SET',
    anchors: { debt: opts.debtId },
    payload: { counterparty_label: opts.counterpartyLabel, due_on: opts.dueOn, reason: opts.reason },
  })
}

export const RECONCILIATION_LABEL: Record<string, string> = {
  no_events: 'No events yet',
  opening_balance_only: 'Opening balance only — historical funding undisclosed',
  unverified: 'Unverified — a source no longer resolves',
  reconciled: 'Reconciled against source transactions',
}

// --- splitting a bill ------------------------------------------------------------

export type SplitCandidatesPage = { generation: string; candidates: AllocationCandidate[]; next_cursor: string | null }

/** Outgoing bills that can still be split: an unclaimed amount and no owner share recorded yet. */
export function loadSplitCandidates(opts: { generation: string; search?: string; limit?: number }): Promise<SplitCandidatesPage> {
  const q = new URLSearchParams({ generation: opts.generation, limit: String(opts.limit ?? 30) })
  if (opts.search) q.set('search', opts.search)
  return reportedApi<SplitCandidatesPage>('debts/split-candidates?' + q)
}

/** One other person's share: an existing receivable, or someone new (`debtId`
 * null) whose receivable is opened in the same batch. */
export type SplitShare = { debtId: string | null; name: string; amount: string }

/** The draft for a split: a receivable for each new person, an
 * EXPENSE_PAID_FOR_OTHER per share backed by the bill, and the owner's own
 * share as the bill's residual. Its amount is left to the server, which
 * records whatever the shares leave of the bill (or nothing, when they cover
 * it) -- see `previewDebtDraft`. */
export function splitItems(
  bill: AllocationCandidate,
  shares: SplitShare[],
  category: string,
  reason: string,
): DraftItem[] {
  const day = bill.effective_at.slice(0, 10)
  const items: DraftItem[] = []
  for (const share of shares) {
    let debtId = share.debtId
    if (debtId === null) {
      debtId = crypto.randomUUID()
      items.push({
        kind: 'PERSONAL_DEBT_OPEN', anchors: { debt: debtId },
        payload: { direction: 'RECEIVABLE', currency: bill.currency, counterparty_label: share.name.trim(), opened_on: day, due_on: null, reason },
      })
    }
    items.push({
      kind: 'PERSONAL_DEBT_EVENT_SET', anchors: { debt: debtId, event: crypto.randomUUID() },
      payload: {
        event_kind: 'EXPENSE_PAID_FOR_OTHER', direction: 'RECEIVABLE', principal_amount: share.amount.trim(), effective_on: day,
        entry_allocations: [{ entry_anchor: bill.entry_anchor, principal_amount: share.amount.trim(), interest_amount: '0', fee_amount: '0', separate_fee_amount: '0' }],
        opening_balance: false, expense_category: null, reason,
      },
    })
  }
  items.push({ kind: 'ENTRY_RESIDUAL_SET', anchors: { entry: bill.entry_anchor }, payload: { expense_amount: null, expense_category: category, reason } })
  return items
}

// --- drafts checked by the server --------------------------------------------

export type DraftItem = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }

export type TimelineEvent = {
  event_id: string
  event_kind: string
  effective_on: string
  signed: number
  principal: string
  /** The principal standing after this event. */
  balance: string
}

export type DraftDebt = {
  debt_id: string
  counterparty_label: string
  direction: 'PAYABLE' | 'RECEIVABLE'
  currency: string
  before: { status: string; closed_on: string | null; remaining: string } | null
  after: { status: string; closed_on: string | null; remaining: string }
  events: (TimelineEvent & { change: 'added' | 'changed' | 'balance' | null; balance_before: string | null })[]
  removed: TimelineEvent[]
  schedule?: { before: PlanState | null; after: PlanState | null }
}

export type DraftPreview = { generation: string; items: DraftItem[]; debts: DraftDebt[] }

/** Check a draft as one batch and see what it does to every later balance.
 * `items` comes back exactly as it should be saved, with any amount the draft
 * left null completed by the server. Nothing is recorded. */
export function previewDebtDraft(generation: string, items: DraftItem[]): Promise<DraftPreview> {
  return api<DraftPreview>('debts/preview', { expected_generation: generation, items })
}

export type DebtEventPayload = {
  event_id: string
  event_kind: string
  direction: 'PAYABLE' | 'RECEIVABLE'
  principal_amount: string
  effective_on: string
  entry_allocations: AllocationInput[]
  opening_balance: boolean
  expense_category: string | null
  reason: string
}

export type DebtEventRead = { generation: string; debt_id: string; event: DebtEventPayload; sources: AllocationCandidate[] }

/** One recorded event as its payload, with its sources described as candidates. */
export function loadDebtEvent(debtId: string, eventId: string, generation: string): Promise<DebtEventRead> {
  const q = new URLSearchParams({ generation })
  return api<DebtEventRead>(`debts/${encodeURIComponent(debtId)}/events/${encodeURIComponent(eventId)}?` + q)
}

/** A closed debt is reopened around a correction and, unless the user says
 * otherwise, closed again on `recloseOn` in the same record. */
export function aroundClosed(
  debt: { debt_id: string; status: string },
  items: DraftItem[],
  opts: { reopenOn: string; recloseOn: string | null; reason: string },
): DraftItem[] {
  if (debt.status !== 'CLOSED') return items
  const lifecycle = (kind: string, on: string): DraftItem => ({ kind, anchors: { debt: debt.debt_id }, payload: { effective_on: on, reason: opts.reason } })
  return [
    lifecycle('PERSONAL_DEBT_REOPEN', opts.reopenOn),
    ...items,
    ...(opts.recloseOn ? [lifecycle('PERSONAL_DEBT_CLOSE', opts.recloseOn)] : []),
  ]
}

export function kindLabel(kind: string): string {
  return [...EVENT_KINDS.PAYABLE, ...EVENT_KINDS.RECEIVABLE].find(spec => spec.kind === kind)?.label ?? kind
}

/** The draft that makes a recurring commitment the debt's repayment plan and
 * ends the commitment; the server works out every row and amount. */
export function loadCommitmentSchedule(debtId: string, commitment: string, generation: string): Promise<{ generation: string; items: DraftItem[] }> {
  const q = new URLSearchParams({ commitment, generation })
  return api(`debts/${encodeURIComponent(debtId)}/commitment-schedule?` + q)
}

export const PLAN_STATE_LABEL: Record<PlanRowState, string> = {
  paid: 'Paid', part_paid: 'Part paid', overdue: 'Overdue', upcoming: 'Upcoming', trimmed: 'Not needed',
}

export type PlanCadence = 'monthly' | 'weekly' | 'fortnightly'

/** The cadence in the shape the server expects, anchored on the first payment's date. */
export function cadenceSchedule(cadence: PlanCadence, start: string): Record<string, unknown> {
  if (cadence === 'monthly') return { kind: 'monthly', day: Number(start.slice(8, 10)), anchor_on: start }
  return { kind: 'interval_weeks', weeks: cadence === 'weekly' ? 1 : 2, anchor_on: start }
}
