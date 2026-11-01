import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight, CheckCircle2, CircleHelp } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ApiError, type Dashboard } from '@/lib/api'
import { loadBaselineEstimate, loadBaselineReadiness, type BaselineEstimate, type BaselineReadiness, type CurrencyEstimate } from '@/features/baseline/api'
import { loadCashPosition, type CashPosition } from '@/features/cash/api'
import { loadForecast, type ForecastCurrency, type ForecastPage } from '@/features/forecast/api'
import { reportedMoney } from '@/lib/workspaceReporting'
import { MoneyJourneyPanel } from '@/features/journey'
import { loadRecurring, type RecurringPage } from '@/features/planning/api'
import { loadImports, type ImportsPage } from '@/features/setup/api'
import { dateLabel } from '@/lib/format'
import { readerLimitations } from '@/lib/limitations'
import { Cv, MoneyFlowPanel } from './MoneyFlowCards'
import { ReportingPolicySettings } from './ReportingPolicySettings'
import {
  type MoneyFlowOut, type OverviewFilters, type ReportingPolicySummary,
  emptyOverviewFilters, loadMoneyFlow, loadReportingPolicy, overviewScopeKey,
} from './reportedApi'

export type OverviewDestination =
  | 'Setup' | 'Accounts' | 'Baseline' | 'Cash' | 'Forecast' | 'Controls' | 'Cash Flow' | 'Categorization'
  | 'Obligations' | 'Debts' | 'Will I have enough'
  | 'Planning' | 'Money Owed' | 'Net worth' | 'Analysis' | 'Transactions' | 'Income'

type Snapshot = {
  imports?: ImportsPage
  readiness?: BaselineReadiness
  estimate?: BaselineEstimate
  cash?: CashPosition
  forecast?: ForecastPage
  recurring?: RecurringPage
}

type StaleBanner = 'none' | 'same-scope' | 'refresh-required'
type LastGood = { key: string; policyCurrency: string; response: MoneyFlowOut }

const unavailable = 'Evidence not available yet'

/**
 * The Overview tab (M3b.3 step 10, plan §6.3, §3.3-3.4). One
 * `GET /api/v1/money-flow` read over *all* pockets by default drives the
 * reported figures; reporting currency is shown as a setting via
 * `ReportingPolicySettings`, never inferred from — or narrowed by — the
 * legacy per-currency Dashboard `data` this component also still reads for
 * its unrelated, still-native setup checklist and position glance below.
 */
