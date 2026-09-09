import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { money } from '@/lib/api'
import { loadForecast, type ForecastCase, type ForecastCurrency, type ForecastPage, type Milestone } from './api'

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

/** Stage 3B presentation only. Every value below comes from the read-only timeline. */
export function ForecastView() {
  const [page, setPage] = useState<ForecastPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)
  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadForecast().then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  if (loading && !page) return <div className="skeleton-page" aria-label="Loading forecast"><div /><div /><div /></div>
  if (error && !page) return <div className="alert error" role="alert"><div><strong>We couldn’t load the forecast.</strong><p>{error}</p></div><Button variant="outline" onClick={load}>Try again</Button></div>
  if (!page) return null
  const currencies = Object.entries(page.by_currency)

  return <div className="forecast-view">
    <div className="scope-caption"><span>{page.horizon ? `Forecast from the supported cutoff ${page.horizon.supported_cash_cutoff} through ${page.horizon.end}` : 'No supported forecast horizon'} · estimates never enter recorded totals</span><Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button></div>
    {!page.available && page.reason && gap(page.reason)}
    {currencies.map(([currency, cell]) => cell.available ? <CurrencyForecast key={currency} currency={currency} forecast={cell} /> : <div key={currency}>{gap(`${currency}: ${cell.reason}`)}</div>)}
    {currencies.length === 0 && gap(page.reason ?? 'No currency has a supported forecast input.')}
    {page.limitations.length > 0 && <Card className="forecast-card"><div className="panel-title"><div><p className="eyebrow">LIMITATIONS</p><h2>Named forecast gaps</h2></div></div><ul className="issue-list">{page.limitations.map((item, i) => <li key={i}><p>{item}</p></li>)}</ul></Card>}
    <AssumptionsAndSources page={page} />
  </div>
}

function CurrencyForecast({ currency, forecast }: { currency: string; forecast: ForecastCurrency }) {
  return <>
    <Card className="forecast-card">
      <div className="panel-title"><div><p className="eyebrow">SIX-MONTH CASH TIMELINE · {currency}</p><h2>Opening {money(forecast.starting_balance, currency)} as of {forecast.starting_balance_as_of}</h2></div><Badge variant="outline">Projected</Badge></div>
      <p className="muted-note">Both cases use the same opening cash. A partial month shows only modelled days within the horizon; each next row opens at the prior row’s closing balance.</p>
    </Card>
    <BreachBanner base={forecast.base} conservative={forecast.conservative} />
    <Card className="forecast-card"><div className="panel-title"><div><p className="eyebrow">MONTHLY CONTINUITY</p><h2>Both cases, side by side</h2></div></div><MonthTable currency={currency} base={forecast.base} conservative={forecast.conservative} /></Card>
    <Card className="forecast-card"><div className="panel-title"><div><p className="eyebrow">CLOSING BALANCE</p><h2>Balance by month</h2></div></div><BalanceChart currency={currency} base={forecast.base} conservative={forecast.conservative} /></Card>
  </>
}

function BreachBanner({ base, conservative }: { base: ForecastCase; conservative: ForecastCase }) {
  const warnings = [['Base case', base], ['Conservative case', conservative]] as const
  const breaches = warnings.filter(([, scenario]) => scenario.first_zero.occurs || (scenario.first_reserve_breach.available && scenario.first_reserve_breach.occurs))
  if (!breaches.length) return null
  return <div className="alert forecast-breach" role="alert"><AlertTriangle size={19} /><div><strong>Cash-risk breach in the forecast</strong>{breaches.map(([label, scenario]) => <p key={label}><b>{label}:</b> {scenario.first_zero.occurs ? `cash reaches zero or below — ${timingLabel(scenario.first_zero)}.` : scenario.first_reserve_breach.available ? `reserve breach — ${timingLabel(scenario.first_reserve_breach)}.` : 'cash-risk timing is unavailable.'} {scenario.months.filter(row => row.closes_positive_over_an_intra_month_low).map(row => `${monthLabel(row.month)} still closes positive after an intra-month breach.`).join(' ')}</p>)}</div></div>
}

