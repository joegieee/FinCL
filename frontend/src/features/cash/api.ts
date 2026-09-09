import { api } from '@/lib/api'

export type BoundaryDetail = {
  balance: string
  as_of: string
  basis: string
  observation?: string
  [k: string]: unknown
}

export type CashPocket = {
  account_id: string
  institution: string | null
  display_name: string | null
  source_type: string | null
  currency_pocket: string
  liquid: boolean
  excluded_reason: string | null
  basis: string
  balance: string | null
  as_of: string | null
  date_precision: string | null
  stale: boolean
  staleness_days: number | null
  unavailable_reason: string | null
  statement_running_balance: BoundaryDetail | null
  manual_observation: BoundaryDetail | null
  derived_balance: string | null
  derived_as_of: string | null
  derived_basis: string | null
  coverage_status: 'supported' | 'unsupported'
}

export type CashBridgePocket = {
  account_id: string
  currency_pocket: string
  opening: string
  opening_as_of: string
  external_inflows: string
  external_outflows: string
  owned_transfer_inflows: string
  owned_transfer_outflows: string
  closing: string
  closing_as_of: string
  calculated_closing: string
  reconciled: boolean
  movement_count: number
  paired_transfer_link_count: number
  unsupported_reason: string | null
}

export type CashBridgeCombined = {
  opening: string
  external_inflows: string
  external_outflows: string
  closing: string
  reconciled: boolean
  owned_transfer_pairs_cancelled: number
}

export type MissingInterval = {
  account_id: string
  currency_pocket: string
  start: string
  end: string
  reason: string
}

export type CashPosition = {
  generated_for: string
  stale_after_days: number
  supported_cutoff: string | null
  current_position_supported: boolean
  current_position_reasons: string[]
  included_pockets: CashPocket[]
  pockets: CashPocket[]
  combined_liquid_position: Record<string, string>
  liquid_total: Record<string, string>
  liquid_total_withheld: Record<string, string>
  stale: boolean
  missing_intervals: MissingInterval[]
  cash_bridge: {
    pockets: CashBridgePocket[]
    combined: {
      by_currency: Record<string, CashBridgeCombined>
      owned_transfer_pairs_cancelled: number
      policy: string
    }
  }
}

export const loadCashPosition = () => api<CashPosition>('position')

export type CommandResult = { status: string }

async function previewThenCommit(body: {
  request_id: string
  expected_generation: string
  kind: string
  anchors: Record<string, string>
  payload: Record<string, unknown>
}): Promise<CommandResult> {
  await api('preview', body)
  return api<CommandResult>('commands', body)
}

export type BalanceObservationInput = {
  account_id: string
  currency_pocket: string
  observed_on: string
  balance: string
  reason: string
}

/** Assert a dated balance directly, for when there is no statement to import. */
export function recordBalanceObservation(
  generation: string,
  input: BalanceObservationInput,
): Promise<CommandResult> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'BALANCE_OBSERVATION_SET',
    anchors: { observation: crypto.randomUUID() },
    payload: { ...input },
  })
}

/** Withdraw a previously recorded manual observation; the read path falls back to the next-best basis. */
export function retireBalanceObservation(
  generation: string,
  observationId: string,
  reason: string,
): Promise<CommandResult> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'BALANCE_OBSERVATION_RETIRE',
    anchors: { observation: observationId },
    payload: { reason },
  })
}