export function OverviewView({
  data, start, end, initialPocketCurrency, onOpen,
}: {
  data: Dashboard
  start: string
  end: string
  // §6.3: a legacy `?currency=USD` URL seeds this screen's own explicit,
  // clearable Account-currency filter once, on first mount only — never a
  // reporting-currency change.
  initialPocketCurrency: string
  onOpen: (destination: OverviewDestination) => void
}) {
  const [filters, setFilters] = useState<OverviewFilters>(() => ({ ...emptyOverviewFilters, pocketCurrency: initialPocketCurrency }))
  const [page, setPage] = useState<MoneyFlowOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const lastGood = useRef<LastGood | null>(null)
  const seq = useRef(0)

  const refreshPolicy = useCallback(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])
  useEffect(() => { refreshPolicy() }, [refreshPolicy])

  const fetchMoneyFlow = useCallback(() => {
    const id = ++seq.current
    const key = overviewScopeKey({ historyStart: start, historyEnd: end, filters })
    setLoading(true); setError('')
    loadMoneyFlow({ historyStart: start, historyEnd: end, filters })
      .then(response => {
        if (id !== seq.current) return
        lastGood.current = { key, policyCurrency: response.reporting_currency, response }
        setPage(response); setStaleBanner('none')
      })
      .catch(async caught => {
        if (id !== seq.current) return
        if (caught instanceof ApiError && caught.status === 409) {
          const saved = lastGood.current
          // Same requested scope: reuse the saved response's own provenance,
          // never a different scope's values (§6.2 item 6).
          if (saved && saved.key === key) {
            try {
              const current = await loadReportingPolicy()
              if (id !== seq.current) return
              setPolicy(current)
              // A reporting-policy change invalidates even a same-scope
              // saved response (§6.3: "clear last-good values on policy change").
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
  }, [start, end, filters])

  useEffect(() => { fetchMoneyFlow() }, [fetchMoneyFlow])

  const limitations = readerLimitations(page?.limitations ?? [])

  return (
    <div aria-busy={loading}>
      <div className="scope-caption overview-reporting-caption">
        <ReportingPolicySettings
          generation={data.generation} policy={policy}
          onChanged={() => { refreshPolicy(); fetchMoneyFlow() }}
        />
        <Button variant="ghost" size="sm" disabled={loading} onClick={fetchMoneyFlow}>Refresh</Button>
      </div>

      {filters.pocketCurrency && (
        <p className="analysis-scope-note">
          Showing only {filters.pocketCurrency} pockets.{' '}
          <button className="linkish" onClick={() => setFilters(emptyOverviewFilters)}>Show all pockets</button>
        </p>
      )}

      {error && (
        <div className="alert error" role="alert">
          <div><strong>Couldn’t load the money-flow summary.</strong><p>{error}</p></div>
          <Button variant="outline" onClick={fetchMoneyFlow}>Try again</Button>
        </div>
      )}

      {staleBanner === 'refresh-required' && (
        <div className="alert" role="alert">
          <div>
            <strong>Refresh required.</strong>
            <p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this summary.</p>
          </div>
          <Button variant="outline" onClick={fetchMoneyFlow}>Refresh</Button>
        </div>
      )}
      {staleBanner === 'same-scope' && page && (
        <div className="alert" role="status">
          <div>
            <strong>Showing the last loaded values for this exact period.</strong>
            <p>New ledger inputs are available. Refresh to recompute.</p>
          </div>
          <Button variant="outline" onClick={fetchMoneyFlow}>Refresh</Button>
        </div>
      )}

      <MoneyJourneyPanel
        generation={data.generation} onOpen={onOpen}
        cash={page && <div className="ov-stat">
          <button className="linkish" onClick={() => onOpen('Cash')}>Cash on {page.summary.cash_at_cutoff.balance_as_of ? dateLabel(page.summary.cash_at_cutoff.balance_as_of) : 'an unknown date'} <ArrowRight size={13} /></button>
          <strong><Cv result={page.summary.cash_at_cutoff.amount} currency={page.reporting_currency} /></strong>
        </div>}
      >
        {page && <MoneyFlowPanel page={page} onOpen={() => onOpen('Analysis')} />}
      </MoneyJourneyPanel>

      {limitations.length > 0 && (
        <details className="ov-more">
          <summary>{limitations.length} limit{limitations.length === 1 ? '' : 's'} on these figures</summary>
          <ul>{limitations.map(text => <li key={text}>{text}</li>)}</ul>
        </details>
      )}

      <OverviewActionPanel data={data} onOpen={onOpen} />
    </div>
  )
}

export function OverviewActionPanel({ data, onOpen }: { data: Dashboard; onOpen: (destination: OverviewDestination) => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot>({})
  const sequence = useRef(0)

  const load = useCallback(() => {
    const id = ++sequence.current
    void Promise.allSettled([
      loadImports(), loadBaselineReadiness(), loadBaselineEstimate(), loadCashPosition(), loadForecast(), loadRecurring(),
    ]).then(results => {
      if (id !== sequence.current) return
      const value = <T,>(index: number) => results[index].status === 'fulfilled' ? results[index].value as T : undefined
      setSnapshot({
        imports: value<ImportsPage>(0), readiness: value<BaselineReadiness>(1), estimate: value<BaselineEstimate>(2), cash: value<CashPosition>(3),
        forecast: value<ForecastPage>(4), recurring: value<RecurringPage>(5),
      })
    })
  }, [])
  useEffect(() => { load() }, [load, data.generation])

  const reserve = reserveText(snapshot.estimate)
  const risk = riskText(snapshot.forecast)
  const imports = snapshot.imports
  const readiness = snapshot.readiness
  const estimate = snapshot.estimate
  const recurring = snapshot.recurring

  const steps = [
    { done: !!imports && imports.imports.length > 0, title: 'Import statements', detail: !imports ? unavailable : imports.imports.length === 0 ? 'No statements imported.' : `${imports.imports.length} import record${imports.imports.length === 1 ? '' : 's'} retained.`, open: 'Setup' as const },
    { done: !!readiness && readiness.pockets.length > 0 && readiness.tasks.length === 0, title: 'Inspect account and month readiness', detail: !readiness ? unavailable : readiness.pockets.length === 0 ? 'No account or month evidence yet.' : readiness.tasks.length ? `${readiness.tasks.length} readiness item${readiness.tasks.length === 1 ? '' : 's'} still need attention.` : `${readiness.pockets.length} pocket${readiness.pockets.length === 1 ? '' : 's'} inspected.`, open: readiness?.tasks.some(task => task.kind === 'import-missing') ? 'Setup' as const : 'Baseline' as const },
    { done: data.review_count === 0 && !!recurring && recurring.candidates.length === 0, title: 'Review detected classifications and commitments', detail: !recurring ? unavailable : `${data.review_count} classification${data.review_count === 1 ? '' : 's'} to review · ${recurring.candidates.length} detected commitment candidate${recurring.candidates.length === 1 ? '' : 's'}.`, open: data.review_count > 0 ? 'Categorization' as const : 'Planning' as const },
    { done: estimate?.available === true && snapshot.cash?.current_position_supported === true, title: 'Inspect derived baseline and evidenced cash', detail: baselineAndCashText(estimate, snapshot.cash), open: estimate?.available ? 'Cash' as const : 'Baseline' as const },
    { done: reserve.ready, title: 'Choose a reserve policy', detail: reserve.policyNote, open: 'Controls' as const },
    { done: forecastReady(snapshot.forecast), title: 'Open forecast', detail: risk.detail, open: 'Forecast' as const },
  ]
  const done = steps.filter(step => step.done).length
  if (done === steps.length) return null

  return <details className="ov-more setup-checklist">
    <summary><span className="ov-pips" aria-hidden="true">{steps.map(step => <i key={step.title} className={step.done ? 'is-done' : ''} />)}</span>{done} of {steps.length} setup steps done</summary>
    <ol>{steps.map(step => <ChecklistItem key={step.title} done={step.done} title={step.title} detail={step.detail} onOpen={() => onOpen(step.open)} />)}</ol>
  </details>
}

function ChecklistItem({ done, title, detail, onOpen }: { done: boolean; title: string; detail: string; onOpen: () => void }) {
  return <li><span className={`checklist-state ${done ? 'is-done' : ''}`}>{done ? <CheckCircle2 size={16} /> : <CircleHelp size={16} />}</span><div><strong>{title}</strong><p>{detail}</p></div><Button variant="ghost" size="sm" onClick={onOpen}>Open <ArrowRight size={13} /></Button></li>
}

// Every currency pocket is shown, each in the reporting currency. Pockets are
// listed side by side, never summed: the owners define these per pocket.
function pockets<T>(entries: [string, T | undefined][], render: (cell: T, pocket: string) => string | null): string | null {
  const parts = entries.flatMap(([pocket, cell]) => {
    const text = cell === undefined ? null : render(cell, pocket)
    return text === null ? [] : [entries.length > 1 ? `${text} (${pocket} pocket)` : text]
  })
  return parts.length ? parts.join(' · ') : null
}

function reserveText(estimate: BaselineEstimate | undefined) {
  if (!estimate) return { value: unavailable, note: 'Checking baseline evidence.', policyNote: 'Reserve evidence is still being checked.', ready: false }
  if (!estimate.available) return { value: 'Not derivable', note: estimate.reason, policyNote: 'A reserve policy cannot be grounded until the baseline is derived.', ready: false }
  const reserves = Object.entries(estimate.by_currency).map(([pocket, cell]) => [pocket, cell.reserve] as [string, CurrencyEstimate['reserve'] | undefined])
  if (!reserves.length) return { value: unavailable, note: 'No reserve read is available.', policyNote: 'No reserve read is available.', ready: false }
  const missing = reserves.filter(([, reserve]) => !reserve?.available)
  const value = pockets(reserves, (reserve, pocket) => reserve.available ? reportedMoney(reserve, 'amount', pocket) : null)
  if (value === null) {
    const first = missing[0]?.[1]
    const reason = first && !first.available ? first.reason : 'No pocket has a derivable reserve.'
    return { value: 'Not derivable', note: reason, policyNote: reason, ready: false }
  }
  const note = missing.length ? `Not derivable for ${missing.map(([pocket]) => pocket).join(', ')}.` : reserves.map(([, reserve]) => reserve?.available ? reserve.basis : '').find(Boolean) ?? ''
  return { value, note, policyNote: `Evidence-derived minimum: ${value}. Review how it is used in Controls.`, ready: missing.length === 0 }
}

function riskText(page: ForecastPage | undefined) {
  if (!page) return { value: unavailable, note: 'Checking the forecast.', detail: 'Forecast inputs are still being checked.', warning: false }
  if (!page.available) return { value: 'Forecast unavailable', note: page.reason ?? 'No supported forecast input.', detail: page.reason ?? 'No supported forecast input.', warning: true }
  const usable = Object.values(page.by_currency).filter((item): item is ForecastCurrency => item.available)
  if (!usable.length) return { value: 'Forecast unavailable', note: page.reason ?? 'No currency has a supported forecast.', detail: page.reason ?? 'No currency has a supported forecast.', warning: true }
  const breaches = usable.filter(item => item.base.first_zero.occurs || item.conservative.first_zero.occurs || (item.base.first_reserve_breach.available && item.base.first_reserve_breach.occurs) || (item.conservative.first_reserve_breach.available && item.conservative.first_reserve_breach.occurs))
  return breaches.length ? { value: 'Breach flagged', note: `${breaches.length} forecast currency path${breaches.length === 1 ? '' : 's'} reaches a cash or reserve breach.`, detail: 'Review the flagged cash or reserve breach in the forecast.', warning: true } : { value: 'No breach flagged', note: 'Within the supported forecast horizon.', detail: 'The supported horizon currently has no cash or reserve breach flagged.', warning: false }
}

function baselineAndCashText(estimate: BaselineEstimate | undefined, cash: CashPosition | undefined) {
  if (!estimate || !cash) return unavailable
  if (!estimate.available && !cash.current_position_supported) return `${estimate.reason} Cash is not observed today.`
  if (!estimate.available) return estimate.reason
  if (!cash.current_position_supported) return cash.current_position_reasons[0] ?? 'Cash is not observed today.'
  return 'Derived baseline and observed current cash are available.'
}

function forecastReady(page: ForecastPage | undefined) {
  return !!page?.available && Object.values(page.by_currency).some(item => item.available)
}