function MonthTable({ currency, base, conservative }: { currency: string; base: ForecastCase; conservative: ForecastCase }) {
  const rows = base.months.map((baseRow, index) => ({ base: baseRow, conservative: conservative.months[index] })).filter((row): row is { base: ForecastCase['months'][number]; conservative: ForecastCase['months'][number] } => Boolean(row.conservative))
  return <table className="pocket-table forecast-table"><thead><tr><th>Month</th><th>Base opening → closing</th><th>Base in / out</th><th>Conservative opening → closing</th><th>Conservative in / out</th><th>Intra-month low</th></tr></thead><tbody>{rows.map(({ base: a, conservative: b }) => <tr key={a.month}><td>{monthLabel(a.month)}{a.partial && <small>{a.days_covered}/{a.days_in_month} modelled days</small>}</td><td>{money(a.opening, currency)} → {money(a.closing, currency)}</td><td>{money(a.income, currency)} / {money(a.expense, currency)}</td><td>{money(b.opening, currency)} → {money(b.closing, currency)}</td><td>{money(b.income, currency)} / {money(b.expense, currency)}</td><td>{money(b.lowest_balance.amount, currency)}<small>{b.lowest_balance.precision === 'window' ? `Conditional window: ${b.lowest_balance.window.earliest} → ${b.lowest_balance.window.latest}` : b.lowest_balance.on ?? 'Conditional timing unresolved'}{b.closes_positive_over_an_intra_month_low && ' · closes positive after breach'}</small></td></tr>)}</tbody></table>
}

function BalanceChart({ currency, base, conservative }: { currency: string; base: ForecastCase; conservative: ForecastCase }) {
  const data = base.months.map((row, index) => ({ month: monthLabel(row.month), base: Number(row.closing), conservative: Number(conservative.months[index]?.closing) }))
  return <div className="forecast-chart" role="img" aria-label={`Projected monthly closing balances in ${currency}; base and conservative cases`}><ResponsiveContainer width="100%" height={260}><LineChart data={data}><CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="month" /><YAxis /><Tooltip formatter={(value, name) => [money(String(value), currency), name === 'base' ? 'Base case' : 'Conservative case']} /><Legend formatter={value => value === 'base' ? 'Base case' : 'Conservative case'} /><Line type="monotone" dataKey="base" stroke="#247b69" strokeWidth={2} dot={false} isAnimationActive={false} /><Line type="monotone" dataKey="conservative" stroke="#b55a45" strokeWidth={2} dot={false} isAnimationActive={false} /></LineChart></ResponsiveContainer></div>
}

function AssumptionsAndSources({ page }: { page: ForecastPage }) {
  if (!page.case_policy) return null
  const casePolicy = page.case_policy
  const events = Object.entries(page.by_currency).flatMap(([currency, cell]) => cell.available ? cell.base.events.items.map(event => ({ currency, event })) : [])
  return <Card className="forecast-card"><details><summary><span className="eyebrow">ASSUMPTIONS &amp; SOURCES</span><h2>How this estimate was formed</h2></summary><div className="forecast-drill"><p><strong>Base case:</strong> {casePolicy.base}</p><p><strong>Conservative case:</strong> {casePolicy.conservative}</p><p><strong>Upper-spend assumption:</strong> observed stress case — it uses observed spending, not a percentile or confidence interval.</p>{casePolicy.strong_income_interruption && <p><strong>Income interruption:</strong> stress case, not the expected outcome. {casePolicy.strong_income_interruption_note}</p>}{Object.entries(page.metadata ?? {}).map(([label, detail]) => <p key={label}><strong>{label.replaceAll('_', ' ')}:</strong> {detail}</p>)}<h3>Source adapters</h3><ul className="issue-list">{(page.provenance ?? []).map(source => <li key={source.input}><strong>{source.input}</strong> · {source.available ? 'available' : 'unavailable'}<p>{source.reason ?? `${source.module}; used for ${source.consumed_by.join(', ')}`}</p></li>)}</ul><h3>Modelled source events · base case</h3>{events.length ? <ul className="issue-list">{events.map(({ currency, event }) => <li key={`${currency}:${event.event_id}`}><strong>{event.label}</strong> · {money(event.amount, currency)}<p>{eventTimingLabel(event)} · source {event.source_id}</p></li>)}</ul> : <p>There are no safely placed source events to display. That is a named input limitation, not a zero event.</p>}</div></details></Card>
}

export function ForecastEstimateSummary({ onOpen }: { onOpen: () => void }) {
  const [page, setPage] = useState<ForecastPage | null>(null)
  useEffect(() => { void loadForecast().then(setPage).catch(() => setPage(null)) }, [])
  if (!page) return null
  const first = Object.entries(page.by_currency).find((entry): entry is [string, ForecastCurrency] => entry[1].available)
  return <button className="insight forecast-summary" onClick={onOpen}><span className="insight-icon sand"><AlertTriangle size={18} /></span><div><h3>Six-month forecast</h3><p>{first ? `${first[0]} conservative closing: ${money(first[1].conservative.closing_balance, first[0])}. Estimate only — open Forecast for cases and assumptions.` : `Forecast gap: ${page.reason ?? 'inputs are unavailable.'}`}</p></div><span>Forecast →</span></button>
}
