import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ApiError, money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { humanize } from '@/lib/limitations'
import { ConsolidatedMoney, Money } from '@/components/Money'
import { NetWorthCharts } from './NetWorthCharts'
import {
  type NativeCurrencySummaryOut, type NetWorthComponentOut, type NetWorthPageOut,
  type NetWorthTotalsOut, type ReportingPolicySummary,
  loadReportedNetWorth, loadReportingPolicy, netWorthScopeKey,
} from './reportedApi'

const KIND_LABEL: Record<string, string> = {
  cash: 'Cash position',
  debt: 'Debt / receivable',
  card_liability: 'Card liability',
}

type Scope = { asOf: string; priorAsOf: string }
type StaleBanner = 'none' | 'same-scope' | 'refresh-required'
type LastGood = { key: string; policyCurrency: string; response: NetWorthPageOut }

/**
 * Cash-flow framework Stage 5B — tracked net worth (M4 step 13, plan §3.3).
 *
 * Read-only. Every native component (kind, side, currency, amount, as-of
 * date, basis, inclusion, exclusion) is exactly as before — one authority
 * per contributing figure, so an installment already inside a card statement
 * or a manual debt linked to a tracked card still cannot be counted twice.
 * This step adds a reported equivalent to each priceable component, each
 * valued at its own explicit as-of date, then combined into one reporting
 * -currency total shown *beside* the unchanged native per-currency subtotals.
 * Components the ledger cannot value are listed as missing, never as zero;
 * manual values keep their own basis and stay out of the tracked subtotal.
 * This remains a tracked subtotal, not a statement of total personal wealth.
 */
