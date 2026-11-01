import { WorkspaceReportingDetails, reportedMoney } from '@/lib/workspaceReporting'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CalendarRange, CheckCircle2, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { AsyncView } from '@/components/AsyncView'
import { money } from '@/lib/api'
import { adapterLabel } from '@/features/setup/api'
import { loadBaselineEstimate, loadBaselineReadiness, type BaselineEstimate, type BaselineReadiness, type Completeness, type CurrencyEstimate, type ObservedRange, type PocketReadiness, type Ratio } from './api'
import { BaselineContributors } from './Contributors'

const COMPLETENESS_LABEL: Record<Completeness, string> = {
  established: 'Established',
  provisional: 'Observed only',
  missing: 'Missing',
}

const QUALITY_LABEL: Record<string, string> = {
  normal: 'Normal baseline',
  'limited-history': 'Limited history',
  none: 'Not enough evidence',
}

const TASK_LABEL: Record<string, string> = {
  'import-missing': 'Import missing statements',
  'confirm-export-period': 'Confirm an export period',
  'resolve-reconciliation': 'Resolve reconciliation failures',
  'resolve-categorization': 'Classify unresolved spending',
}

/**
 * Cash-flow framework Stage 1A — evidence readiness.
 *
 * Read-only. Shows, per account/pocket, which months are backed by a proven
 * statement period (established), which are only observed (provisional) and
 * which are missing, then the recommended baseline window and the
 * missing-evidence tasks. There is no "enter the baseline instead" control:
 * a baseline is discovered from imported actuals or it is withheld.
 */
export function BaselineView({ onOpenTransactionAnchor }: { onOpenTransactionAnchor?: (encodedAnchor: string) => Promise<void> } = {}) {
  const [page, setPage] = useState<BaselineReadiness | null>(null)
  const [estimate, setEstimate] = useState<BaselineEstimate | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    Promise.all([loadBaselineReadiness(), loadBaselineEstimate()])
      .then(([p, e]) => { if (id === seq.current) { setPage(p); setEstimate(e) } })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <AsyncView loading={loading} error={error} data={page} onRetry={load} loadingLabel="Loading baseline readiness" errorTitle="We couldn’t load baseline readiness.">
      {page => <BaselineContent page={page} estimate={estimate} loading={loading} load={load} onOpenTransaction={onOpenTransactionAnchor} />}
    </AsyncView>
  )
}

function BaselineContent({ page, estimate, loading, load, onOpenTransaction }: { page: BaselineReadiness; estimate: BaselineEstimate | null; loading: boolean; load: () => void; onOpenTransaction?: (encodedAnchor: string) => Promise<void> }) {
  const w = page.baseline_window
  const analysis = page.scope.analysis_window

  return (
    <div className="baseline-view"><WorkspaceReportingDetails data={page}/>{estimate && <WorkspaceReportingDetails data={estimate}/>}
      <div className="scope-caption">
        <span>{page.pockets.length} pocket{page.pockets.length === 1 ? '' : 's'} · {page.scope.currencies.join(', ') || 'no currency'}{analysis && ` · evidence ${analysis.start} → ${analysis.end}`}</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>

      <Card className="baseline-window-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">Recommended baseline window</p>
            <h2>
              <CalendarRange size={18} />{' '}
              {w.quality === 'none' || !w.start_month
                ? 'No window yet'
                : `${w.start_month} → ${w.end_month} · ${w.month_count} month${w.month_count === 1 ? '' : 's'}`}
            </h2>
          </div>
          <Badge variant="outline" className={w.quality === 'normal' ? '' : 'review-badge'}>{QUALITY_LABEL[w.quality] ?? w.quality}</Badge>
        </div>
        <p className="muted-note">{w.reason}</p>
        {w.basis === 'some-provisional' && (
          <p className="muted-note">This window rests on observed-only months. Any figure derived from it is a labelled limited-history estimate, not a normal baseline.</p>
        )}
      </Card>

      {estimate && <EstimateSection estimate={estimate} onOpenTransaction={onOpenTransaction} />}

      <Card className="baseline-tasks-card">
        <div className="panel-title"><div><p className="eyebrow">Before a baseline</p><h2>{page.tasks.length === 0 ? 'Nothing blocking' : `${page.tasks.length} evidence task${page.tasks.length === 1 ? '' : 's'}`}</h2></div></div>
        {page.tasks.length === 0
          ? <p className="muted-note"><CheckCircle2 size={14} /> Every expected month is backed by evidence. You can derive a baseline.</p>
          : (
            <ul className="issue-list">
              {page.tasks.map((task, i) => (
                <li key={i}>
                  <strong>{TASK_LABEL[task.kind] ?? task.kind}</strong>
                  {task.display_name && <> · {task.display_name} {task.currency_pocket}</>}
                  {task.interval && task.interval.start && <> · {task.interval.start} → {task.interval.end}</>}
                  <p>{task.note}</p>
                </li>
              ))}
            </ul>
          )}
      </Card>

      {page.pockets.map(pocket => <PocketCard key={`${pocket.account_id}:${pocket.currency_pocket}`} pocket={pocket} />)}
      {page.pockets.length === 0 && (
        <Card><div className="empty"><AlertTriangle size={24} /><h3>No imported activity</h3><p>Import statements first. The baseline is discovered from them — it is never typed in.</p></div></Card>
      )}
    </div>
  )
}

