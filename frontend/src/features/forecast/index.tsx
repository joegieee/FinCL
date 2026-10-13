import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ChartFrame } from '@/components/ChartFrame'
import { ApiError, money } from '@/lib/api'
import { CHART_AXIS_TICK, CHART_GRID_STROKE, CHART_SEMANTIC, CHART_TOOLTIP_STYLE, currencyTick } from '@/lib/chart-theme'
import { ConsolidatedMoney, Money } from '@/components/Money'
import type { ForecastCase, ForecastCurrency, ForecastPage, Milestone } from './api'
import {
  type CombinedMinimumOut, type ForecastPageOut, type ReportedForecastCurrencyOut,
  type ReportedForecastEventOut, type ReportedForecastScenarioOut, type ReportedMonthRowOut,
  type ReportingPolicySummary, type ValuedMoneyDTO,
  loadReportedForecast, loadReportingPolicy, nativeForecast,
} from './reportedApi'

function monthLabel(month: string) {
  return new Date(`${month}-01T12:00:00`).toLocaleDateString('en-SG', { month: 'short', year: 'numeric' })
}

function timingLabel(milestone: Milestone): string {
  if (!milestone.occurs) return 'No breach in this horizon'
  if (milestone.precision === 'window') return `Conditional window: ${milestone.window.earliest} → ${milestone.window.latest}`
  if (milestone.precision === 'unresolved') return 'Conditional timing: unresolved'
  return milestone.preceded_by_inferred_timing ? `Conditional date: ${milestone.on}` : milestone.on ?? 'Timing unavailable'
}

function eventTimingLabel(event: ForecastCase['events']['items'][number]): string {
  const timing = event.timing
  const placement = timing.precision === 'window'
    ? `Conditional window: ${timing.earliest} → ${timing.latest}`
    : timing.precision === 'unresolved'
      ? 'Conditional timing: unresolved'
      : timing.earliest ?? 'Timing unavailable'
  return event.conditional ? `${placement} · conditional on ${event.condition}` : placement
}

function gap(reason: string) {
  return <Card className="forecast-gap"><AlertTriangle size={20} /><div><strong>Forecast gap</strong><p>{reason}</p></div></Card>
}

/** A display-only `Number()` read of an already-computed exact reported
 * decimal (mirrors `TrendChart.tsx`'s own convention) -- `null` when the
 * amount could not be priced, so the chart shows a gap, never a fabricated 0. */
function chartValue(value: ValuedMoneyDTO): number | null {
  return value.reported ? Number(value.reported.value) : null
}

type StaleBanner = 'none' | 'same-scope' | 'refresh-required'

/**
 * M4 step 14. Every reported figure below comes from the read-only
 * `GET /api/v1/reporting/forecast` timeline; native supporting detail
 * (cash-cutoff bridge status, milestone dates, action items, provenance,
 * source events) is read straight through from the same response's
 * unchanged `native` field and is never recomputed here.
 */
