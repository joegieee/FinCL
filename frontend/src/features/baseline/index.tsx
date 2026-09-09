import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CalendarRange, CheckCircle2, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { money } from '@/lib/api'
import { loadBaselineEstimate, loadBaselineReadiness, type BaselineEstimate, type BaselineReadiness, type Completeness, type CurrencyEstimate, type PocketReadiness, type Ratio } from './api'

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
export function BaselineView() {
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

  if (loading && !page) return <div className="skeleton-page" aria-label="Loading baseline readiness"><div /><div /><div /></div>
  if (error && !page) {
    return <div className="alert error" role="alert"><div><strong>We couldn’t load baseline readiness.</strong><p>{error}</p></div><Button variant="outline" onClick={load}>Try again</Button></div>
  }
  if (!page) return null

  const w = page.baseline_window
  const analysis = page.scope.analysis_window

  return (
    <div className="baseline-view">
      <div className="scope-caption">
        <span>{page.pockets.length} pocket{page.pockets.length === 1 ? '' : 's'} · {page.scope.currencies.join(', ') || 'no currency'}{analysis && ` · evidence ${analysis.start} → ${analysis.end}`}</span>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button>
      </div>

      <Card className="baseline-window-card">
        <div className="panel-title">
          <div>
            <p className="eyebrow">RECOMMENDED BASELINE WINDOW</p>
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

      {estimate && <EstimateSection estimate={estimate} />}

      <Card className="baseline-tasks-card">
        <div className="panel-title"><div><p className="eyebrow">BEFORE A BASELINE</p><h2>{page.tasks.length === 0 ? 'Nothing blocking' : `${page.tasks.length} evidence task${page.tasks.length === 1 ? '' : 's'}`}</h2></div></div>
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
          <p className="eyebrow">{pocket.institution} · {pocket.source_type}</p>
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

function EstimateSection({ estimate }: { estimate: BaselineEstimate }) {
  if (!estimate.available) {
    return (
      <Card className="baseline-window-card">
        <div className="panel-title"><div><p className="eyebrow">WHAT YOUR NORMAL LIFE COSTS</p><h2>Not derivable yet</h2></div></div>
        <p className="muted-note">{estimate.reason}</p>
      </Card>
    )
  }
  return (
    <>
      {estimate.currencies.map(currency => (
        <EstimateCard key={currency} currency={currency} est={estimate.by_currency[currency]} quality={estimate.sample.quality} />
      ))}
      <Card className="baseline-tasks-card">
        <div className="panel-title"><div><p className="eyebrow">METHOD &amp; LIMITS</p><h2>{estimate.sample.method_version}</h2></div></div>
        <p className="muted-note">{estimate.sample.month_count} month sample · {estimate.sample.residual_method} · {estimate.sample.outlier_method}</p>
        <ul className="issue-list">
          {estimate.limitations.map((limit, i) => <li key={i}><p>{limit}</p></li>)}
        </ul>
        {estimate.income_streams.length > 0 && (
          <>
            <p className="eyebrow" style={{ marginTop: 16 }}>LIKELY INCOME STREAMS</p>
            <ul className="issue-list">
              {estimate.income_streams.map((stream, i) => (
                <li key={i}>
                  <strong>{stream.payer}</strong> · {stream.currency}
                  <p>{stream.occurrence_count} receipts {stream.first_month}–{stream.last_month} · {money(stream.min_amount, stream.currency)}–{money(stream.max_amount, stream.currency)} · Likely, never Confirmed</p>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>
    </>
  )
}

function EstimateCard({ currency, est, quality }: { currency: string; est: CurrencyEstimate; quality: string }) {
  return (
    <Card className="baseline-pocket-card">
      <div className="panel-title">
        <div><p className="eyebrow">WHAT YOUR NORMAL LIFE COSTS · {currency}</p><h2>{money(est.routine_lifestyle_baseline, currency)} / month</h2></div>
        <Badge variant="outline" className={quality === 'normal' ? '' : 'review-badge'}>{QUALITY_LABEL[quality] ?? quality}</Badge>
      </div>
      <dl className="baseline-metrics">
        <div><dt>Fixed recurring</dt><dd>{money(est.fixed_recurring_monthly, currency)}</dd></div>
        <div><dt>Variable residual (mean)</dt><dd>{money(est.variable_residual_monthly, currency)}</dd></div>
        <div><dt>· median / min / max</dt><dd>{money(est.variable_residual_median, currency)} · {money(est.variable_residual_min, currency)} · {money(est.variable_residual_max, currency)}</dd></div>
        <div><dt>Irregular allowance (mean)</dt><dd>{money(est.irregular_allowance_monthly, currency)}</dd></div>
        <div><dt>Total observed monthly</dt><dd>{money(est.total_observed_monthly, currency)}</dd></div>
        <div><dt>Net received income (mean)</dt><dd>{money(est.net_received_income_monthly, currency)}</dd></div>
        <div><dt>Historical surplus</dt><dd>{money(est.historical_surplus_monthly, currency)}</dd></div>
        <div><dt>Savings rate</dt><dd>{ratioText(est.savings_rate)}</dd></div>
        <div><dt>Fixed-cost ratio</dt><dd>{ratioText(est.fixed_cost_ratio)}</dd></div>
        {est.roles && <>
          <div><dt>Essential (sample total)</dt><dd>{money(est.roles.totals.essential, currency)}</dd></div>
          <div><dt>Discretionary (sample total)</dt><dd>{money(est.roles.totals.discretionary, currency)}</dd></div>
          <div><dt>Unresolved (sample total)</dt><dd>{money(est.roles.totals.unresolved, currency)}</dd></div>
        </>}
        <div><dt>Minimum reserve</dt><dd>{est.reserve.available ? `${money(est.reserve.amount, currency)} · ${est.reserve.basis}` : `Unavailable — ${est.reserve.reason}`}</dd></div>
      </dl>
      {est.roles && <table className="pocket-table">
        <thead><tr><th>Month</th><th>Essential</th><th>Discretionary</th><th>Unresolved</th><th>Total</th></tr></thead>
        <tbody>{est.roles.months.map(row => <tr key={row.month}><td>{row.month}</td><td>{money(row.essential, currency)}</td><td>{money(row.discretionary, currency)}</td><td>{money(row.unresolved, currency)}</td><td>{money(row.total, currency)}{!row.reconciles && <small className="form-error">does not reconcile</small>}</td></tr>)}</tbody>
      </table>}
      <p className="muted-note">Role defaults are source-backed category rules; confirmed recurring occurrences are marked separately. Unmapped categories and unlinked receipts stay unresolved for review.</p>
      {est.role_groups.length > 0 && <table className="pocket-table">
        <thead><tr><th>Category / source group</th><th>Pattern</th><th>Need</th><th>Tier</th><th>Review</th><th>Observed cost</th></tr></thead>
        <tbody>{est.role_groups.map((group, i) => <tr key={`${group.category_key}:${group.cost_pattern}:${i}`}><td>{group.category_key}<small>{group.source_count} source row{group.source_count === 1 ? '' : 's'}</small></td><td>{group.cost_pattern.replaceAll('_', ' ')}</td><td>{group.essential_status}</td><td>{group.tier === null ? 'Unresolved' : `Tier ${group.tier}`}</td><td title={group.basis}>{group.review_status}</td><td>{money(group.amount, currency)}</td></tr>)}</tbody>
      </table>}
      <table className="pocket-table">
        <thead><tr><th>Month</th><th>Owner cost</th><th>Recurring</th><th>Irregular</th><th>Adjustments</th><th>Routine residual</th></tr></thead>
        <tbody>
          {est.months.map(row => (
            <tr key={row.month}>
              <td>{row.month}</td>
              <td>{money(row.owner_cost_recorded, currency)}</td>
              <td>{money(row.identified_recurring, currency)}</td>
              <td>{money(row.unresolved_irregular, currency)}</td>
              <td>{money(row.adjustments, currency)}</td>
              <td>{money(row.routine_variable_residual, currency)}{!row.reconciles && <small className="form-error">does not reconcile</small>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {est.irregular_risk_entries.length > 0 && (
        <p className="muted-note"><AlertTriangle size={13} /> {est.irregular_risk_entries.length} entr{est.irregular_risk_entries.length === 1 ? 'y' : 'ies'} held out as irregular risk (still in the historical total, out of the routine residual).</p>
      )}
    </Card>
  )
}
