import { useId } from 'react'

// Wire shape of `valuation.ValuedMoneyDTO` (PLAN-reporting-currency-and-money-flow.md
// §3.3, §4.1, §7.2). Hand-typed here rather than imported from the generated
// `lib/schema.d.ts`: no route emits this DTO yet (M3b.1 step 03 is
// reusable-pieces-only; step 04 wires the first real route). Kept aligned
// with `valuation.RateInputDTO`/`ConsolidatedValuationDTO` by hand until
// step 05 replaces every type here with the generated ones.
export type RateInputDTO = {
  base_currency: string
  quote_currency: string
  period_month: string | null
  effective_on: string | null
  observation_kind: string
  source: string
  source_version: string
  inverted: boolean
  // Exact applied ratio as decimal-string integers — never a JS `number`,
  // which silently loses precision above 2**53 (§4.1).
  applied_numerator: string
  applied_denominator: string
  direction: 'direct' | 'inverse'
  // The genuinely imported printed decimal, when one is on file; `null` only
  // when no printed text exists for either direction. For an inverse leg
  // this is the *other* direction's own printed text, never the applied
  // ratio re-rendered as a rounded finite decimal (§4.1).
  imported_rate_text: string | null
}

// Wire shape of `valuation.ExcludedScopeDetailDTO` (§4.1, §4.3): why exactly
// one currency/period scope was excluded, distinct from a flat global reason
// list. Every reason here is a *rate*-availability gap — a caller combining
// this with an evidence-coverage result (missing balance/movement evidence)
// must keep the two lists visibly separate, never merge them into one.
export type ExcludedScopeDetailDTO = {
  currency: string
  period: string | null
  entry_count: number
  reasons: string[]
  remedies: string[]
}

export type UnavailabilityDTO = {
  reason: string
  message: string
  remedy: string
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
  // Not "Current rate": a closed-month POSITION mark is a monthly reference
  // mark, not a live/current-day quote (§4.1) — the label must not imply
  // otherwise.
  current_mark: 'Monthly reference mark',
  held_latest: 'Forward-held rate',
}

// §4.1: a monthly position mark is a reference value for one closed month,
// never a live market quote — shown only for the one basis that could
// otherwise read as "live" ("Monthly reference mark" already says "monthly",
// but the note spells out *why* that matters for a reader skimming the tag).
const BASIS_NOTE: Partial<Record<NonNullable<ValuedMoneyDTO['basis']>, string>> = {
  current_mark: 'A monthly reference value, not a live market quote.',
}

/** §7.2: one accessible `<Money value={valued}>` component. */
export function Money({ value }: { value: ValuedMoneyDTO }) {
  const disclosureId = useId()

  // §7.2: unavailable is "—" plus a *visible* reason and remedy — never a
  // bare dash, and never 0 (§2 invariant 8). The dash is decorative, so the
  // accessible name comes from the reason/message/remedy text beside it.
  if (value.unavailable) {
    return <span className="money money-unavailable">
      <span aria-hidden="true">—</span>
      <span className="money-unavailable-detail">
        <strong>{value.unavailable.reason}</strong>
        <span>{value.unavailable.message}</span>
        <span className="money-remedy">{value.unavailable.remedy}</span>
      </span>
    </span>
  }

  const reported = value.reported
  const isNative = value.basis === 'identity' || reported == null
  const displayed = isNative ? value.native : reported
  const basisLabel = value.basis ? BASIS_LABEL[value.basis] : null
  const basisNote = value.basis ? BASIS_NOTE[value.basis] : null
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
          {basisNote && <div><dt>About this rate</dt><dd>{basisNote}</dd></div>}
          {value.rate_path.map((rate, index) => (
            <div key={index}>
              <dt>Source</dt>
              <dd>
                {rate.source} ({rate.source_version}){rate.direction === 'inverse' ? ' · inverse' : ''}
                {' — applied '}
                {/* An exact ratio, not a rounded finite decimal (§4.1): a
                    reciprocal like 1/0.75 is 4/3 exactly, never "1.33". */}
                {rate.applied_numerator}/{rate.applied_denominator} (exact)
                {rate.imported_rate_text != null && ` · imported as ${rate.imported_rate_text}`}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </details>
  </span>
}

/** A consolidated valuation's total/partial_total (§3.6, §7.2 "Known subtotal"). */
export function ConsolidatedMoney({
  total, partialTotal, currency, unavailableReasons = [], excludedDetail = [],
}: {
  total: MoneyAmountDTO | null
  partialTotal: MoneyAmountDTO | null
  currency: string
  unavailableReasons?: string[]
  // §4.1/§4.3: per-scope reason(s)/remedy(ies), alongside the flat
  // `unavailableReasons` fallback above. Every entry here is a *rate* gap
  // (see `ExcludedScopeDetailDTO`); a caller must render a separate,
  // visibly distinct list for any evidence-coverage gap rather than mixing
  // the two into this prop.
  excludedDetail?: ExcludedScopeDetailDTO[]
}) {
  if (total) {
    return <span className="money">
      <span className="money-amount">{groupDigits(total.value)} {currency}</span>
    </span>
  }
  const excludedDisclosure = excludedDetail.length > 0 && (
    <details className="money-disclosure money-excluded-disclosure">
      <summary><span className="sr-only">Excluded currency/period scopes</span></summary>
      <div className="money-disclosure-panel">
        <dl>
          {excludedDetail.map((detail, index) => (
            <div key={index}>
              <dt>{detail.currency}{detail.period ? ` · ${detail.period}` : ''}</dt>
              <dd>
                {detail.reasons.join(', ')}
                {detail.remedies.length > 0 && (
                  <span className="money-remedy"> {detail.remedies.join(' ')}</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </details>
  )
  if (partialTotal) {
    return <span className="money">
      <span className="money-amount">{groupDigits(partialTotal.value)} {currency}</span>
      <span className="money-basis-tag money-partial-tag">Known subtotal</span>
      {excludedDisclosure}
    </span>
  }
  // An aria-hidden dash on its own would leave a screen reader with no
  // accessible name at all, so the "Not available" text is always rendered.
  return <span className="money money-unavailable">
    <span aria-hidden="true">—</span>
    <span className="money-unavailable-detail">
      <strong>Not available</strong>
      {unavailableReasons.length > 0 && <span>{unavailableReasons.join(', ')}</span>}
    </span>
    {excludedDisclosure}
  </span>
}

/** A reconciliation note for a non-zero `rounding_delta` between children and a once-rounded parent (§3.6, §7.2). */
export function RoundingReconciliationNote({ delta, currency }: { delta: MoneyAmountDTO; currency: string }) {
  return <p className="money-rounding-note">
    Rows are rounded individually for display; the total is rounded once and may
    differ by {groupDigits(delta.value)} {currency}.
  </p>
}