export function ForecastView() {
  const [page, setPage] = useState<ForecastPageOut | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const lastGood = useRef<{ policyCurrency: string; response: ForecastPageOut } | null>(null)
  const seq = useRef(0)

  const refreshPolicy = useCallback(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])
  useEffect(() => { refreshPolicy() }, [refreshPolicy])

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadReportedForecast()
      .then(response => {
        if (id !== seq.current) return
        lastGood.current = { policyCurrency: response.reporting_currency, response }
        setPage(response); setStaleBanner('none')
      })
      .catch(async caught => {
        if (id !== seq.current) return
        if (caught instanceof ApiError && caught.status === 409) {
          // Forecast has exactly one scope (no history/record filter), so
          // any saved response is automatically "the same scope" -- only a
          // reporting-policy change (checked below) invalidates it.
          const saved = lastGood.current
          if (saved) {
            try {
              const current = await loadReportingPolicy()
              if (id !== seq.current) return
              setPolicy(current)
              if (current.currency === saved.policyCurrency) {
                setPage(saved.response); setStaleBanner('same-scope'); return
              }
            } catch { /* fall through to refresh-required below */ }
          }
          lastGood.current = null
          setPage(null); setStaleBanner('refresh-required')
          return
        }
        setError((caught as Error).message); setPage(null); setStaleBanner('none')
      })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  if (loading && !page && staleBanner === 'none' && !error) {
    return <div className="skeleton-page" aria-label="Loading forecast"><div /><div /><div /></div>
  }

  const reportingCurrency = page?.reporting_currency ?? policy?.currency ?? 'SGD'
  const native = page ? nativeForecast(page) : null
  const reportedByCurrency = page ? Object.fromEntries(page.currencies.map(c => [c.currency, c])) : {}
  const currencies = native ? Object.entries(native.by_currency) : []
  const usableCurrencies = currencies.filter((entry): entry is [string, ForecastCurrency] => entry[1].available)

  return <div className="forecast-view" aria-busy={loading}>
    <div className="scope-caption">
      <span>Reporting in {reportingCurrency}{native?.horizon ? ` · forecast from the supported cutoff ${native.horizon.supported_cash_cutoff} through ${native.horizon.end}` : ''} · estimates never enter recorded totals</span>
      <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
    </div>

    {error && <div className="alert error" role="alert"><div><strong>We couldn’t load the forecast.</strong><p>{error}</p></div><Button variant="outline" onClick={load}>Try again</Button></div>}

    {staleBanner === 'refresh-required' && (
      <div className="alert" role="alert">
        <div><strong>Refresh required.</strong><p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this forecast.</p></div>
        <Button variant="outline" onClick={load}>Refresh</Button>
      </div>
    )}
    {staleBanner === 'same-scope' && page && (
      <div className="alert" role="status">
        <div><strong>Showing the last loaded values for this exact scope.</strong><p>New ledger inputs are available. Refresh to recompute.</p></div>
        <Button variant="outline" onClick={load}>Refresh</Button>
      </div>
    )}

    {native && page && <>
      {!usableCurrencies.length && !native.available && native.reason && gap(native.reason)}
      <CombinedTotalsCard page={page} />
      {currencies.map(([currency, cell]) => cell.available
        ? <CurrencyForecast key={currency} currency={currency} native={cell} reported={reportedByCurrency[currency]} currencyLabel={reportingCurrency} />
        : <div key={currency}>{gap(`${currency}: ${cell.reason}`)}</div>)}
      {currencies.length === 0 && gap(native.reason ?? 'No currency has a supported forecast input.')}
      <ForecastActions items={native.action_items ?? []} />
      {native.limitations.length > 0 && <Card className="forecast-card"><div className="panel-title"><div><p className="eyebrow">LIMITATIONS</p><h2>Named forecast gaps</h2></div></div><ul className="issue-list">{native.limitations.map((item, i) => <li key={i}><p>{item}</p></li>)}</ul></Card>}
      <AssumptionsAndSources native={native} reported={page.currencies} currency={reportingCurrency} />
    </>}
  </div>
}

function CombinedTotalsCard({ page }: { page: ForecastPageOut }) {
  const currency = page.reporting_currency
  return <Card className="forecast-card">
    <div className="panel-title"><div><p className="eyebrow">COMBINED ACROSS EVERY POCKET · {currency}</p><h2>Six-month closing and worst point</h2></div></div>
    <div className="forecast-totals">
      <div><small>Base closing</small><div><ConsolidatedMoney total={page.totals.base_closing.total} partialTotal={page.totals.base_closing.partial_total} currency={currency} unavailableReasons={page.totals.base_closing.unavailable_reasons} excludedDetail={page.totals.base_closing.excluded_detail} /></div></div>
      <div><small>Conservative closing</small><div><ConsolidatedMoney total={page.totals.conservative_closing.total} partialTotal={page.totals.conservative_closing.partial_total} currency={currency} unavailableReasons={page.totals.conservative_closing.unavailable_reasons} excludedDetail={page.totals.conservative_closing.excluded_detail} /></div></div>
      <CombinedMinimumCell label="Base combined minimum" minimum={page.totals.base_combined_minimum} currency={currency} />
      <CombinedMinimumCell label="Conservative combined minimum" minimum={page.totals.conservative_combined_minimum} currency={currency} />
    </div>
    <p className="muted-note">
      Held at one issue-date rate for every pocket, valued as of {page.valuation_as_of}. The combined minimum is the
      exact sum of every pocket's own worst point on the <em>same</em> calendar day, minimised across the horizon —
      never a sum of each pocket's own independently timed low. It is withheld unless every pocket in this ledger has
      both a supported forecast and an importable rate.
    </p>
  </Card>
}

