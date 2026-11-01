import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, PenLine, RefreshCw, ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ApiError, money } from '@/lib/api'
import { adapterLabel } from '@/features/setup/api'
import { ConsolidatedMoney, Money } from '@/components/Money'
import { BalanceObservationForm } from './BalanceObservationForm'
import { CertifyCoverageForm, type CertifyTarget } from './CertifyCoverageForm'
import { CoveragePanel } from './CoveragePanel'
import {
  type CoverageReport, type MovementCertification,
  loadCertifications, loadCoverageReport, retireCoverage,
} from './api'
import {
  type CashFilters, type CashPageOut, type CashPocketOut, type ReportingPolicySummary,
  emptyCashFilters, loadReportedCash, loadReportingPolicy, cashScopeKey,
} from './reportedApi'
import { dateLabel } from '@/lib/format'

type StaleBanner = 'none' | 'same-scope' | 'refresh-required'
type LastGood = { key: string; policyCurrency: string; response: CashPageOut }

/**
 * Cash-flow framework Stage 2A — the evidence-backed liquid position (M4
 * step 12, plan §3.3).
 *
 * Read-only. Every native figure is resolved from a printed closing balance,
 * a reconciled statement close or a journalled operator observation, and
 * carries its basis, as-of date and staleness -- exactly as before. This
 * step additionally attaches a reported equivalent to each priceable native
 * amount, valued at that amount's own date under the shared reporting
 * policy; it changes no native calculation.
 *
 * Guardrails this view must keep:
 * - When today's opening cash lacks complete actual-movement coverage it is
 *   withheld. A historical common cutoff remains a dated observed-derived view,
 *   and no "available today" figure, reserve verdict or spending allowance is
 *   shown -- the module never emits those and this view never invents them.
 * - A currency total the module withholds shows its stated reason, never a zero.
 * - A receivable is not cash: only settled bank and e-wallet pockets are liquid.
 * - Same-date totals require supported balances for that date: a pocket with
 *   no common cutoff never gets summed with another pocket at a different date.
 */
export function CashView({
  generation, onSaved, onOpenSetup,
}: {
  generation: string
  onSaved?: (status: string) => void | Promise<void>
  /** Opens Setup, where a missing statement is uploaded. */
  onOpenSetup: () => void
}) {
  const [filters, setFilters] = useState<CashFilters>(emptyCashFilters)
  const [page, setPage] = useState<CashPageOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [staleBanner, setStaleBanner] = useState<StaleBanner>('none')
  const [policy, setPolicy] = useState<ReportingPolicySummary | null>(null)
  const [observing, setObserving] = useState<CashPocketOut | null>(null)
  const [certifying, setCertifying] = useState<CertifyTarget | null>(null)
  const [coverage, setCoverage] = useState<CoverageReport | null>(null)
  const [certifications, setCertifications] = useState<MovementCertification[]>([])
  const lastGood = useRef<LastGood | null>(null)
  const seq = useRef(0)

  const refreshPolicy = useCallback(() => { loadReportingPolicy().then(setPolicy).catch(() => {}) }, [])
  useEffect(() => { refreshPolicy() }, [refreshPolicy])

  // Coverage and certifications are native reads on their own routes, so a
  // failure there degrades the panel rather than the whole position.
  const refreshCoverage = useCallback(() => {
    loadCoverageReport().then(setCoverage).catch(() => setCoverage(null))
    loadCertifications().then(r => setCertifications(r.certifications)).catch(() => setCertifications([]))
  }, [])
  useEffect(() => { refreshCoverage() }, [refreshCoverage])

  const load = useCallback(() => {
    const id = ++seq.current
    const key = cashScopeKey({ filters })
    setLoading(true); setError('')
    loadReportedCash({ filters })
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
  }, [filters])
  useEffect(() => { load() }, [load])

  if (loading && !page && staleBanner === 'none' && !error) {
    return <div className="skeleton-page" aria-label="Loading cash position"><div /><div /><div /></div>
  }

  const reportingCurrency = page?.reporting_currency ?? policy?.currency ?? 'SGD'

  return (
    <div className="cash-view" aria-busy={loading}>
      <div className="scope-caption">
        <span>Reporting in {reportingCurrency}</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>

      {filters.pocketCurrency && (
        <p className="analysis-scope-note">
          Showing only {filters.pocketCurrency} pockets.{' '}
          <button className="linkish" onClick={() => setFilters(emptyCashFilters)}>Show all pockets</button>
        </p>
      )}

      {error && (
        <div className="alert error" role="alert">
          <div><strong>We couldn’t load the cash position.</strong><p>{error}</p></div>
          <Button variant="outline" onClick={load}>Try again</Button>
        </div>
      )}

      {staleBanner === 'refresh-required' && (
        <div className="alert" role="alert">
          <div>
            <strong>Refresh required.</strong>
            <p>The ledger has changed since the last refresh (new evidence, a rate import, or a reporting-policy change). Refresh before reading this position.</p>
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
        <CashPage
          page={page} filters={filters} onFiltersChange={setFilters} generation={generation}
          onObserve={setObserving} coverage={coverage} certifications={certifications}
          onCertify={setCertifying}
          onOpenSetup={onOpenSetup}
          onObserveScope={(accountId, pocketCurrency) => {
            const match = page.pockets.find(
              p => p.account_id === accountId && p.currency_pocket === pocketCurrency,
            )
            if (match) setObserving(match)
          }}
          onRetireCertification={async (certification) => {
            try {
              const result = await retireCoverage(
                generation, certification.certification, 'no longer certain this range was quiet',
              )
              load(); refreshCoverage()
              await onSaved?.(result.status)
            } catch { /* the banner from the next load reports it */ }
          }}
        />
      )}

      {observing && (
        <BalanceObservationForm
          pocket={observing}
          generation={generation}
          onCancel={() => setObserving(null)}
          onSaved={async (status) => {
            setObserving(null)
            load(); refreshCoverage()
            await onSaved?.(status)
          }}
        />
      )}

      {certifying && (
        <CertifyCoverageForm
          target={certifying}
          generation={generation}
          onCancel={() => setCertifying(null)}
          onSaved={async (status) => {
            setCertifying(null)
            load(); refreshCoverage()
            await onSaved?.(status)
          }}
        />
      )}
    </div>
  )
}

