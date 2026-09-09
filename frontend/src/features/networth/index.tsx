import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, RefreshCw, ShieldCheck } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { AsyncView } from '@/components/AsyncView'
import { money } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import { loadNetWorth, type NetWorthComponent, type NetWorthPage } from './api'

const KIND_LABEL: Record<NetWorthComponent['kind'], string> = {
  cash: 'Cash position',
  debt: 'Debt / receivable',
  card_liability: 'Card liability',
}

/**
 * Cash-flow framework Stage 5B — tracked net worth.
 *
 * Read-only. A per-currency subtotal built from the dated cash, debt,
 * receivable and card positions the ledger can already support — each figure
 * taken from exactly one upstream read, so an installment already inside a card
 * statement, or a manual debt linked to a tracked card, cannot be counted
 * twice. Components the ledger cannot value are listed as missing, never as
 * zero. Manual values keep their basis on a separate line. This is a tracked
 * subtotal, not a statement of total personal wealth, and is never labelled as
 * one.
 */
export function NetWorthView() {
  const [page, setPage] = useState<NetWorthPage | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [asOf, setAsOf] = useState('')
  const [priorAsOf, setPriorAsOf] = useState('')
  const seq = useRef(0)

  const load = useCallback((params?: { as_of?: string; prior_as_of?: string }) => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadNetWorth(params)
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <AsyncView loading={loading} error={error} data={page} onRetry={() => load()} loadingLabel="Loading tracked net worth" errorTitle="We couldn’t load tracked net worth.">
      {page => <NetWorthContent page={page} loading={loading} load={load} asOf={asOf} setAsOf={setAsOf} priorAsOf={priorAsOf} setPriorAsOf={setPriorAsOf} />}
    </AsyncView>
  )
}