function CombinedMinimumCell({ label, minimum, currency }: { label: string; minimum: CombinedMinimumOut; currency: string }) {
  return <div>
    <small>{label}{minimum.month ? ` · ${monthLabel(minimum.month)}` : ''}</small>
    <div><ConsolidatedMoney total={minimum.amount.total} partialTotal={minimum.amount.partial_total} currency={currency} unavailableReasons={minimum.amount.unavailable_reasons} excludedDetail={minimum.amount.excluded_detail} /></div>
  </div>
}

function ForecastActions({ items }: { items: NonNullable<ForecastPage['action_items']> }) {
  if (!items.length) return null
  return <Card className="forecast-card"><div className="panel-title"><div><p className="eyebrow">FIX NEXT</p><h2>Actions for this forecast</h2></div></div><ul className="issue-list">{items.map(item => <li key={`${item.output}:${item.currency ?? ''}:${item.detail}`}><strong>{item.action}</strong>{item.currency ? ` · ${item.currency}` : ''}<p><Badge variant="outline">{item.kind === 'needs_review' ? 'Needs review' : item.kind === 'missing_evidence' ? 'Missing evidence' : 'Software limitation'}</Badge> {item.detail}</p></li>)}</ul></Card>
}

function CurrencyForecast({ currency, native, reported, currencyLabel }: {
  currency: string
  native: ForecastCurrency
  reported: ReportedForecastCurrencyOut | undefined
  currencyLabel: string
}) {
  const cutoff = native.cash_cutoff
  return <>
    <Card className="forecast-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">SIX-MONTH CASH TIMELINE · {currency}</p>
          <h2>Opening {money(native.starting_balance, currency)} as of {native.starting_balance_as_of}</h2>
        </div>
        <Badge variant="outline">{native.forecast_kind === 'spending-only stress view' ? 'Spending-only stress view' : 'Projected'}</Badge>
      </div>
      {reported && currency !== currencyLabel && (
        <p className="muted-note">Reported: <Money value={reported.starting_balance} /></p>
      )}
      <p className="muted-note">Both cases use the same opening cash. A partial month shows only modelled days within the horizon; each next row opens at the prior row’s closing balance.</p>
      {cutoff?.bridge_status === 'partial' && <p className="muted-note">Opening cash is supported through {cutoff.observation_as_of ?? native.starting_balance_as_of}{cutoff.stale_days !== null && cutoff.stale_days !== undefined ? ` (${cutoff.stale_days} stale days)` : ''}; movement after that date is incomplete.</p>}
      {cutoff?.missing_intervals.length ? <p className="muted-note">Missing movement interval: {cutoff.missing_intervals.map(interval => `${interval.start} → ${interval.end}`).join(', ')}.</p> : null}
    </Card>
    <BreachBanner base={native.base} conservative={native.conservative} />
    <Card className="forecast-card"><div className="panel-title"><div><p className="eyebrow">MONTHLY CONTINUITY · {currencyLabel}</p><h2>Both cases, side by side</h2></div></div>{reported ? <MonthTable base={reported.base} conservative={reported.conservative} /> : <p className="muted-note">Reported monthly detail is unavailable for this pocket.</p>}</Card>
    {reported && <BalanceChart currency={currencyLabel} base={reported.base} conservative={reported.conservative} />}
  </>
}