function PocketCard({ pocket }: { pocket: PocketReadiness }) {
  const recon = pocket.reconciliation
  return (
    <Card className="baseline-pocket-card">
      <div className="panel-title">
        <div>
          <p className="eyebrow">{pocket.source_type ? adapterLabel(pocket.source_type) : pocket.institution}</p>
          <h2>{pocket.display_name} · {pocket.currency_pocket}</h2>
          <small className="muted-note">
            {pocket.observed_span.start ? `${pocket.observed_span.start} → ${pocket.observed_span.end}` : 'no entries'} · {pocket.entry_count} entries
            {recon.total > 0 && ` · reconciliation ${recon.passed}/${recon.total}`}
          </small>
        </div>
      </div>

      <ol className="baseline-month-grid">
        {pocket.months.map(cell => (
          <li key={cell.month} className={`baseline-month is-${cell.completeness}`} title={`${cell.month} · ${COMPLETENESS_LABEL[cell.completeness]} · ${cell.basis} · ${cell.entry_count} entries`}>
            <span className="baseline-month-label">{cell.month}</span>
            <span className="baseline-month-state">{COMPLETENESS_LABEL[cell.completeness]}</span>
            <span className="baseline-month-count">{cell.entry_count}</span>
          </li>
        ))}
      </ol>

      {pocket.missing_intervals.length > 0 && (
        <p className="form-error">Missing: {pocket.missing_intervals.map(iv => `${iv.start}→${iv.end}`).join(', ')}</p>
      )}
      {pocket.warnings.map((warning, i) => <p key={i} className="muted-note"><AlertTriangle size={13} /> {warning}</p>)}
    </Card>
  )
}

function ratioText(ratio: Ratio): string {
  return ratio.available ? ratio.value : `Unavailable — ${ratio.reason}`
}

