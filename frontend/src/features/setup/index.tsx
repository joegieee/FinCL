import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, FileUp, RefreshCw, ShieldCheck, Upload, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { AsyncView } from '@/components/AsyncView'
import { ApiError } from '@/lib/api'
import {
  ADAPTERS, MAX_BYTES, MAX_FILES,
  loadImportDetail, loadImports, loadSetup, submitImport,
  type ImportDetail, type ImportRun, type SetupOptions, type UploadResult,
} from './api'

function when(value: string | null) {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('en-SG', { dateStyle: 'medium', timeStyle: 'short' })
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
export function SetupView({ onImported }: { onImported: () => void }) {
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
      {setup => <SetupContent setup={setup} imports={imports} loading={loading} load={load} onImported={onImported} detail={detail} setDetail={setDetail} setError={setError} />}
    </AsyncView>
  )
}

function SetupContent({ setup, imports, loading, load, onImported, detail, setDetail, setError }: {
  setup: SetupOptions; imports: ImportRun[] | null; loading: boolean; load: () => void; onImported: () => void
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

      <ImportWizard onDone={() => { load(); onImported() }} />

      <Card className="import-history">
        <div className="panel-title"><div><p className="eyebrow">IMPORT HISTORY</p><h2>Every import, with its evidence</h2></div>
          <Button variant="ghost" size="sm" onClick={load} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} /> Refresh</Button></div>
        {imports && imports.length === 0 && (
          <div className="empty"><FileUp size={24} /><h3>No imports yet</h3><p>Upload your first statement above. Accepted files, reused evidence and failures are all listed here.</p></div>
        )}
        {imports && imports.length > 0 && (
          <div className="table-scroll">
            <table className="import-table">
              <thead><tr><th>Started</th><th>Outcome</th><th>Size</th><th>Parser</th><th>Issues</th><th><span className="sr-only">Detail</span></th></tr></thead>
              <tbody>
                {imports.map(run => (
                  <tr key={run.id}>
                    <td>{when(run.started_at)}</td>
                    <td><Badge variant="outline" className={run.status === 'FAILED' ? 'review-badge' : ''}>{run.status}</Badge>{run.extraction_reused ? <small>evidence reused</small> : null}</td>
                    <td>{sizeLabel(run.byte_size)}</td>
                    <td>{run.parser_version || '—'}</td>
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
              <div><dt>Outcome</dt><dd>{detail.status}</dd></div>
              <div><dt>Evidence SHA-256</dt><dd className="mono">{detail.evidence_sha256 || '—'}</dd></div>
              <div><dt>Source profile</dt><dd>{detail.source_profile_version || '—'}</dd></div>
              <div><dt>Selection state</dt><dd>{detail.selection_state || '—'}</dd></div>
            </dl>
            {detail.error_message && <p className="form-error" role="alert">{detail.error_message}</p>}
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

function ImportWizard({ onDone }: { onDone: () => void }) {
  const [adapter, setAdapter] = useState(ADAPTERS[0].id)
  const [files, setFiles] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<UploadResult | null>(null)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const oversize = files.find(f => f.size > MAX_BYTES)
  const tooMany = files.length > MAX_FILES
  const canSubmit = files.length > 0 && !oversize && !tooMany && !busy

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true); setError(''); setResult(null)
    try {
      const outcome = await submitImport(adapter, files)
      setResult(outcome)
      setFiles([])
      if (inputRef.current) inputRef.current.value = ''
      onDone()
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
          <select aria-label="Statement source" value={adapter} disabled={busy} onChange={e => { setAdapter(e.target.value); setResult(null) }}>
            {ADAPTERS.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
        </label>
        <label>Files
          <input
            ref={inputRef}
            type="file"
            multiple
            aria-label="Statement files"
            disabled={busy}
            onChange={e => { setFiles(Array.from(e.target.files ?? [])); setResult(null); setError('') }}
          />
          <small>Up to {MAX_FILES} files, {Math.round(MAX_BYTES / 1024 / 1024)} MiB each. Filenames are display text only.</small>
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
          <Button type="submit" disabled={!canSubmit}>{busy ? <>Uploading… <RefreshCw size={15} className="spin" /></> : <>Upload <Upload size={15} /></>}</Button>
        </div>
      </form>
      {result && (
        <div className="upload-result" role="status">
          <p><Check size={15} /> {result.accepted} of {result.results.length} file{result.results.length === 1 ? '' : 's'} accepted.</p>
          <ul className="issue-list">
            {result.results.map((r, i) => (
              <li key={i}>
                <Badge variant="outline" className={r.status === 'FAILED' ? 'review-badge' : ''}>{r.status}</Badge>
                <strong>{r.filename}</strong>
                {r.evidence_reused ? <small>evidence reused</small> : null}
                {r.error ? <p className="form-error">{r.error}</p> : null}
                {r.blockers ? <p>{r.blockers} blocking issue{r.blockers === 1 ? '' : 's'} — open it in Review.</p> : null}
              </li>
            ))}
          </ul>
          <Button variant="ghost" size="sm" onClick={() => setResult(null)}><X size={14} /> Dismiss</Button>
        </div>
      )}
    </Card>
  )
}
