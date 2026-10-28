import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, FileUp, RefreshCw, ShieldCheck, Upload, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { AsyncView } from '@/components/AsyncView'
import { ApiError } from '@/lib/api'
import { dateLabel } from '@/lib/format'
import {
  ADAPTERS, MAX_BYTES, MAX_FILES,
  loadImportDetail, loadImports, loadSetup, refreshImports, submitFxRates, submitImport,
  type FxRatesResult, type ImportDetail, type ImportRun, type SetupOptions, type UploadOutcome, type UploadResult,
} from './api'

function when(value: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('en-SG', { dateStyle: 'medium', timeStyle: 'short' })
}
function periodLabel(start: string | null | undefined, end: string | null | undefined) {
  if (!start || !end) return null
  return start === end ? dateLabel(start) : `${dateLabel(start)} – ${dateLabel(end)}`
}
function supersededLabel(period: string) {
  const [start, end] = period.split('..')
  return periodLabel(start, end) ?? period
}

const OUTCOME_LABEL: Record<string, string> = {
  ACCEPTED: 'Added',
  ALREADY_IMPORTED: 'Already imported',
  QUARANTINED: 'Not added',
  CONFLICT: 'Conflicting copy',
  FAILED: 'Failed',
  RUNNING: 'Running',
}