function EstimateSection({ estimate, onOpenTransaction }: { estimate: BaselineEstimate; onOpenTransaction?: (encodedAnchor: string) => Promise<void> }) {
  if (!estimate.available) {
    return (
      <Card className="baseline-window-card">
        <div className="panel-title"><div><p className="eyebrow">What your normal life costs</p><h2>Not derivable yet</h2></div></div>
        <p className="muted-note">{estimate.reason}</p>
      </Card>
    )
  }
  return (
    <>
      {estimate.currencies.map(currency => (
        <EstimateCard key={currency} currency={currency} est={estimate.by_currency[currency]} quality={estimate.sample.quality} monthCount={estimate.sample.month_count} onOpenTransaction={onOpenTransaction} />
      ))}
      <Card className="baseline-tasks-card">
        <div className="panel-title"><div><p className="eyebrow">Method &amp; limits</p><h2>{estimate.sample.method_version}</h2></div></div>
        <p className="muted-note">{estimate.sample.month_count} month sample · {estimate.sample.residual_method} · {estimate.sample.outlier_method}</p>
        <ul className="issue-list">
          {estimate.limitations.map((limit, i) => <li key={i}><p>{limit}</p></li>)}
        </ul>
        {estimate.income_streams.length > 0 && (
          <>
            <p className="eyebrow" style={{ marginTop: 16 }}>Likely income streams</p>
            <ul className="issue-list">
              {estimate.income_streams.map((stream, i) => (
                <li key={i}>
                  <strong>{stream.payer}</strong> · {stream.currency}
                  <p>{stream.occurrence_count} receipts {stream.first_month}–{stream.last_month} · {reportedMoney(stream, 'min_amount', stream.currency)}–{reportedMoney(stream, 'max_amount', stream.currency)} · Likely, never Confirmed</p>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </>
  )
}

function rangeText(range: ObservedRange, currency: string): string {
  return `${reportedMoney(range, 'min_amount', currency)} (${range.min_month}) to ${reportedMoney(range, 'max_amount', currency)} (${range.max_month})`
}

function EstimateCard({ currency, est, quality, monthCount, onOpenTransaction }: { currency: string; est: CurrencyEstimate; quality: string; monthCount: number; onOpenTransaction?: (encodedAnchor: string) => Promise<void> }) {
  const [drillMonth, setDrillMonth] = useState<string | null>(null)
  const spread = est.observed_range
  return (
    <Card className="baseline-pocket-card">
      <div className="panel-title">
        <div><p className="eyebrow">What your normal life costs · {currency}</p><h2>{reportedMoney(est, 'routine_lifestyle_baseline', currency)} / month</h2></div>
        <Badge variant="outline" className={quality === 'normal' ? '' : 'review-badge'}>{QUALITY_LABEL[quality] ?? quality}</Badge>
      </div>
      <p className="muted-note">
        Observed months ran from {rangeText(spread.routine_lifestyle_baseline, currency)} across {monthCount} month{monthCount === 1 ? '' : 's'}. This is the spread of what happened, not a confidence interval.
      </p>
      {est.regime.warning && (
        <p className="muted-note"><AlertTriangle size={13} /> Regime: {est.regime.reasons.join('; ')}.</p>
      )}
      <dl className="baseline-metrics">
        <div><dt>Fixed recurring</dt><dd>{reportedMoney(est, 'fixed_recurring_monthly', currency)}</dd></div>
        <div><dt>Variable residual (mean)</dt><dd>{reportedMoney(est, 'variable_residual_monthly', currency)}</dd></div>
        <div><dt>· median / min / max</dt><dd>{reportedMoney(est, 'variable_residual_median', currency)} · {reportedMoney(est, 'variable_residual_min', currency)} · {reportedMoney(est, 'variable_residual_max', currency)}</dd></div>
        <div><dt>Irregular allowance (mean)</dt><dd>{reportedMoney(est, 'irregular_allowance_monthly', currency)}</dd></div>
        <div><dt>· observed range</dt><dd>{rangeText(spread.irregular_allowance_monthly, currency)} · {spread.irregular_allowance_monthly.months_with_entries} of {monthCount} month{monthCount === 1 ? '' : 's'} with held-out entries</dd></div>
        {est.trip_allowance_monthly && <div><dt>Trip allowance (mean)</dt><dd>{reportedMoney(est, 'trip_allowance_monthly', currency)}{est.trip_allowance_projected ? '' : ' · not projected'}</dd></div>}
        <div><dt>Total observed monthly</dt><dd>{reportedMoney(est, 'total_observed_monthly', currency)}</dd></div>
        <div><dt>· observed range</dt><dd>{rangeText(spread.total_observed_monthly, currency)}</dd></div>
        <div><dt>Net received income (mean)</dt><dd>{reportedMoney(est, 'net_received_income_monthly', currency)}</dd></div>
        <div><dt>Historical surplus</dt><dd>{reportedMoney(est, 'historical_surplus_monthly', currency)}</dd></div>
        <div><dt>Savings rate</dt><dd>{ratioText(est.savings_rate)}</dd></div>
        <div><dt>Fixed-cost ratio</dt><dd>{ratioText(est.fixed_cost_ratio)}</dd></div>
        <div><dt>Sensitivity (highest month removed)</dt><dd>{est.sensitivity.available
          ? `${reportedMoney(est.sensitivity, 'routine_lifestyle_baseline', currency)} / month without ${est.sensitivity.excluded_month} — a spread check, not a probability`
          : est.sensitivity.reason}</dd></div>
        {est.roles && <>
          <div><dt>Essential (sample total)</dt><dd>{reportedMoney(est.roles.totals, 'essential', currency)}</dd></div>
          <div><dt>Discretionary (sample total)</dt><dd>{reportedMoney(est.roles.totals, 'discretionary', currency)}</dd></div>
          <div><dt>Unresolved (sample total)</dt><dd>{reportedMoney(est.roles.totals, 'unresolved', currency)}</dd></div>
        </>}
        <div><dt>Minimum reserve</dt><dd>{est.reserve.available ? `${reportedMoney(est.reserve, 'amount', currency)} · ${est.reserve.basis}` : `Unavailable — ${est.reserve.reason}`}</dd></div>
      </dl>
      {est.roles && <table className="pocket-table">
        <thead><tr><th>Month</th><th>Essential</th><th>Discretionary</th><th>Unresolved</th><th>Total</th></tr></thead>
        <tbody>{est.roles.months.map(row => <tr key={row.month}><td>{row.month}</td><td>{reportedMoney(row, 'essential', currency)}</td><td>{reportedMoney(row, 'discretionary', currency)}</td><td>{reportedMoney(row, 'unresolved', currency)}</td><td>{reportedMoney(row, 'total', currency)}{!row.reconciles && <small className="form-error">does not reconcile</small>}</td></tr>)}</tbody>
      </table>}
      <p className="muted-note">Role defaults are source-backed category rules; confirmed recurring occurrences are marked separately. Unmapped categories and unlinked receipts stay unresolved for review.</p>
      {est.role_groups.length > 0 && <table className="pocket-table">
        <thead><tr><th>Category / source group</th><th>Pattern</th><th>Need</th><th>Tier</th><th>Review</th><th>Observed cost</th></tr></thead>
        <tbody>{est.role_groups.map((group, i) => <tr key={`${group.category_key}:${group.cost_pattern}:${i}`}><td>{group.category_key}<small>{group.source_count} source row{group.source_count === 1 ? '' : 's'}</small></td><td>{group.cost_pattern.replaceAll('_', ' ')}</td><td>{group.essential_status}</td><td>{group.tier === null ? 'Unresolved' : `Tier ${group.tier}`}</td><td title={group.basis}>{group.review_status}</td><td>{reportedMoney(group, 'amount', currency)}</td></tr>)}</tbody>
      </table>}
      <table className="pocket-table">
        <thead><tr><th>Month</th><th>Owner cost</th><th>Recurring</th><th>Trips</th><th>Irregular</th><th>Adjustments</th><th>Routine residual</th><th>Contributors</th></tr></thead>
        <tbody>
          {est.months.map(row => (
            <tr key={row.month}>
              <td>{row.month}</td>
              <td>{reportedMoney(row, 'owner_cost_recorded', currency)}</td>
              <td>{reportedMoney(row, 'identified_recurring', currency)}</td>
              <td>{reportedMoney(row, 'trip', currency)}</td>
              <td>{reportedMoney(row, 'unresolved_irregular', currency)}</td>
              <td>{reportedMoney(row, 'adjustments', currency)}</td>
              <td>{reportedMoney(row, 'routine_variable_residual', currency)}{!row.reconciles && <small className="form-error">does not reconcile</small>}</td>
              <td><button className="linkish" aria-expanded={drillMonth === row.month} onClick={() => setDrillMonth(drillMonth === row.month ? null : row.month)}>{drillMonth === row.month ? 'Hide' : 'Show'} {row.month}</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {drillMonth && <BaselineContributors currency={currency} month={drillMonth} onClose={() => setDrillMonth(null)} onOpenTransaction={onOpenTransaction} />}
      {est.irregular_risk_entries.length > 0 && (
        <p className="muted-note"><AlertTriangle size={13} /> {est.irregular_risk_entries.length} entr{est.irregular_risk_entries.length === 1 ? 'y' : 'ies'} held out as irregular risk (still in the historical total, out of the routine residual).</p>
      )}
    </Card>
  )
}
