import { api, type components } from '@/lib/api'

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

export type CoverageRemedy = {
  action: 'record-balance-observation' | 'certify-quiet-interval' | 'import-statement'
  decision_kind: string | null
  account_id: string
  currency_pocket: string
  display_name: string | null
  institution: string | null
  unlocks: 'unblocks-shared-cutoff' | 'adds-a-pocket' | 'brings-forward'
  rank: number
  detail: string
  parameters: { period_start?: string; period_end?: string; suggested_observed_on?: string }
}

export type CoverageReport = {
  generated_for: string
  coverage: {
    included_pockets: number
    covered_pockets: number
    short_pockets: number
    ratio: number
    basis: string
    status: 'complete' | 'partial' | 'unavailable'
  }
  supported_cutoff: string | null
  current_position_supported: boolean
  remedies: CoverageRemedy[]
  partial_position: PartialPosition
  freshness: AccountFreshness[]
}

export type PartialPosition = components['schemas']['PartialPositionOut']

/** How fresh one account's evidence is; see `position/freshness.py`. */
export type AccountFreshness = {
  account: string
  display_name: string
  currencies: string[]
  evidence_through: string | null
  age_days: number | null
  stale: boolean
  basis: 'statement' | 'observation' | 'certification' | null
  holds_back_start: boolean
}

export const loadCoverageReport = () => api<CoverageReport>('position/coverage')

export type MovementCertification = {
  certification: string
  account_id: string
  currency_pocket: string
  display_name: string | null
  institution: string | null
  period_start: string
  period_end: string
  basis: string
}

export const loadCertifications = () =>
  api<{ certifications: MovementCertification[] }>('position/certifications')

/**
 * The three inspections that can back a coverage certification. The operator
 * picks the one they actually performed; it is recorded and shown back, so a
 * certification can be weighed later rather than taken on trust.
 */
export const certificationBases = [
  { value: 'account-app-transaction-list', label: "I checked the account's own transaction list" },
  { value: 'statement-inspected-no-activity', label: 'I inspected a statement showing no activity' },
  { value: 'account-closed-or-dormant', label: 'The account was closed or dormant' },
] as const

export type CertifyCoverageInput = {
  account_id: string
  currency_pocket: string
  period_start: string
  period_end: string
  basis: string
  reason: string
}

/**
 * Attest that a date range holds no movement for one pocket.
 *
 * This supplies *completeness*, never an amount: it lets an existing observed
 * balance carry forward across a quiet range, and can never introduce a balance
 * the ledger never saw.
 */
export function certifyCoverage(
  generation: string,
  input: CertifyCoverageInput,
): Promise<CommandResult> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'MOVEMENT_COVERAGE_CERTIFY',
    anchors: { certification: crypto.randomUUID() },
    payload: { ...input },
  })
}

/** Withdraw a certification; the days it covered become unproven again. */
export function retireCoverage(
  generation: string,
  certification: string,
  reason: string,
): Promise<CommandResult> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'MOVEMENT_COVERAGE_RETIRE',
    anchors: { certification },
    payload: { reason },
  })
}