export function NetWorthView() {
  const [asOfInput, setAsOfInput] = useState('')
  const [priorAsOfInput, setPriorAsOfInput] = useState('')
  const [scope, setScope] = useState<Scope>({ asOf: '', priorAsOf: '' })
  const [page, setPage] = useState<NetWorthPageOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const lastGood = useRef<LastGood | null>(null)
  const seq = useRef(0)

  const refreshPolicy = useCallback(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])
  useEffect(() => { refreshPolicy() }, [refreshPolicy])

  const load = useCallback(() => {
    const id = ++seq.current
    const key = netWorthScopeKey(scope)
    setLoading(true); setError('')
    loadReportedNetWorth({ asOf: scope.asOf || undefined, priorAsOf: scope.priorAsOf || undefined })
      .then(response => {
        if (id !== seq.current) return
        lastGood.current = { key, policyCurrency: response.reporting_currency, response }
        setPage(response); setStaleBanner('none')
      })
      .catch(async caught => {
        if (id !== seq.current) return
        if (caught instanceof ApiError && caught.status === 409) {
          const saved = lastGood.current
          if (saved && saved.key === key) {
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
  }, [scope])
  useEffect(() => { load() }, [load])

  if (loading && !page && staleBanner === 'none' && !error) {
    return <div className="skeleton-page" aria-label="Loading tracked net worth"><div /><div /><div /></div>
  }

  const reportingCurrency = page?.reporting_currency ?? policy?.currency ?? 'SGD'

  return (
    <div className="networth-view" aria-busy={loading}>
      <div className="scope-caption">
        <span>Reporting in {reportingCurrency}{page ? ` · tracked subtotal as of ${page.as_of}` : ''} · not a statement of total personal wealth</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          <RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh
        </Button>
      </div>

      {error && (
        <div className="alert error" role="alert">
          <div><strong>We couldn’t load tracked net worth.</strong><p>{error}</p></div>
          <Button variant="outline" onClick={load}>Try again</Button>
        </div>
      )}

      {staleBanner === 'refresh-required' && (
        <div className="alert" role="alert">
          <div>
            <strong>Refresh required.</strong>
            <p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this subtotal.</p>
          </div>
          <Button variant="outline" onClick={load}>Refresh</Button>
        </div>
      )}
      {staleBanner === 'same-scope' && page && (
        <div className="alert" role="status">
          <div>
            <strong>Showing the last loaded values for this exact scope.</strong>
            <p>New ledger inputs are available. Refresh to recompute.</p>
          </div>
          <Button variant="outline" onClick={load}>Refresh</Button>
        </div>
      )}

      {page && (
        <NetWorthContent
          page={page} loading={loading}
          asOfInput={asOfInput} setAsOfInput={setAsOfInput}
          priorAsOfInput={priorAsOfInput} setPriorAsOfInput={setPriorAsOfInput}
          onApply={() => setScope({ asOf: asOfInput, priorAsOf: priorAsOfInput })}
        />
      )}
    </div>
  )
}

function NetWorthContent({
  page, loading, asOfInput, setAsOfInput, priorAsOfInput, setPriorAsOfInput, onApply,
}: {
  page: NetWorthPageOut
  loading: boolean
  asOfInput: string; setAsOfInput: (v: string) => void
  priorAsOfInput: string; setPriorAsOfInput: (v: string) => void
  onApply: () => void
}) {
  const currency = page.reporting_currency
  const currencies = Object.keys(page.per_currency).sort()
  const totals = page.totals

  return (
    <>
      <div className="alert" role="status">
        <div><strong>This is a tracked subtotal, not your net worth.</strong><p>{page.disclaimer}</p></div>
      </div>

      <Card className="obl-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">{currency} · Reported tracked subtotal</p>
            <h2><ConsolidatedMoney total={totals.net_worth.total} partialTotal={totals.net_worth.partial_total} currency={currency} unavailableReasons={totals.net_worth.unavailable_reasons} excludedDetail={totals.net_worth.excluded_detail} /> tracked net worth</h2>
          </div>
        </div>
        <dl className="baseline-metrics">
          <div><dt>Assets</dt><dd><ConsolidatedMoney total={totals.assets.total} partialTotal={totals.assets.partial_total} currency={currency} unavailableReasons={totals.assets.unavailable_reasons} excludedDetail={totals.assets.excluded_detail} /></dd></div>
          <div><dt>Liabilities</dt><dd><ConsolidatedMoney total={totals.liabilities.total} partialTotal={totals.liabilities.partial_total} currency={currency} unavailableReasons={totals.liabilities.unavailable_reasons} excludedDetail={totals.liabilities.excluded_detail} /></dd></div>
          <div><dt>Tracked net worth</dt><dd><ConsolidatedMoney total={totals.net_worth.total} partialTotal={totals.net_worth.partial_total} currency={currency} unavailableReasons={totals.net_worth.unavailable_reasons} excludedDetail={totals.net_worth.excluded_detail} /></dd></div>
        </dl>
        <p className="muted-note">Each component valued at its own as-of date, then combined once into {currency} — never a native amount summed across currencies before valuation.</p>

        <div className="panel-title"><div><p className="eyebrow">Manual values · separate basis</p></div></div>
        <dl className="baseline-metrics">
          <div><dt>Manual assets</dt><dd><ConsolidatedMoney total={totals.manual_assets.total} partialTotal={totals.manual_assets.partial_total} currency={currency} unavailableReasons={totals.manual_assets.unavailable_reasons} excludedDetail={totals.manual_assets.excluded_detail} /></dd></div>
          <div><dt>Manual liabilities</dt><dd><ConsolidatedMoney total={totals.manual_liabilities.total} partialTotal={totals.manual_liabilities.partial_total} currency={currency} unavailableReasons={totals.manual_liabilities.unavailable_reasons} excludedDetail={totals.manual_liabilities.excluded_detail} /></dd></div>
          <div><dt>Net worth incl. manual</dt><dd><ConsolidatedMoney total={totals.net_worth_including_manual.total} partialTotal={totals.net_worth_including_manual.partial_total} currency={currency} unavailableReasons={totals.net_worth_including_manual.unavailable_reasons} excludedDetail={totals.net_worth_including_manual.excluded_detail} /></dd></div>
        </dl>
        <p className="muted-note">Manual values keep their own basis and are reported here separately; they are not part of the evidence-only tracked subtotal.</p>
      </Card>

      <Card className="obl-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">Two dated observations</p>
            <h2>Compare a basic trend</h2>
          </div>
        </div>
        <div className="date-controls">
          <label>As of<Input type="date" aria-label="As of date" value={asOfInput} onChange={e => setAsOfInput(e.target.value)} /></label>
          <label>Compare with<Input type="date" aria-label="Prior observation date" value={priorAsOfInput} onChange={e => setPriorAsOfInput(e.target.value)} /></label>
          <Button variant="outline" onClick={onApply} disabled={loading}>Apply</Button>
        </div>
        {page.trend ? (
          <>
            <p className="muted-note">
              {dateLabel(page.trend.prior_as_of)} → {dateLabel(page.trend.current_as_of)}.{' '}
              {page.trend.scope_changed
                ? <strong>Scope changed between the two observations — the delta spans a different set of components.</strong>
                : page.trend.comparability}
            </p>
            <dl className="baseline-metrics">
              <div>
                <dt>{currency} · reported tracked net worth change</dt>
                <dd>
                  <ConsolidatedMoney total={page.trend.prior_net_worth.total} partialTotal={page.trend.prior_net_worth.partial_total} currency={currency} unavailableReasons={page.trend.prior_net_worth.unavailable_reasons} excludedDetail={page.trend.prior_net_worth.excluded_detail} />
                  {' → '}
                  <ConsolidatedMoney total={page.trend.current_net_worth.total} partialTotal={page.trend.current_net_worth.partial_total} currency={currency} unavailableReasons={page.trend.current_net_worth.unavailable_reasons} excludedDetail={page.trend.current_net_worth.excluded_detail} />
                  {page.trend.delta
                    ? <> {' '}(Δ {money(page.trend.delta.value, page.trend.delta.currency)})</>
                    : <small className="form-error"> delta unavailable — one or both sides could not be valued</small>}
                  {!page.trend.both_complete && <small className="form-error">one or both observations were incomplete</small>}
                </dd>
              </div>
            </dl>
            {page.trend.scope_changes.length > 0 && (
              <ul className="issue-list">
                {page.trend.scope_changes.map(sc => (
                  <li key={sc.component_id}><strong>{sc.component_id}</strong> — {sc.change.replaceAll('_', ' ')}</li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <p className="muted-note">Pick an earlier date in “Compare with” to see the change in the tracked subtotal between two dated observations.</p>
        )}
      </Card>

      <NetWorthCharts page={page} />

      {currencies.map(currency => (
        <NativeCurrencyCard key={currency} currency={currency} summary={page.per_currency[currency]} />
      ))}

      <Card className="obl-card">
        <div className="panel-title"><div><p className="eyebrow">Where each figure comes from</p><h2>Components</h2></div></div>
        <table className="pocket-table">
          <thead>
            <tr>
              <th>Component</th>
              <th>Side</th>
              <th>Native amount</th>
              <th>Reported ({currency})</th>
              <th>Basis</th>
              <th>As of</th>
              <th>Origin</th>
            </tr>
          </thead>
          <tbody>
            {page.components.map(c => <ComponentRow key={c.component_id} component={c} />)}
          </tbody>
        </table>
      </Card>

      <Card className="obl-card">
        <div className="panel-title"><div><p className="eyebrow">How this stays honest</p><h2>Rules</h2></div></div>
        <ul className="issue-list">
          {page.notes.map((note, i) => <li key={i}><p>{note}</p></li>)}
        </ul>
        <p className="muted-note"><ShieldCheck size={13} /> Reused once each: the position read, the obligations read and the card cash-conversion contract.</p>
      </Card>

      {currencies.length === 0 && (
        <Card className="obl-card">
          <div className="empty">
            <AlertTriangle size={24} />
            <h3>No tracked positions yet</h3>
            <p>Cash pockets, personal debts, receivables and card liability appear here once the ledger holds them.</p>
          </div>
        </Card>
      )}
    </>
  )
}

function NativeCurrencyCard({ currency, summary }: { currency: string; summary: NativeCurrencySummaryOut }) {
  return (
    <Card className="obl-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">{currency} · Native evidence-only subtotal</p>
          <h2>{money(summary.tracked_net_worth, currency)} tracked net worth</h2>
        </div>
        <Badge variant="outline" className={summary.complete ? '' : 'review-badge'}>
          {summary.complete ? 'All components valued' : 'Known subtotal — components missing'}
        </Badge>
      </div>
      <dl className="baseline-metrics">
        <div><dt>Assets</dt><dd>{money(summary.tracked_assets, currency)}</dd></div>
        <div><dt>Liabilities</dt><dd>{money(summary.tracked_liabilities, currency)}</dd></div>
        <div><dt>Tracked net worth</dt><dd>{money(summary.tracked_net_worth, currency)}</dd></div>
      </dl>
      <p className="muted-note">{summary.subtotal_note}</p>

      {summary.missing_components.length > 0 && (
        <>
          <div className="panel-title"><div><p className="eyebrow">Missing components</p></div></div>
          <ul className="issue-list">
            {summary.missing_components.map(m => (
              <li key={m.component_id}>
                <strong>{m.component_id}</strong> ({m.side}) — {m.reason ?? 'no valuation available'}
              </li>
            ))}
          </ul>
          <p className="muted-note">An unvalued or unsupported component is a named missing position here, never a zero.</p>
        </>
      )}

      {summary.excluded_components.length > 0 && (
        <>
          <div className="panel-title"><div><p className="eyebrow">Explicitly excluded</p></div></div>
          <ul className="issue-list">
            {summary.excluded_components.map(x => (
              <li key={x.component_id}>
                <strong>{x.component_id}</strong> · {money(x.amount, currency)} — {x.reason ?? 'excluded'}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  )
}

function ComponentRow({ component }: { component: NetWorthComponentOut }) {
  return (
    <tr className={component.included ? '' : 'obl-group-likely'}>
      <td>
        <strong>{component.label}</strong>
        <small>{KIND_LABEL[component.kind] ?? component.kind} · {component.currency}</small>
        {component.confidence && <small>confidence: {component.confidence}</small>}
      </td>
      <td>{component.side}</td>
      <td>
        {component.amount === null
          ? <span className="obl-gap">Missing</span>
          : money(component.amount, component.currency)}
        {!component.included && <small>{component.note ?? 'not counted'}</small>}
      </td>
      <td>{component.reported ? <Money value={component.reported} /> : <small className="muted-note">No evidence</small>}</td>
      <td>
        <Badge variant="outline" className={component.basis === 'manual' ? 'review-badge' : ''}>{humanize(component.basis)}</Badge>
      </td>
      <td>{component.as_of ? dateLabel(component.as_of) : '—'}</td>
      <td><small>{component.origin.replaceAll('_', ' ')}</small></td>
    </tr>
  )
}
