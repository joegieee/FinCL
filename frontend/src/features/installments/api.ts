import { api } from '@/lib/api'

export type WireMoney = { value: string; currency: string | null }

export type PlanProgress = {
  known_principal: WireMoney | null
  term_months: number | null
  observed_principal_paid: WireMoney
  scheduled_principal: WireMoney
  settled_principal: WireMoney
  remaining_obligation: WireMoney | null
  occurrence_count: number
  occurrences_bound: number
}

export type PlanShape = {
  plan_id: string
  card_account_id: string | null
  currency: string
  plan_reference: string | null
  merchant_label: string
  opened_on: string
  status: string
  effective_status: string
  interpretation_status: string
  bound: boolean
  dangling_reason: string | null
  progress: PlanProgress
}

export type InstallmentsPage = {
  generation: string
  plans: PlanShape[]
  next_cursor: string | null
}

export type Occurrence = {
  occurrence_id: string
  occurrence_index: number | null
  occurrence_count: number | null
  entry_anchor: string
  effective_on: string
  date_precision: string
  amount: WireMoney
  bound: boolean
}

export type ScheduleRow = {
  schedule_index: number
  due_on: string
  principal: WireMoney
  fee: WireMoney
  rounding_remainder: WireMoney
  is_projected: boolean
}

export type PurchaseAllocation = {
  entry_anchor: string
  component_role: 'principal' | 'fee'
  amount: WireMoney
  bound: boolean
}

export type Settlement = {
  settlement_id: string
  settled_on: string
  source_entry_anchor: string
  principal: WireMoney
  fee: WireMoney
  waiver: WireMoney
  bound: boolean
}

export type InstallmentDetail = {
  generation: string
  as_of: string
  plan: PlanShape
  source_occurrences: Occurrence[]
  purchase_allocations: PurchaseAllocation[]
  schedule: ScheduleRow[]
  settlements: Settlement[]
  views: {
    recognized_purchase_cost: {
      recognized_principal: WireMoney | null
      financing_fee: WireMoney | null
      note: string
    }
    spread_allocation: {
      observed_occurrences: Occurrence[]
      projected_schedule: ScheduleRow[]
      note: string
    }
  }
}

export function loadInstallments(opts: {
  card?: string
  status?: string
  generation: string
}): Promise<InstallmentsPage> {
  const q = new URLSearchParams({ generation: opts.generation, limit: '200' })
  if (opts.card) q.set('card', opts.card)
  if (opts.status) q.set('status', opts.status)
  return api<InstallmentsPage>('installments?' + q)
}

export function loadInstallmentDetail(
  id: string,
  opts: { asOf?: string; generation: string },
): Promise<InstallmentDetail> {
  const q = new URLSearchParams({ generation: opts.generation })
  if (opts.asOf) q.set('as_of', opts.asOf)
  return api<InstallmentDetail>(`installments/${encodeURIComponent(id)}?` + q)
}

// --- candidates: chains the card parser already printed, not yet confirmed ----

export type CandidateOccurrence = {
  entry_anchor: string
  occurrence_index: number
  occurrence_count: number
  effective_on: string
  date_precision: string
  amount: string
  description: string | null
}

export type PlanCandidate = {
  candidate_key: string
  card_account_id: string
  currency: string
  family: string
  term_months: number
  merchant_label: string
  opened_on: string
  first_ordinal: number
  last_ordinal: number
  observed_count: number
  complete: boolean
  observed_total: WireMoney
  known_principal: WireMoney | null
  occurrences: CandidateOccurrence[]
  source: string
}

export type AmbiguousGroup = {
  card_account_id: string
  currency: string
  family: string
  term_months: number
  reason: string
  occurrences: Omit<CandidateOccurrence, 'date_precision'>[]
}

export type CandidatesPage = {
  generation: string
  candidates: PlanCandidate[]
  ambiguous_groups: AmbiguousGroup[]
  claimed_occurrence_count: number
}

export function loadCandidates(opts: { card?: string; generation: string }): Promise<CandidatesPage> {
  const q = new URLSearchParams({ generation: opts.generation })
  if (opts.card) q.set('card', opts.card)
  return api<CandidatesPage>('installments/candidates?' + q)
}

// --- source transactions for attribution and settlement ----------------------

export type EntryCandidate = {
  entry_anchor: string
  effective_at: string
  account_id: string
  currency: string
  /** The entry's full magnitude, for context. */
  amount: string
  /** What is still unclaimed by any domain — the ceiling for a new allocation. */
  open_amount: string
  open_fee: string
  description: string | null
}

