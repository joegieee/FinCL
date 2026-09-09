import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, CalendarRange, CheckCircle2, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { loadBaselineReadiness, type BaselineReadiness, type Completeness, type PocketReadiness } from './api'

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
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    loadBaselineReadiness()
      .then(p => { if (id === seq.current) setPage(p) })
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