function BreachBanner({ base, conservative }: { base: ForecastCase; conservative: ForecastCase }) {
  const warnings = [['Base case', base], ['Conservative case', conservative]] as const
  const breaches = warnings.filter(([, scenario]) => scenario.first_zero.occurs || (scenario.first_reserve_breach.available && scenario.first_reserve_breach.occurs))
  if (!breaches.length) return null
  return <div className="alert forecast-breach" role="alert"><AlertTriangle size={19} /><div><strong>Cash-risk breach in the forecast</strong>{breaches.map(([label, scenario]) => <p key={label}><b>{label}:</b> {scenario.first_zero.occurs ? `cash reaches zero or below — ${timingLabel(scenario.first_zero)}.` : scenario.first_reserve_breach.available ? `reserve breach — ${timingLabel(scenario.first_reserve_breach)}.` : 'cash-risk timing is unavailable.'} {scenario.months.filter(row => row.closes_positive_over_an_intra_month_low).map(row => `${monthLabel(row.month)} still closes positive after an intra-month breach.`).join(' ')}</p>)}</div></div>
}

function MonthTable({ base, conservative }: { base: ReportedForecastScenarioOut; conservative: ReportedForecastScenarioOut }) {
  const rows = base.months.map((baseRow, index) => ({ base: baseRow, conservative: conservative.months[index] })).filter((row): row is { base: ReportedMonthRowOut; conservative: ReportedMonthRowOut } => Boolean(row.conservative))
  const banded = rows.some(row => row.base.closing_low && row.base.closing_high)
  return <>
    <table className="pocket-table forecast-table"><thead><tr><th>Month</th><th>Base opening → closing</th><th>Base in / out</th><th>Conservative opening → closing</th><th>Conservative in / out</th><th>Intra-month low</th></tr></thead><tbody>{rows.map(({ base: a, conservative: b }) => <tr key={a.month}><td>{monthLabel(a.month)}</td><td><Money value={a.opening} /> → <Money value={a.closing} />{a.closing_low && a.closing_high && <small className="forecast-band">range <Money value={a.closing_low} /> – <Money value={a.closing_high} /></small>}</td><td><Money value={a.income} /> / <Money value={a.expense} /></td><td><Money value={b.opening} /> → <Money value={b.closing} /></td><td><Money value={b.income} /> / <Money value={b.expense} /></td><td><Money value={b.lowest_balance} /></td></tr>)}</tbody></table>
    {banded && <p className="muted-note">The range under each base closing balance is <strong>observed dispersion</strong>: what the balance would be if every unscheduled category repeated its own cheapest or dearest month from the baseline window. It widens across the horizon because each month’s difference stays in the balance. It is not a confidence interval — a month outside it is possible, it simply has no precedent in what was observed.</p>}
  </>
}

function BalanceChart({ currency, base, conservative }: { currency: string; base: ReportedForecastScenarioOut; conservative: ReportedForecastScenarioOut }) {
  // A conservative case shorter than the base case emits an explicit gap
  // (null), never NaN -- matching TrendChart's gap handling.
  const data = base.months.map((row, index) => {
    const conservativeRow = conservative.months[index]
    return { month: monthLabel(row.month), base: chartValue(row.closing), conservative: conservativeRow ? chartValue(conservativeRow.closing) : null }
  })
  const shorterConservative = conservative.months.length < base.months.length
  return <ChartFrame
    eyebrow="CLOSING BALANCE"
    title="Balance by month"
    ariaLabel={`Projected monthly closing balances in ${currency}; base and conservative cases`}
    footer={shorterConservative ? 'The conservative case ends before the base case; gaps mean unavailable data, not a zero balance.' : 'Gaps mean unavailable data, not a zero balance.'}
    legend={<>
      <span><i style={{ background: CHART_SEMANTIC.base }} />Base case</span>
      <span><i style={{ background: CHART_SEMANTIC.conservative }} />Conservative case</span>
    </>}
    table={<table>
      <thead><tr><th>Month</th><th>Base closing</th><th>Conservative closing</th></tr></thead>
      <tbody>{data.map(row => <tr key={row.month}><td>{row.month}</td><td>{row.base === null ? 'Missing period' : money(String(row.base), currency)}</td><td>{row.conservative === null ? 'Missing period' : money(String(row.conservative), currency)}</td></tr>)}</tbody>
    </table>}
  >
    <div className="forecast-chart">
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" stroke={CHART_GRID_STROKE} />
          <XAxis dataKey="month" tick={CHART_AXIS_TICK} axisLine={{ stroke: CHART_GRID_STROKE }} tickLine={false} />
          <YAxis tick={CHART_AXIS_TICK} axisLine={{ stroke: CHART_GRID_STROKE }} tickLine={false} tickFormatter={currencyTick(currency)} />
          <Tooltip contentStyle={CHART_TOOLTIP_STYLE} formatter={(value, name) => [value === null ? 'Missing period' : money(String(value), currency), name === 'base' ? 'Base case' : 'Conservative case']} />
          <Line type="monotone" dataKey="base" name="base" stroke={CHART_SEMANTIC.base} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
          <Line type="monotone" dataKey="conservative" name="conservative" stroke={CHART_SEMANTIC.conservative} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  </ChartFrame>
}