export type EntryCandidatesPage = {
  generation: string
  plan_id: string
  purpose: 'purchase' | 'settlement'
  currency: string
  card_account_id: string | null
  candidates: EntryCandidate[]
  next_cursor: string | null
}

export function loadEntryCandidates(opts: {
  planId: string
  purpose: 'purchase' | 'settlement'
  generation: string
  search?: string
  /** Narrow to exactly one transaction (the classification editor's hand-off). */
  anchor?: string
  limit?: number
}): Promise<EntryCandidatesPage> {
  const q = new URLSearchParams({
    plan: opts.planId,
    purpose: opts.purpose,
    generation: opts.generation,
    limit: String(opts.limit ?? 50),
  })
  if (opts.search) q.set('search', opts.search)
  if (opts.anchor) q.set('anchor', opts.anchor)
  return api<EntryCandidatesPage>('installments/entry-candidates?' + q)
}

// --- writes ------------------------------------------------------------------

export type InstallmentCommandBody = {
  request_id: string
  expected_generation: string
  kind: string
  anchors: Record<string, string>
  payload: Record<string, unknown>
}

// Same two-step every other write surface uses: `preview` applies the pending
// decision against the current projection and refuses there, so a refusal costs
// nothing; `commands` is what actually appends to the journal.
export async function previewThenCommit(body: InstallmentCommandBody): Promise<{ status: string }> {
  await api('preview', body)
  return api<{ status: string }>('commands', body)
}

/**
 * Confirm a proposed chain as a plan (INSTALLMENT_PLAN_SET).
 *
 * `known_principal` is sent only when the operator has one: a chain whose every
 * instalment has appeared carries its exact observed total, and anything else
 * stays null. The schedule follows the same rule — the payload validator refuses
 * a schedule without a principal, because an unknown principal cannot imply a
 * future payment.
 */
export function confirmPlan(opts: {
  candidate: PlanCandidate
  generation: string
  merchantLabel: string
  planReference: string | null
  knownPrincipal: string | null
  financingFee: string | null
  reason: string
}): Promise<{ status: string }> {
  const { candidate } = opts
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: opts.generation,
    kind: 'INSTALLMENT_PLAN_SET',
    anchors: { plan: crypto.randomUUID() },
    payload: {
      card_account_id: candidate.card_account_id,
      currency: candidate.currency,
      plan_reference: opts.planReference,
      merchant_label: opts.merchantLabel,
      term_months: candidate.term_months,
      known_principal: opts.knownPrincipal,
      financing_fee: opts.financingFee,
      opened_on: candidate.opened_on,
      status: 'CONFIRMED',
      occurrences: candidate.occurrences.map((o) => ({
        occurrence_id: crypto.randomUUID(),
        occurrence_index: o.occurrence_index,
        occurrence_count: o.occurrence_count,
        entry_anchor: o.entry_anchor,
        effective_on: o.effective_on,
        amount: o.amount,
        date_precision: o.date_precision,
      })),
      schedule: [],
      reason: opts.reason,
    },
  })
}

/** Attribute the original purchase to a plan (INSTALLMENT_ALLOCATION_SET). */
export function allocatePurchase(opts: {
  planId: string
  generation: string
  allocations: { entry_anchor: string; component_role: 'principal' | 'fee'; amount: string }[]
  reason: string
}): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: opts.generation,
    kind: 'INSTALLMENT_ALLOCATION_SET',
    anchors: { plan: opts.planId },
    payload: { allocations: opts.allocations, reason: opts.reason },
  })
}

/** Record paying a plan off early (INSTALLMENT_SETTLEMENT_SET). */
export function settlePlan(opts: {
  planId: string
  generation: string
  settledOn: string
  sourceEntryAnchor: string
  principal: string
  fee: string
  waiver: string
  reason: string
}): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: opts.generation,
    kind: 'INSTALLMENT_SETTLEMENT_SET',
    anchors: { plan: opts.planId, settlement: crypto.randomUUID() },
    payload: {
      settled_on: opts.settledOn,
      source_entry_anchor: opts.sourceEntryAnchor,
      principal_amount: opts.principal,
      fee_amount: opts.fee,
      waiver_amount: opts.waiver,
      reason: opts.reason,
    },
  })
}

/** Retire a plan and its dependent allocations / settlement (INSTALLMENT_PLAN_RETIRE). */
export function retirePlan(opts: {
  planId: string
  generation: string
  reason: string
}): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: opts.generation,
    kind: 'INSTALLMENT_PLAN_RETIRE',
    anchors: { plan: opts.planId },
    payload: { reason: opts.reason },
  })
}