function sizeLabel(bytes: number | null) {
  if (!bytes) return '—'
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KiB` : `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

/**
 * v0.5 slice 1 — guided setup state, the single multipart import wizard, and
 * import history. Successful files survive a later file's failure; the chosen
 * adapter and file selection are local draft state and never enter the URL.
 */
/** Accepted imports refresh the ledger before notifying the surrounding app.
 * `onReview` optionally opens the next review workflow. */
export function SetupView({ onImported, onReview }: { onImported: () => void; onReview?: () => void | Promise<void> }) {
  const [setup, setSetup] = useState<SetupOptions | null>(null)
  const [imports, setImports] = useState<ImportRun[] | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<ImportDetail | null>(null)
  const seq = useRef(0)

  const load = useCallback(() => {
    const id = ++seq.current
    setLoading(true); setError('')
    Promise.all([loadSetup(), loadImports()])
      .then(([s, i]) => { if (id === seq.current) { setSetup(s); setImports(i.imports) } })
      .catch(e => { if (id === seq.current) setError((e as Error).message) })
      .finally(() => { if (id === seq.current) setLoading(false) })
  }, [])
  useEffect(() => { load() }, [load])

  return (
    <AsyncView loading={loading} error={error} data={setup} onRetry={load} loadingLabel="Loading setup" errorTitle="We couldn’t load setup.">
      {setup => <SetupContent setup={setup} imports={imports} loading={loading} load={load} onImported={onImported} onReview={onReview} detail={detail} setDetail={setDetail} setError={setError} />}
    </AsyncView>
  )
}

function SetupContent({ setup, imports, loading, load, onImported, onReview, detail, setDetail, setError }: {
  setup: SetupOptions; imports: ImportRun[] | null; loading: boolean; load: () => void; onImported: () => void; onReview?: () => void | Promise<void>
  detail: ImportDetail | null; setDetail: (d: ImportDetail | null) => void; setError: (e: string) => void
}) {
  return (
    <div className="setup-view">
      <Card className="setup-summary">
        <div className="panel-title"><div><p className="eyebrow">THIS LEDGER</p><h2>{setup.current_ledger}</h2></div>
          <Badge variant="outline">{setup.state === 'ready' ? 'Ready' : setup.state}</Badge></div>
        <dl className="detail-list">
          <div><dt>Storage profile</dt><dd>{setup.storage_profile}</dd></div>
          <div><dt>Workspace root</dt><dd className="mono">{setup.workspace_root}</dd></div>
        </dl>
        <p className="muted-note"><ShieldCheck size={13} /> New ledgers and switching are granted by the host launcher (<code>fincl setup --workspace-root</code>). This screen never takes an absolute path from the browser.</p>
      </Card>

      <ImportWizard onDone={() => { load(); onImported() }} onReview={onReview} />

      <FxRatesWizard />

      <Card className="import-history">
        <div className="panel-title"><div><p className="eyebrow">IMPORT HISTORY</p><h2>Every import, with its evidence</h2></div>
          <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button></div>
        {imports && imports.length === 0 && (
          <div className="empty"><FileUp size={24} /><h3>No imports yet</h3><p>Upload your first statement above. Accepted files, reused evidence and failures are all listed here.</p></div>
        )}
        {imports && imports.length > 0 && (
          <div className="table-scroll">
            <table className="import-table">
              <thead><tr><th>Started</th><th>File</th><th>Account</th><th>Period</th><th>Rows</th><th>Outcome</th><th>Issues</th><th><span className="sr-only">Detail</span></th></tr></thead>
              <tbody>
                {imports.map(run => (
                  <tr key={run.id}>
                    <td>{when(run.started_at)}</td>
                    <td>{run.filename || '—'}<small>{sizeLabel(run.byte_size)}</small></td>
                    <td>{run.account_display_name || '—'}</td>
                    <td>{periodLabel(run.period_start, run.period_end) ?? '—'}</td>
                    <td>{run.transaction_count ?? '—'}</td>
                    <td><Badge variant="outline" className={run.outcome === 'ACCEPTED' || run.outcome === 'ALREADY_IMPORTED' ? '' : 'review-badge'}>{OUTCOME_LABEL[run.outcome] ?? run.outcome}</Badge>{run.extraction_reused ? <small>same file again</small> : null}</td>
                    <td>{run.blockers > 0 ? <span className="issue-count">{run.blockers} blocking</span> : run.error_message ? 'error' : 'none'}</td>
                    <td><Button variant="ghost" size="sm" onClick={() => { setDetail(null); loadImportDetail(run.id).then(setDetail).catch(e => setError((e as Error).message)) }}>Open</Button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {detail && (
        <Dialog open onOpenChange={open => { if (!open) setDetail(null) }}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Import {when(detail.started_at)}</DialogTitle>
              <DialogDescription>Preserved evidence, parser version and remaining issues.</DialogDescription>
            </DialogHeader>
            <dl className="detail-list">
              <div><dt>Outcome</dt><dd>{OUTCOME_LABEL[detail.outcome] ?? detail.outcome}</dd></div>
              {detail.filename && <div><dt>File</dt><dd>{detail.filename}</dd></div>}
              {detail.account_display_name && <div><dt>Account</dt><dd>{detail.account_display_name}</dd></div>}
              {periodLabel(detail.period_start, detail.period_end) && <div><dt>Period</dt><dd>{periodLabel(detail.period_start, detail.period_end)}</dd></div>}
              <div><dt>Evidence SHA-256</dt><dd className="mono">{detail.evidence_sha256 || '—'}</dd></div>
              <div><dt>Source profile</dt><dd>{detail.source_profile_version || '—'}</dd></div>
              <div><dt>Selection state</dt><dd>{detail.selection_state || '—'}</dd></div>
            </dl>
            {detail.error_message && <p className="form-error" role="alert">{detail.error_message}</p>}
            {detail.superseded_by && (
              <p className="muted-note">Nothing was added: the statement for {supersededLabel(detail.superseded_by)} already has every row of this one.</p>
            )}
            {detail.conflict_with.length > 0 && (
              <div className="alert" role="note">
                <div>
                  <strong>Two different files claim to be the same statement.</strong>
                  <p>Their rows disagree, so neither is counted until one is removed. Check which one the bank issued.</p>
                  <ul className="issue-list">
                    {detail.conflict_with.map(copy => (
                      <li key={copy.evidence_sha256}>
                        <strong>{copy.filename ?? copy.evidence_sha256.slice(0, 12)}</strong>
                        {copy.this_import ? ' (this import)' : ''} · {periodLabel(copy.period_start, copy.period_end) ?? '—'}
                        {copy.imported_at && <small>imported {when(copy.imported_at)}</small>}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
            {detail.issues.length > 0 ? (
              <ul className="issue-list">
                {detail.issues.map((issue, i) => (
                  <li key={i}><Badge variant="outline" className={issue.severity === 'BLOCKING' ? 'review-badge' : ''}>{issue.severity}</Badge> <strong>{issue.code}</strong><p>{issue.message}</p></li>
                ))}
              </ul>
            ) : <p className="muted-note">No recorded issues for this import.</p>}
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}

function ImportWizard({ onDone, onReview }: { onDone: () => void; onReview?: () => void | Promise<void> }) {
  const [adapter, setAdapter] = useState(ADAPTERS[0].id)
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [result, setResult] = useState<UploadResult | null>(null)
  const [error, setError] = useState('')
  const [refreshError, setRefreshError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const oversize = files.find(f => f.size > MAX_BYTES)
  const tooMany = files.length > MAX_FILES
  const canSubmit = files.length > 0 && !oversize && !tooMany && !busy && !reviewing

  async function refreshAccepted() {
    setReviewing(true); setRefreshError('')
    try {
      await refreshImports()
    } catch (e) {
      setRefreshError((e as Error).message)
    } finally {
      setReviewing(false)
      onDone()
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true); setError(''); setRefreshError(''); setResult(null)
    try {
      const outcome = await submitImport(adapter, files)
      setResult(outcome)
      setFiles([])
      if (inputRef.current) inputRef.current.value = ''
      // A quarantined copy can still demote a statement it conflicts with.
      if (outcome.accepted + (outcome.quarantined ?? 0) > 0) await refreshAccepted()
      else onDone()
    } catch (e) {
      // Draft (adapter + file selection) is intentionally preserved on failure.
      setError(e instanceof ApiError ? e.message : (e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="import-wizard">
      <div className="panel-title"><div><p className="eyebrow">IMPORT A STATEMENT</p><h2>Add transactions from your bank</h2></div></div>
      <form onSubmit={submit}>
        <label>Source
          <select aria-label="Statement source" value={adapter} disabled={busy || reviewing} onChange={e => { setAdapter(e.target.value); setResult(null); setRefreshError('') }}>
            {ADAPTERS.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
        </label>
        <label>Files
          <input
            ref={inputRef}
            type="file"
            multiple
            accept=".pdf,application/pdf"
            aria-label="Statement files"
            disabled={busy || reviewing}
            onChange={e => { setFiles(Array.from(e.target.files ?? [])); setResult(null); setError(''); setRefreshError('') }}
          />
          <small>PDF statements only; you can mix banks when recognising automatically. Up to {MAX_FILES} files, {Math.round(MAX_BYTES / 1024 / 1024)} MiB each.</small>
        </label>
        {files.length > 0 && (
          <ul className="file-chips">
            {files.map((f, i) => (
              <li key={i} className={f.size > MAX_BYTES ? 'over' : ''}>
                <span>{f.name}</span><small>{sizeLabel(f.size)}</small>
              </li>
            ))}
          </ul>
        )}
        {oversize && <p className="form-error" role="alert"><AlertTriangle size={14} /> {oversize.name} is over the {Math.round(MAX_BYTES / 1024 / 1024)} MiB limit.</p>}
        {tooMany && <p className="form-error" role="alert"><AlertTriangle size={14} /> Choose {MAX_FILES} files or fewer.</p>}
        {error && (
          <div className="alert error" role="alert">
            <div><strong>The upload didn’t complete.</strong><p>{error}</p><p className="muted-note">Your source and file selection are kept. Retrying identical bytes reuses the existing evidence.</p></div>
          </div>
        )}
        <div className="dialog-actions">
          <Button type="submit" disabled={!canSubmit}>{busy ? <>Uploading… <RefreshCw size={15} className="spin" /></> : <>Upload statements <Upload size={15} /></>}</Button>
        </div>
      </form>
      {result && (
        <div className="upload-result" role="status">
          <p>{result.accepted > 0 ? <Check size={15} /> : <AlertTriangle size={15} />} {result.accepted} of {result.results.length} file{result.results.length === 1 ? '' : 's'} accepted.
            {result.quarantined ? ` ${result.quarantined} could not be added.` : ''}</p>
          {result.evidence_through && result.evidence_through.length > 0 && (
            <ul className="evidence-through">
              {result.evidence_through.map(item => (
                <li key={item.account_id}>{item.display_name}: statements now reach {item.through ? dateLabel(item.through) : '—'}</li>
              ))}
            </ul>
          )}
          {reviewing && <p>Refreshing ledger…</p>}
          {refreshError && <div className="alert error" role="alert"><div><strong>Your statements were accepted, but the ledger refresh failed.</strong><p>{refreshError}</p><p>Retry the refresh to make the new transactions visible.</p></div></div>}
          <ul className="issue-list">
            {result.results.map((r, i) => (
              <li key={i}>
                <Badge variant="outline" className={r.status === 'ACCEPTED' ? '' : 'review-badge'}>{r.status === 'UNRECOGNISED' ? 'NOT RECOGNISED' : r.status}</Badge>
                <strong>{r.filename}</strong>
                {statementLine(r) && <small>{statementLine(r)}</small>}
                {r.detected && r.adapter ? <small>recognised as {ADAPTERS.find(a => a.id === r.adapter)?.label ?? r.adapter}</small> : null}
                {r.evidence_reused ? <small>evidence reused</small> : null}
                {r.superseded_by ? <p>Nothing new: the statement for {supersededLabel(r.superseded_by)} already has every row of this one.</p> : null}
                {r.supersedes && r.supersedes.length > 0 ? <p>Replaces the shorter statement{r.supersedes.length === 1 ? '' : 's'} for {r.supersedes.map(supersededLabel).join(', ')}, so no row is counted twice.</p> : null}
                {r.error ? <p className="form-error">{r.error}</p> : null}
                {r.blocking_issue_count ? <p>{r.blocking_issue_count} blocking issue{r.blocking_issue_count === 1 ? '' : 's'} — open the import in history for details.</p> : null}
              </li>
            ))}
          </ul>
          <div className="dialog-actions">
            <Button variant="ghost" size="sm" disabled={busy || reviewing} onClick={() => setResult(null)}><X size={14} /> Dismiss</Button>
            {refreshError && <Button size="sm" disabled={busy || reviewing} onClick={() => { void refreshAccepted() }}>Retry ledger refresh</Button>}
            {onReview && result.accepted > 0 && !refreshError && <Button size="sm" disabled={busy || reviewing} onClick={async () => { setReviewing(true); try { await onReview() } catch (e) { setRefreshError((e as Error).message) } finally { setReviewing(false) } }}>
              {reviewing ? <>Refreshing… <RefreshCw size={14} className="spin" /></> : 'Review new transactions'}
            </Button>}
          </div>
        </div>
      )}
    </Card>
  )
}

function statementLine(r: UploadOutcome) {
  if (!r.account_display_name) return null
  const period = periodLabel(r.period_start, r.period_end)
  const rows = r.transaction_count === undefined ? '' : ` · ${r.transaction_count} row${r.transaction_count === 1 ? '' : 's'}`
  return `${r.account_display_name}${period ? ` · ${period}` : ''}${rows}`
}

function FxRatesWizard() {
  const [file, setFile] = useState<File | null>(null)
  const [masWeeklyCsv, setMasWeeklyCsv] = useState(false)
  const [sourceVersion, setSourceVersion] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<FxRatesResult | null>(null)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const needsVersion = masWeeklyCsv && !sourceVersion.trim()
  const canSubmit = !!file && !needsVersion && !busy

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!file || !canSubmit) return
    setBusy(true); setError(''); setResult(null)
    try {
      const outcome = await submitFxRates(file, masWeeklyCsv, sourceVersion.trim())
      setResult(outcome)
      setFile(null)
      if (inputRef.current) inputRef.current.value = ''
    } catch (e) {
      // Draft (file, format, source version) is intentionally preserved on failure.
      setError(e instanceof ApiError ? e.message : (e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="import-wizard fx-rates-wizard">
      <div className="panel-title"><div><p className="eyebrow">LOAD FX RATES</p><h2>Add reference exchange rates</h2></div></div>
      <form onSubmit={submit}>
        <label>File
          <input
            ref={inputRef}
            type="file"
            aria-label="FX rate document"
            disabled={busy}
            onChange={e => { setFile(e.target.files?.[0] ?? null); setResult(null); setError('') }}
          />
          <small>A JSON rate document, or a MAS weekly CSV with the option below.</small>
        </label>
        <label className="inline-check">
          <input
            type="checkbox"
            checked={masWeeklyCsv}
            disabled={busy}
            onChange={e => { setMasWeeklyCsv(e.target.checked); setResult(null) }}
          /> Parse as a MAS “Exchange Rates (Weekly)” CSV
        </label>
        {masWeeklyCsv && (
          <label>Source version
            <input
              type="text"
              placeholder="mas-weekly-2026-09"
              disabled={busy}
              value={sourceVersion}
              onChange={e => { setSourceVersion(e.target.value); setResult(null) }}
            />
            <small>An immutable label for this rate set. Reimporting it later must supply the identical rates.</small>
          </label>
        )}
        {error && (
          <div className="alert error" role="alert">
            <div><strong>The upload didn’t complete.</strong><p>{error}</p></div>
          </div>
        )}
        <div className="dialog-actions">
          <Button type="submit" disabled={!canSubmit}>{busy ? <>Uploading… <RefreshCw size={15} className="spin" /></> : <>Upload rates <Upload size={15} /></>}</Button>
        </div>
      </form>
      {result && (
        <div className="upload-result" role="status">
          <p><Check size={15} /> {result.rates_written} rate{result.rates_written === 1 ? '' : 's'} loaded.</p>
          <Button variant="ghost" size="sm" onClick={() => setResult(null)}><X size={14} /> Dismiss</Button>
        </div>
      )}
    </Card>
  )
}