function AssumptionsAndSources({ native, reported, currency }: { native: ForecastPage; reported: ReportedForecastCurrencyOut[]; currency: string }) {
  if (!native.case_policy) return null
  const casePolicy = native.case_policy
  const events = reported.flatMap(cell => cell.base.events.map((event: ReportedForecastEventOut) => ({ currency: cell.currency, event })))
  return <Card className="forecast-card"><details><summary><span className="eyebrow">ASSUMPTIONS &amp; SOURCES</span><h2>How this estimate was formed</h2></summary><div className="forecast-drill"><p><strong>Base case:</strong> {casePolicy.base}</p><p><strong>Conservative case:</strong> {casePolicy.conservative}</p><p><strong>Upper-spend assumption:</strong> observed stress case — it uses observed spending, not a percentile or confidence interval.</p>{casePolicy.strong_income_interruption && <p><strong>Income interruption:</strong> stress case, not the expected outcome. {casePolicy.strong_income_interruption_note}</p>}{Object.entries(native.metadata ?? {}).map(([label, detail]) => <p key={label}><strong>{label.replaceAll('_', ' ')}:</strong> {detail}</p>)}<h3>Source adapters</h3><ul className="issue-list">{(native.provenance ?? []).map(source => <li key={source.input}><strong>{source.input}</strong> · {source.available ? 'available' : 'unavailable'}<p>{source.reason ?? `${source.module}; used for ${source.consumed_by.join(', ')}`}</p></li>)}</ul><h3>Modelled source events · base case · {currency}</h3>{events.length ? <ul className="issue-list">{events.map(({ currency: native_currency, event }) => <li key={`${native_currency}:${event.event_id}`}><strong><Money value={event.amount} /></strong><p>source {event.source_id} · {native_currency}</p></li>)}</ul> : <p>There are no safely placed source events to display. That is a named input limitation, not a zero event.</p>}</div></details></Card>
}

// Takes the already-fetched native page from OverviewActionPanel's own load
// instead of issuing a second `loadForecast()` for the same data
// (PLAN-ui-cohesion.md §1.5). Deliberately native, single-currency and
// unconverted -- an unrelated glance widget, not the Forecast workspace.
export function ForecastEstimateSummary({ page, onOpen }: { page: ForecastPage | null; onOpen: () => void }) {
  if (!page) return null
  const first = Object.entries(page.by_currency).find((entry): entry is [string, ForecastCurrency] => entry[1].available)
  return <button className="insight forecast-summary" onClick={onOpen}><span className="insight-icon sand"><AlertTriangle size={18} /></span><div><h3>Six-month forecast</h3><p>{first ? `${first[0]} conservative closing: ${money(first[1].conservative.closing_balance, first[0])}. Estimate only — open Forecast for cases and assumptions.` : `Forecast gap: ${page.reason ?? 'inputs are unavailable.'}`}</p></div><span>Forecast →</span></button>
}