function CashPage({
  page, filters, onFiltersChange, onObserve,
  coverage, certifications, onCertify, onObserveScope, onRetireCertification, onOpenSetup,
}: {
  page: CashPageOut
  filters: CashFilters
  onFiltersChange: (f: CashFilters) => void
  generation: string
  onObserve: (pocket: CashPocketOut) => void
  coverage: CoverageReport | null
  certifications: MovementCertification[]
  onCertify: (target: CertifyTarget) => void
  onObserveScope: (accountId: string, currencyPocket: string) => void
  onRetireCertification: (certification: MovementCertification) => void
  onOpenSetup: () => void
}) {
  const currency = page.reporting_currency
  const observedToday = page.current_position_supported
  const withheld = Object.entries(page.liquid_total_withheld)
  const carried = Object.entries(page.combined_liquid_position)
  const excluded = page.pockets.filter(p => !p.liquid)
  const included = page.pockets.filter(p => p.liquid)
  const pocketCurrencies = Array.from(new Set(page.pockets.map(p => p.currency_pocket))).sort()

  return (
    <>
      <div className="scope-caption cash-account-filter">
        <label className="currency-label">
          Account currency
          <select
            aria-label="Filter by account currency"
            value={filters.pocketCurrency}
            onChange={e => onFiltersChange({ pocketCurrency: e.target.value })}
          >
            <option value="">All pockets</option>
            {pocketCurrencies.map(c => <option key={c}>{c}</option>)}
          </select>
        </label>
      </div>

      <Card className="cash-today-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">Liquid cash today · {dateLabel(page.generated_for)}</p>
            <h2>
              {observedToday
                ? Object.entries(page.liquid_total).map(([c, v]) => `${money(v, c)} available today`).join(' · ') || 'Nothing included'
                : 'Not observed as of today'}
            </h2>
          </div>
          <Badge variant="outline" className={observedToday ? '' : 'review-badge'}>{observedToday ? 'Observed through today' : page.supported_cutoff ? `Observed through ${page.supported_cutoff}` : 'Not supported'}</Badge>
        </div>
        <p className="muted-note">
          Reported total ({currency}), same common cutoff: <ConsolidatedMoney
            total={page.combined.amount.total} partialTotal={page.combined.amount.partial_total} currency={currency}
            unavailableReasons={page.combined.amount.unavailable_reasons} excludedDetail={page.combined.amount.excluded_detail}
          />
          {page.combined.balance_as_of && ` as of ${dateLabel(page.combined.balance_as_of)}`}.
        </p>
        {observedToday
          ? <p className="muted-note"><CheckCircle2 size={14} /> Complete actual movements run from the supported cutoff through today.</p>
          : (
            <>
              <p className="muted-note">
                Today’s opening cash is not supported by complete actual-movement evidence. While coverage is incomplete, no current-cash figure,
                reserve verdict or spending allowance is derived.
              </p>
              {page.current_position_reasons.length > 0 && (
                <ul className="issue-list">
                  {page.current_position_reasons.map((reason, i) => <li key={i}><p>{reason}</p></li>)}
                </ul>
              )}
              {carried.length > 0 && (
                <p className="muted-note">
                  Last common supported balance{page.supported_cutoff ? ` · as of ${page.supported_cutoff}` : ''}:{' '}
                  {carried.map(([c, v]) => money(v, c)).join(' · ')}. It is derived from observed movements through that date, not reported as today’s cash.
                </p>
              )}
            </>
          )}
      </Card>

      <CoveragePanel
        summary={page.coverage_summary} partial={page.partial_position} report={coverage} certifications={certifications}
        onCertify={onCertify} onObserve={onObserveScope}
        onImport={onOpenSetup}
        onRetireCertification={onRetireCertification}
      />

      {withheld.length > 0 && (
        <Card className="cash-withheld-card">
          <div className="panel-title"><div><p className="eyebrow">Withheld currency totals</p><h2>{withheld.length} currenc{withheld.length === 1 ? 'y' : 'ies'} withheld</h2></div></div>
          <ul className="issue-list">
            {withheld.map(([currency, reason]) => (
              <li key={currency}><strong>{currency}</strong> — {reason}</li>
            ))}
          </ul>
          <p className="muted-note">A withheld total states its reason. It is never reported as a zero.</p>
        </Card>
      )}

      <Card className="cash-pockets-card">
        <div className="panel-title"><div><p className="eyebrow">Per-account &amp; per-pocket native balances</p><h2>{included.length === 0 ? 'No included pockets' : `${included.length} in liquid scope`}</h2></div></div>
        <table className="pocket-table">
          <thead><tr><th>Account</th><th>Pocket</th><th>Native balance</th><th>Reported ({currency})</th><th>Coverage-backed balance</th><th>Reported ({currency})</th><th>Basis</th><th>As of</th><th>Scope</th><th><span className="sr-only">Manual observation</span></th></tr></thead>
          <tbody>
            {page.pockets.map(pocket => <PocketRow key={`${pocket.account_id}:${pocket.currency_pocket}`} pocket={pocket} staleAfter={page.stale_after_days} onObserve={() => onObserve(pocket)} />)}
          </tbody>
        </table>
        <p className="muted-note">
          <ShieldCheck size={13} /> A receivable is not cash. Only settled bank and e-wallet pockets are counted as liquid;
          credit-card facility pockets are an obligation and are excluded.
        </p>
        <p className="muted-note">
          <PenLine size={13} /> "Record balance" asserts a dated balance by hand — from a bank app or a passbook — for a
          pocket with no statement to import. It carries the same weight as a printed statement close, but on its own it
          proves only that one day; it does not certify the days around it.
        </p>
      </Card>

      {excluded.length > 0 && (
        <Card className="cash-excluded-card">
          <div className="panel-title"><div><p className="eyebrow">Excluded from liquid assets</p><h2>{excluded.length} pocket{excluded.length === 1 ? '' : 's'}</h2></div></div>
          <ul className="issue-list">
            {excluded.map(pocket => (
              <li key={`${pocket.account_id}:${pocket.currency_pocket}`}>
                <strong>{pocket.display_name ?? pocket.account_id} · {pocket.currency_pocket}</strong>
                {pocket.source_type && <> · {adapterLabel(pocket.source_type)}</>}
                <p>{pocket.excluded_reason ?? 'not supported as liquid cash'}</p>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card className="cash-bridge-card">
        <div className="panel-title"><div><p className="eyebrow">Opening + movements → closing</p><h2>{page.cash_bridge.pockets.length === 0 ? 'No reconstructable interval' : `${page.cash_bridge.pockets.length} account bridge${page.cash_bridge.pockets.length === 1 ? '' : 's'}`}</h2></div></div>
        <p className="muted-note">Native evidence only; each bridge stays in its own account/pocket currency.</p>
        {page.cash_bridge.pockets.map(bridge => <BridgeRow key={`${bridge.account_id}:${bridge.currency_pocket}`} bridge={bridge} />)}
        {Object.entries(page.cash_bridge.combined_by_currency).map(([currency, c]) => (
          <p key={currency} className="muted-note">
            Combined {currency}: {money(c.opening, currency)} opening + {money(c.external_inflows, currency)} in − {money(c.external_outflows, currency)} out → {money(c.closing, currency)} closing ·{' '}
            {c.reconciled ? 'reconciled' : 'not reconciled'} · {c.owned_transfer_pairs_cancelled} owned transfer pair{c.owned_transfer_pairs_cancelled === 1 ? '' : 's'} cancelled
          </p>
        ))}
      </Card>

      {page.missing_intervals.length > 0 && (
        <Card className="cash-missing-card">
          <div className="panel-title"><div><p className="eyebrow">Missing movement intervals</p><h2>{page.missing_intervals.length} named gap{page.missing_intervals.length === 1 ? '' : 's'}</h2></div></div>
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
        <div className="panel-title"><div><p className="eyebrow">Staleness</p><h2>Disclosed past {page.stale_after_days} days</h2></div></div>
        <p className="muted-note">
          A pocket balance older than {page.stale_after_days} days is still shown, with its real as-of date, but flagged
          stale. {page.stale ? 'At least one included pocket is stale.' : 'No included pocket is currently stale.'}
        </p>
      </Card>
    </>
  )
}

function PocketRow({ pocket, staleAfter, onObserve }: { pocket: CashPocketOut; staleAfter: number; onObserve: () => void }) {
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
      <td>{pocket.reported_balance ? <Money value={pocket.reported_balance} /> : <small className="muted-note">No evidence</small>}</td>
      <td>
        {pocket.derived_balance === null
          ? <small className="muted-note">No common coverage-backed balance</small>
          : <><strong>{money(pocket.derived_balance, pocket.currency_pocket)}</strong><small>{pocket.derived_basis} · through {pocket.derived_as_of ? dateLabel(pocket.derived_as_of) : ''}</small></>}
      </td>
      <td>{pocket.reported_derived_balance ? <Money value={pocket.reported_derived_balance} /> : <small className="muted-note">No common cutoff</small>}</td>
      <td>{pocket.basis}{pocket.date_precision && pocket.date_precision !== 'DAY' && <small>{pocket.date_precision} precision</small>}</td>
      <td>{pocket.as_of ?? '—'}</td>
      <td>
        {pocket.liquid
          ? <Badge variant="outline">Liquid</Badge>
          : <Badge variant="outline" className="review-badge" title={pocket.excluded_reason ?? undefined}>Excluded</Badge>}
      </td>
      <td>
        <Button
          variant="ghost" size="sm"
          title="Record a manual balance observation for this pocket"
          aria-label={`${pocket.manual_observation ? 'Update' : 'Record'} manual balance for ${pocket.display_name ?? pocket.account_id} ${pocket.currency_pocket}`}
          onClick={onObserve}
        >
          <PenLine size={14} /> {pocket.manual_observation ? 'Update' : 'Record balance'}
        </Button>
      </td>
    </tr>
  )
}

function BridgeRow({ bridge }: { bridge: CashPageOut['cash_bridge']['pockets'][number] }) {
  return (
    <div className="cash-bridge-row">
      <p>
        <strong>{bridge.currency_pocket}</strong> · opening {money(bridge.opening, bridge.currency_pocket)} ({dateLabel(bridge.opening_as_of)})
        {' + '}{money(bridge.external_inflows, bridge.currency_pocket)} external in
        {' − '}{money(bridge.external_outflows, bridge.currency_pocket)} external out
        {' → '}calculated {money(bridge.calculated_closing, bridge.currency_pocket)}
        {' vs '}closing {money(bridge.closing, bridge.currency_pocket)} ({dateLabel(bridge.closing_as_of)})
      </p>
      <p className={bridge.reconciled ? 'muted-note' : 'form-error'}>
        {bridge.reconciled
          ? <><CheckCircle2 size={13} /> Reconciled across {bridge.movement_count} movement{bridge.movement_count === 1 ? '' : 's'}, {bridge.paired_transfer_link_count} owned transfer link{bridge.paired_transfer_link_count === 1 ? '' : 's'}.</>
          : <><AlertTriangle size={13} /> Not reconciled — {bridge.unsupported_reason ?? 'unknown'}</>}
      </p>
    </div>
  )
}
