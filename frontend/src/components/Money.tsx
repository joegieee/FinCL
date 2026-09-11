import { useId } from 'react'

// Wire shape of `valuation.ValuedMoneyDTO` (PLAN-reporting-currency-and-money-flow.md
// §3.3, §7.2). Hand-typed here rather than imported from the generated
// `lib/schema.d.ts`: this M2 milestone does not wire a route to emit it yet
// (no regenerated OpenAPI/TS DTOs in this change), and the shape
// is what the backend's pydantic model actually defines.
export type RateInputDTO = {
  base_currency: string
  quote_currency: string
  period_month: string | null
  effective_on: string | null
  observation_kind: string
  source: string
  source_version: string
  inverted: boolean
}

export type UnavailabilityDTO = {
  reason: string
  message: string
}

export type MoneyAmountDTO = {
  value: string
  currency: string
}

export type ValuedMoneyDTO = {
  native: MoneyAmountDTO
  reported: MoneyAmountDTO | null
  purpose: 'flow' | 'position' | 'forecast'
  basis:
    | 'identity' | 'realized' | 'exact_observation' | 'prior_observation'
    | 'monthly_average' | 'monthly_proxy' | 'current_mark' | 'held_latest'
    | null
  valuation_as_of: string
  rate_as_of: string | null
  rate_period: string | null
  rate_path: RateInputDTO[]
  forward_policy: string | null
  unavailable: UnavailabilityDTO | null
}

// Groups formatting digits with thousand separators without assuming a fixed
// number of decimal places — the DTO's `value` is already printed at its own
// currency's minor-unit scale (JPY "1234", KWD "12.340"), so re-padding to
// two places the way `lib/api.ts`'s `money()` does would be wrong here.
function groupDigits(value: string): string {
  const negative = value.startsWith('-')
  const unsigned = value.replace(/^[+-]/, '')
  const [whole, fraction] = unsigned.split('.')
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${negative ? '−' : ''}${grouped}${fraction != null ? `.${fraction}` : ''}`
}

const BASIS_LABEL: Record<NonNullable<ValuedMoneyDTO['basis']>, string> = {
  identity: 'Native currency',
  realized: 'Realized rate',
  exact_observation: 'Dated rate',
  prior_observation: 'Prior dated rate',
  monthly_average: 'Monthly average rate',
  monthly_proxy: 'Monthly proxy rate',
  current_mark: 'Current rate',
  held_latest: 'Forward-held rate',
}

/** §7.2: one accessible `<Money value={valued}>` component. */
export function Money({ value }: { value: ValuedMoneyDTO }) {
  const disclosureId = useId()

  if (value.unavailable) {
    return <span className="money money-unavailable">
      <span aria-hidden="true">—</span>
      <span className="money-unavailable-detail">
        <strong>{value.unavailable.reason}</strong>
        <span>{value.unavailable.message}</span>
      </span>
    </span>
  }

  const reported = value.reported
  const isNative = value.basis === 'identity' || reported == null
  const displayed = isNative ? value.native : reported
  const basisLabel = value.basis ? BASIS_LABEL[value.basis] : null
  const isForward = value.basis === 'held_latest'

  return <span className={`money${isForward ? ' money-forward' : ''}`}>
    <span className="money-amount" data-basis={value.basis ?? undefined}>
      {groupDigits(displayed.value)} {displayed.currency}
    </span>
    {!isNative && basisLabel && <span className="money-basis-tag">{basisLabel}</span>}
    <details className="money-disclosure">
      <summary aria-describedby={disclosureId}>
        <span className="sr-only">Rate and source detail</span>
      </summary>
      <div id={disclosureId} className="money-disclosure-panel">
        <dl>
          <div><dt>Native amount</dt><dd>{groupDigits(value.native.value)} {value.native.currency}</dd></div>
          {value.rate_as_of && <div><dt>Rate date</dt><dd>{value.rate_as_of}</dd></div>}
          {value.rate_period && <div><dt>Rate period</dt><dd>{value.rate_period}</dd></div>}
          {value.rate_path.map((rate, index) => (
            <div key={index}>
              <dt>Source</dt>
              <dd>{rate.source} ({rate.source_version}){rate.inverted ? ' · inverted' : ''}</dd>
            </div>
          ))}
        </dl>
      </div>
    </details>
  </span>
}

/** A consolidated valuation's total/partial_total (§3.6, §7.2 "Known subtotal"). */
export function ConsolidatedMoney({
  total, partialTotal, currency,
}: {
  total: MoneyAmountDTO | null
  partialTotal: MoneyAmountDTO | null
  currency: string
}) {
  if (total) {
    return <span className="money">
      <span className="money-amount">{groupDigits(total.value)} {currency}</span>
    </span>
  }
  if (partialTotal) {
    return <span className="money">
      <span className="money-amount">{groupDigits(partialTotal.value)} {currency}</span>
      <span className="money-basis-tag money-partial-tag">Known subtotal</span>
    </span>
  }
  return <span className="money money-unavailable"><span aria-hidden="true">—</span></span>
}

/** A reconciliation note for a non-zero `rounding_delta` between children and a once-rounded parent (§3.6, §7.2). */
export function RoundingReconciliationNote({ delta, currency }: { delta: MoneyAmountDTO; currency: string }) {
  return <p className="money-rounding-note">
    Rows are rounded individually for display; the total is rounded once and may
    differ by {groupDigits(delta.value)} {currency}.
  </p>
}
