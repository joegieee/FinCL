import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, RefreshCw, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { money } from '@/lib/api'
import { loadCashPosition, type CashBridgePocket, type CashPocket, type CashPosition } from './api'

/**
 * Cash-flow framework Stage 2A — the evidence-backed liquid position.
 *
 * Read-only. Every figure is resolved from a printed closing balance, a
 * reconciled statement close or a journalled operator observation, and carries
 * its basis, as-of date and staleness. Nothing here is a mutable "current
 * balance".
 *
 * Guardrails this view must keep:
 * - When today's opening cash is only modelled (no complete actual-movement
 *   interval from the supported cutoff through today) it is labelled modelled
 *   and no "available today" figure, reserve verdict or spending allowance is
 *   shown — the module never emits those and this view never invents them.
 * - A currency total the module withholds shows its stated reason, never a zero.
 * - A receivable is not cash: only settled bank and e-wallet pockets are liquid.
 */
export function CashView() {
  const [page, setPage] = useState<CashPosition | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadCashPosition()
      .then(p => { if (id === seq.current) setPage(p) })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  if (loading && !page) return <div className="skeleton-page" aria-label="Loading cash position"><div /><div /><div /></div>
  if (error && !page) {
    return <div className="alert error" role="alert"><div><strong>We couldn’t load the cash position.</strong><p>{error}</p></div><Button variant="outline" onClick={load}>Try again</Button></div>
  }
  if (!page) return null

  const observedToday = page.current_position_supported
  const withheld = Object.entries(page.liquid_total_withheld)
  const carried = Object.entries(page.combined_liquid_position)
  const excluded = page.pockets.filter(p => !p.liquid)
  const included = page.pockets.filter(p => p.liquid)

  return (
    <div className="cash-view">
      <div className="scope-caption">
        <span>
          {included.length} liquid pocket{included.length === 1 ? '' : 's'}
          {' · '}{Array.from(new Set(included.map(p => p.currency_pocket))).join(', ') || 'no currency'}
          {page.supported_cutoff ? ` · supported cutoff ${page.supported_cutoff}` : ' · no common supported cutoff'}
        </span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>

      <Card className="cash-today-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">LIQUID CASH TODAY · {page.generated_for}</p>
            <h2>
              {observedToday
                ? Object.entries(page.liquid_total).map(([c, v]) => `${money(v, c)} available today`).join(' · ') || 'Nothing included'
                : 'Not observed as of today'}
            </h2>
          </div>
          <Badge variant="outline" className={observedToday ? '' : 'review-badge'}>{observedToday ? 'Observed' : 'Modelled'}</Badge>
        </div>
        {observedToday
          ? <p className="muted-note"><CheckCircle2 size={14} /> Complete actual movements run from the supported cutoff through today.</p>
          : (
            <>
              <p className="muted-note">
                Today’s opening cash is modelled, not observed. While the evidence is incomplete, no current-cash figure,
                reserve verdict or spending allowance is derived.
              </p>
              {page.current_position_reasons.length > 0 && (
                <ul className="issue-list">
                  {page.current_position_reasons.map((reason, i) => <li key={i}><p>{reason}</p></li>)}
                </ul>
              )}
              {carried.length > 0 && (
                <p className="muted-note">
                  Last supported combined balance{page.supported_cutoff ? ` · as of ${page.supported_cutoff}` : ''}:{' '}
                  {carried.map(([c, v]) => money(v, c)).join(' · ')}. Carried forward for reference only.
                </p>
              )}
            </>
          )}
      </Card>

      {withheld.length > 0 && (
        <Card className="cash-withheld-card">
          <div className="panel-title"><div><p className="eyebrow">WITHHELD CURRENCY TOTALS</p><h2>{withheld.length} currenc{withheld.length === 1 ? 'y' : 'ies'} withheld</h2></div></div>
          <ul className="issue-list">
            {withheld.map(([currency, reason]) => (
              <li key={currency}><strong>{currency}</strong> — {reason}</li>
            ))}
          </ul>
          <p className="muted-note">A withheld total states its reason. It is never reported as a zero.</p>
        </Card>
      )}

      <Card className="cash-pockets-card">
        <div className="panel-title"><div><p className="eyebrow">PER-ACCOUNT &amp; PER-POCKET NATIVE BALANCES</p><h2>{included.length === 0 ? 'No included pockets' : `${included.length} in liquid scope`}</h2></div></div>
        <table className="pocket-table">
          <thead><tr><th>Account</th><th>Pocket</th><th>Native balance</th><th>Basis</th><th>As of</th><th>Scope</th></tr></thead>
          <tbody>
            {page.pockets.map(pocket => <PocketRow key={`${pocket.account_id}:${pocket.currency_pocket}`} pocket={pocket} staleAfter={page.stale_after_days} />)}
          </tbody>
        </table>
        <p className="muted-note">
          <ShieldCheck size={13} /> A receivable is not cash. Only settled bank and e-wallet pockets are counted as liquid;
          credit-card facility pockets are an obligation and are excluded.
        </p>
      </Card>

      {excluded.length > 0 && (
        <Card className="cash-excluded-card">
          <div className="panel-title"><div><p className="eyebrow">EXCLUDED FROM LIQUID ASSETS</p><h2>{excluded.length} pocket{excluded.length === 1 ? '' : 's'}</h2></div></div>
          <ul className="issue-list">
            {excluded.map(pocket => (
              <li key={`${pocket.account_id}:${pocket.currency_pocket}`}>
                <strong>{pocket.display_name ?? pocket.account_id} · {pocket.currency_pocket}</strong>
                {pocket.source_type && <> · {pocket.source_type}</>}
                <p>{pocket.excluded_reason ?? 'not supported as liquid cash'}</p>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card className="cash-bridge-card">
        <div className="panel-title"><div><p className="eyebrow">OPENING + MOVEMENTS → CLOSING</p><h2>{page.cash_bridge.pockets.length === 0 ? 'No reconstructable interval' : `${page.cash_bridge.pockets.length} account bridge${page.cash_bridge.pockets.length === 1 ? '' : 's'}`}</h2></div></div>
        {page.cash_bridge.pockets.map(bridge => <BridgeRow key={`${bridge.account_id}:${bridge.currency_pocket}`} bridge={bridge} />)}
        {Object.entries(page.cash_bridge.combined.by_currency).map(([currency, c]) => (
          <p key={currency} className="muted-note">
            Combined {currency}: {money(c.opening, currency)} opening + {money(c.external_inflows, currency)} in − {money(c.external_outflows, currency)} out → {money(c.closing, currency)} closing ·{' '}
            {c.reconciled ? 'reconciled' : 'not reconciled'} · {c.owned_transfer_pairs_cancelled} owned transfer pair{c.owned_transfer_pairs_cancelled === 1 ? '' : 's'} cancelled
          </p>
        ))}
      </Card>

      {page.missing_intervals.length > 0 && (
        <Card className="cash-missing-card">
          <div className="panel-title"><div><p className="eyebrow">MISSING MOVEMENT INTERVALS</p><h2>{page.missing_intervals.length} named gap{page.missing_intervals.length === 1 ? '' : 's'}</h2></div></div>
          <ul className="issue-list">
            {page.missing_intervals.map((gap, i) => (
              <li key={i}>
                <strong>{gap.currency_pocket}</strong> · {gap.start} → {gap.end}
                <p>{gap.reason}</p>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card className="cash-disclosure-card">
        <div className="panel-title"><div><p className="eyebrow">STALENESS</p><h2>Disclosed past {page.stale_after_days} days</h2></div></div>
        <p className="muted-note">
          A pocket balance older than {page.stale_after_days} days is still shown, with its real as-of date, but flagged
          stale. {page.stale ? 'At least one included pocket is stale.' : 'No included pocket is currently stale.'}
        </p>
      </Card>
    </div>
  )
}

function PocketRow({ pocket, staleAfter }: { pocket: CashPocket; staleAfter: number }) {
  return (
    <tr>
      <td>
        {pocket.display_name ?? pocket.account_id}
        {pocket.institution && <small>{pocket.institution}</small>}
      </td>
      <td>{pocket.currency_pocket}</td>
      <td>
        {pocket.balance === null
          ? <span className="form-error">{pocket.unavailable_reason ?? 'unavailable'}</span>
          : money(pocket.balance, pocket.currency_pocket)}
        {pocket.stale && pocket.staleness_days !== null && (
          <small className="form-error">stale · {pocket.staleness_days} days &gt; {staleAfter}</small>
        )}
      </td>
      <td>{pocket.basis}{pocket.date_precision && pocket.date_precision !== 'DAY' && <small>{pocket.date_precision} precision</small>}</td>
      <td>{pocket.as_of ?? '—'}</td>
      <td>
        {pocket.liquid
          ? <Badge variant="outline">Liquid</Badge>
          : <Badge variant="outline" className="review-badge" title={pocket.excluded_reason ?? undefined}>Excluded</Badge>}
      </td>
    </tr>
  )
}

function BridgeRow({ bridge }: { bridge: CashBridgePocket }) {
  return (
    <div className="cash-bridge-row">
      <p>
        <strong>{bridge.currency_pocket}</strong> · opening {money(bridge.opening, bridge.currency_pocket)} ({bridge.opening_as_of})
        {' + '}{money(bridge.external_inflows, bridge.currency_pocket)} external in
        {' − '}{money(bridge.external_outflows, bridge.currency_pocket)} external out
        {' → '}calculated {money(bridge.calculated_closing, bridge.currency_pocket)}
        {' vs '}closing {money(bridge.closing, bridge.currency_pocket)} ({bridge.closing_as_of})
      </p>
      <p className={bridge.reconciled ? 'muted-note' : 'form-error'}>
        {bridge.reconciled
          ? <><CheckCircle2 size={13} /> Reconciled across {bridge.movement_count} movement{bridge.movement_count === 1 ? '' : 's'}, {bridge.paired_transfer_link_count} owned transfer link{bridge.paired_transfer_link_count === 1 ? '' : 's'}.</>
          : <><AlertTriangle size={13} /> Not reconciled — {bridge.unsupported_reason ?? 'unknown'}</>}
      </p>
    </div>
  )
}