function NetWorthContent({ page, loading, load, asOf, setAsOf, priorAsOf, setPriorAsOf }: {
  page: NetWorthPage; loading: boolean; load: (params?: { as_of?: string; prior_as_of?: string }) => void
  asOf: string; setAsOf: (v: string) => void; priorAsOf: string; setPriorAsOf: (v: string) => void
}) {
  const currencies = Object.keys(page.per_currency).sort()

  return (
    <div className="networth-view">
      <div className="scope-caption">
        <span>
          Tracked subtotal as of {page.as_of}
          {' · '}{currencies.join(', ') || 'no currency'}
          {' · '}not a statement of total personal wealth
        </span>
        <Button variant="ghost" size="sm" onClick={() => load({ as_of: asOf || undefined, prior_as_of: priorAsOf || undefined })} disabled={loading}>
          <RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh
        </Button>
      </div>

      <div className="alert" role="status">
        <div><strong>This is a tracked subtotal, not your net worth.</strong><p>{page.disclaimer}</p></div>
      </div>

      <Card className="obl-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">TWO DATED OBSERVATIONS</p>
            <h2>Compare a basic trend</h2>
          </div>
        </div>
        <div className="date-controls">
          <label>As of<Input type="date" aria-label="As of date" value={asOf} onChange={e => setAsOf(e.target.value)} /></label>
          <label>Compare with<Input type="date" aria-label="Prior observation date" value={priorAsOf} onChange={e => setPriorAsOf(e.target.value)} /></label>
          <Button variant="outline" onClick={() => load({ as_of: asOf || undefined, prior_as_of: priorAsOf || undefined })} disabled={loading}>Apply</Button>
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
              {Object.entries(page.trend.change_by_currency).map(([currency, c]) => (
                <div key={currency}>
                  <dt>{currency} · tracked net worth change</dt>
                  <dd>
                    {money(c.prior_tracked_net_worth, currency)} → {money(c.current_tracked_net_worth, currency)}
                    {' '}(Δ {money(c.delta, currency)})
                    {!c.both_complete && <small className="form-error">one or both observations were incomplete</small>}
                  </dd>
                </div>
              ))}
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

      {currencies.map(currency => {
        const s = page.per_currency[currency]
        return (
          <Card className="obl-card" key={currency}>
            <div className="panel-title">
              <div>
                <p className="eyebrow">{currency} · EVIDENCE-ONLY TRACKED SUBTOTAL</p>
                <h2>{money(s.tracked_net_worth, currency)} tracked net worth</h2>
              </div>
              <Badge variant="outline" className={s.complete ? '' : 'review-badge'}>
                {s.complete ? 'All components valued' : 'Known subtotal — components missing'}
              </Badge>
            </div>
            <dl className="baseline-metrics">
              <div><dt>Assets</dt><dd>{money(s.tracked_assets, currency)}</dd></div>
              <div><dt>Liabilities</dt><dd>{money(s.tracked_liabilities, currency)}</dd></div>
              <div><dt>Tracked net worth</dt><dd>{money(s.tracked_net_worth, currency)}</dd></div>
            </dl>
            <p className="muted-note">{s.subtotal_note}</p>

            <div className="panel-title"><div><p className="eyebrow">MANUAL VALUES · SEPARATE BASIS</p></div></div>
            <dl className="baseline-metrics">
              <div><dt>Manual assets</dt><dd>{money(s.manual_assets, currency)}</dd></div>
              <div><dt>Manual liabilities</dt><dd>{money(s.manual_liabilities, currency)}</dd></div>
              <div><dt>Net worth incl. manual</dt><dd>{money(s.net_worth_including_manual, currency)}</dd></div>
            </dl>
            <p className="muted-note">{s.manual_basis_note}</p>

            {s.missing_components.length > 0 && (
              <>
                <div className="panel-title"><div><p className="eyebrow">MISSING COMPONENTS</p></div></div>
                <ul className="issue-list">
                  {s.missing_components.map(m => (
                    <li key={m.component_id}>
                      <strong>{m.component_id}</strong> ({m.side}) — {m.reason ?? 'no valuation available'}
                    </li>
                  ))}
                </ul>
                <p className="muted-note">An unvalued or unsupported component is a named missing position here, never a zero.</p>
              </>
            )}

            {s.excluded_components.length > 0 && (
              <>
                <div className="panel-title"><div><p className="eyebrow">EXPLICITLY EXCLUDED</p></div></div>
                <ul className="issue-list">
                  {s.excluded_components.map(x => (
                    <li key={x.component_id}>
                      <strong>{x.component_id}</strong> · {money(x.amount, currency)} — {x.reason ?? 'excluded'}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </Card>
        )
      })}

      <Card className="obl-card">
        <div className="panel-title"><div><p className="eyebrow">WHERE EACH FIGURE COMES FROM</p><h2>Components</h2></div></div>
        <table className="pocket-table">
          <thead>
            <tr>
              <th>Component</th>
              <th>Side</th>
              <th>Amount</th>
              <th>Basis</th>
              <th>As of</th>
              <th>Origin</th>
            </tr>
          </thead>
          <tbody>
            {page.components.map(c => (
              <tr key={c.component_id} className={c.included ? '' : 'obl-group-likely'}>
                <td>
                  <strong>{c.label}</strong>
                  <small>{KIND_LABEL[c.kind]} · {c.currency}</small>
                  {c.confidence && <small>confidence: {c.confidence}</small>}
                </td>
                <td>{c.side}</td>
                <td>
                  {c.amount === null
                    ? <span className="obl-gap">Missing</span>
                    : money(c.amount, c.currency)}
                  {!c.included && <small>{c.note ?? 'not counted'}</small>}
                </td>
                <td>
                  <Badge variant="outline" className={c.basis === 'manual' ? 'review-badge' : ''}>{c.basis}</Badge>
                </td>
                <td>{c.as_of ? dateLabel(c.as_of) : '—'}</td>
                <td><small>{c.origin}</small></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card className="obl-card">
        <div className="panel-title"><div><p className="eyebrow">HOW THIS STAYS HONEST</p><h2>Rules</h2></div></div>
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
    </div>
  )
}
