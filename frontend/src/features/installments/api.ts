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
